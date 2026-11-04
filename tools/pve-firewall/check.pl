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
