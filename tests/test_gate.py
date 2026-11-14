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
        "sort --compress-program=gzip /tmp/x",
        "openssl x509 -engine foo -in /tmp/c",
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
