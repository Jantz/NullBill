#!/usr/bin/perl
# Parse a rendered firewall directory with PVE::Firewall's own parser and fail
# on any warning it emits (unknown options, bad rules, missing ipsets).
use strict;
use warnings;
use PVE::Firewall;

my $dir = shift // die "usage: check.pl <firewall dir>\n";
my @problems;
local $SIG{__WARN__} = sub { push @problems, $_[0] };

sub parse {
    my ($file, $cluster, $empty, $env) = @_;
    my $conf = PVE::Firewall::generic_fw_config_parser($file, $cluster // $empty, $empty, $env);
    die "$file: empty or unreadable\n" if !%$conf;
    return $conf;
}

for my $cluster_file ("$dir/cluster.fw", "$dir/cluster-lan-open.fw") {
    my $cluster = parse($cluster_file, undef,
        { rules => [], options => {}, aliases => {}, groups => {}, group_comments => {}, ipset => {}, ipset_comments => {} },
        'cluster');
    parse("$dir/host.fw", $cluster, { rules => [], options => {} }, 'host');
    opendir(my $dh, $dir) or die "$dir: $!\n";
    for my $f (sort grep { /^\d+\.fw$/ } readdir($dh)) {
        parse("$dir/$f", $cluster, { rules => [], options => {}, aliases => {}, ipset => {}, ipset_comments => {} }, 'vm');
    }
}

if (@problems) {
    print STDERR @problems;
    exit 1;
}
print "pve-firewall parser: no problems\n";

# Compile each variant with the stock compiler, as on a host whose LAN is
# $lan, and send packets through it with PVE's own simulator: the closed
# variant must drop the LAN at 8006 and 22 and admit the vpn guest; both
# must admit the controller (host.fw) at the host's mgmt address.
use File::Temp qw(tempdir);
use File::Copy qw(copy);
use PVE::FirewallSimulator;

my $lan = '192.168.1.0/24';
my ($lan_client, $lan_host) = ('192.168.1.50', '192.168.1.2');
my %expect_lan = ('cluster.fw' => 'DROP', 'cluster-lan-open.fw' => 'ACCEPT');
my $failed = 0;
for my $variant (sort keys %expect_lan) {
    my $tmp = tempdir(CLEANUP => 1);
    copy("$dir/$variant", "$tmp/cluster.fw") or die "$variant: $!\n";
    copy("$dir/host.fw", "$tmp/host.fw") or die "host.fw: $!\n";
    PVE::Firewall::local_network($lan);
    my $vmdata = { qemu => {}, lxc => {}, testdir => $tmp };
    my ($ruleset, $ipsets) = PVE::Firewall::compile(undef, undef, $vmdata);
    my $cluster = PVE::Firewall::load_clusterfw_conf("$tmp/cluster.fw");
    my $first = sub { my $set = $cluster->{ipset}->{ $_[0] } or die "no ipset $_[0]\n"; $set->[0]->{cidr} };
    my ($vpn, $controller, $mgmt_host) = map { $first->($_) } qw(nb-svc-vpn nb-svc-nullbill nb-host);
    my @cases = (
        [ "LAN to 8006", $lan_client, $lan_host, 8006, $expect_lan{$variant} ],
        [ "LAN to 22", $lan_client, $lan_host, 22, $expect_lan{$variant} ],
        [ "vpn guest to 8006", $vpn, $mgmt_host, 8006, 'ACCEPT' ],
        [ "controller to 22 at the mgmt address", $controller, $mgmt_host, 22, 'ACCEPT' ],
        [ "controller to 8006 at the mgmt address", $controller, $mgmt_host, 8006, 'ACCEPT' ],
    );
    for my $case (@cases) {
        my ($what, $source, $dest, $port, $want) = @$case;
        my $test = { from => 'outside', to => 'host', source => $source, dest => $dest, dport => $port, action => 'QUERY' };
        my $got = PVE::FirewallSimulator::simulate_firewall($ruleset->{filter}, $ipsets, $dest, $vmdata, $test);
        if ($got ne $want) {
            print STDERR "$variant: $what: $got, want $want\n";
            $failed = 1;
        }
    }
}
exit 1 if $failed;
print "pve-firewall compiler: the LAN reaches 8006 and 22 only while open; the vpn guest and the controller always\n";
