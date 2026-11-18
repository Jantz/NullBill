"""What the page's agent may run, decided by the page (docs/adr/0010-page-agent.md).

The agent asks for an action; this module sorts it into a tier, and the
owner's mode says which tiers run without asking them:

- read: only looks (logs, status, listings). Runs in every mode.
- change: changes something that can be put back (a restart, an install, a
  setup run). Runs without asking in Auto and YOLO; Manual asks.
- risky: loses something or cuts access (deleting a guest or a file tree,
  rolling back, rebooting), or runs code the page cannot read ahead (a
  script, an interpreter, eval). Runs without asking only in YOLO;
  otherwise the owner approves and types the word the card shows.
- blocked: never through the agent, in any mode: erasing disks, deleting
  backups, reading secrets, changing who may sign in, silencing the command
  log, and touching the NullBill container the page and agent run in. The
  owner can still do these by hand in the Proxmox shell.

A command is read ahead token by token. This is a lexical check, the
owner's view of what the agent asks for, not a sandbox: anything it cannot
read ahead (an unknown program, a variable, a name built at run time) is at
least a change, never read, and code it cannot see into is risky, so a new
trick costs the owner a tap rather than running unseen.
"""

import fnmatch
import posixpath
import re
import shlex
from dataclasses import dataclass

READ, CHANGE, RISKY, BLOCKED = "read", "change", "risky", "blocked"
ORDER = [READ, CHANGE, RISKY, BLOCKED]
MODES = ("manual", "auto", "yolo")
# The tiers each mode runs without asking.
RUNS = {"manual": {READ}, "auto": {READ, CHANGE}, "yolo": {READ, CHANGE, RISKY}}
MAX_DEPTH = 6


@dataclass(frozen=True)
class Verdict:
    tier: str
    why: str
    # What the owner types to approve a risky action.
    word: str = ""

    def worse(self, other: "Verdict") -> "Verdict":
        return other if ORDER.index(other.tier) > ORDER.index(self.tier) else self


def runs_unasked(mode: str, tier: str) -> bool:
    return tier in RUNS.get(mode, RUNS["manual"])


def read(why: str = "only reads") -> Verdict:
    return Verdict(READ, why)


def change(why: str) -> Verdict:
    return Verdict(CHANGE, why)


def risky(why: str, word: str) -> Verdict:
    return Verdict(RISKY, why, word)


def blocked(why: str) -> Verdict:
    return Verdict(BLOCKED, why)


def unseen(why: str) -> Verdict:
    """Code the page cannot read ahead."""
    return risky(f"{why}, which the page cannot read ahead", "run")


# Paths whose contents are secrets: keys, tokens, the sops file and age key,
# PVE's private directory, each guest's /etc/nullbill, service configs that
# hold passwords, a process's environment and memory. Matched anywhere in a
# normalised token and in the raw text, so a quoted or joined path is caught.
# /var/lib/nullbill is the NullBill container's home (secrets, sessions, the
# CA); on the host and in guests the same folder keeps state files with no
# secret in them, named in STATE_FILES, which a diagnosis needs to read.
STATE_FILES = (
    r"release-upgrade\.(?:result|held)|kept-copies|discarded-journals|worlds\.json|torrents-log\.json"
    r"|power\.json|backups-seen\.json|updates-since|game-last-active|jellyfin-scan-owed"
    r"|jellyfin-libraries"
)
SECRET_PATHS = re.compile(
    r"/etc/pve/priv|(^|/)priv(/|$)|\.ssh/id_|ssh_host_[a-z0-9]+_key(?!\.pub)|(^|/)g?shadow-?$|/etc/g?shadow"
    rf"|/var/lib/nullbill(?!-agent)(?!/(?:{STATE_FILES})(?![\w./-]))|/etc/nullbill|\.sops\.|keys\.txt|/\.config/sops|age-key|private[-_]key"
    r"|\.key\b|-key\.pem|/etc/ssl/private|/etc/step-ca|/var/lib/step|credentials\.json|\.netrc|authkey|token\.cfg"
    r"|/proc/\S*/(environ|mem)(?![\w.-])|/proc/kcore|/var/lib/tailscale|app\.ini|/etc/litellm"
    r"|/etc/proxmox-backup/(shadow\.json|token\.shadow|.*\.key)|/root/\.ssh/(?!known_hosts|config\b)"
    r"|/var/lib/samba/private|/etc/wireguard|(^|/)(passdb|secrets)\.tdb\b"
)
# A file or folder whose name says it holds a secret, anywhere in a path:
# Forgejo's /etc/forgejo/secrets, Factorio's rcon-password, nb-page's
# page-token, the model gateway's api-key. Only in a word with a slash, so
# `grep password` stays a search.
SECRET_NAMES = re.compile(r"(^|/)[^/\s]*(secret|(?<!common-)password|passphrase|api[-_]key|[-_.]token\b)[^/\s]*(/|$)", re.IGNORECASE)
# Example files in each secret place, for telling whether a wildcard could
# reach one. "*" in a component stands for any name there.
SECRET_EXAMPLES = [
    "/etc/pve/priv/authkey.key",
    "/etc/pve/priv/token.cfg",
    "/root/.ssh/id_ed25519",
    "/root/.ssh/id_rsa",
    "/home/*/.ssh/id_ed25519",
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/ssh/ssh_host_ed25519_key",
    "/etc/ssh/ssh_host_rsa_key",
    "/etc/ssh/ssh_host_ecdsa_key",
    "/var/lib/nullbill/secrets.sops.yaml",
    "/etc/nullbill/tls/web.key",
    "/etc/ssl/private/ssl-cert-snakeoil.key",
    "/etc/step-ca/secrets/root_ca_key",
    "/var/lib/step/secrets/x",
    "/var/lib/tailscale/tailscaled.state",
    "/etc/forgejo/app.ini",
    "/etc/litellm/config.yaml",
    "/etc/proxmox-backup/shadow.json",
    "/etc/proxmox-backup/token.shadow",
    "/proc/1/environ",
    "/proc/kcore",
    "/etc/forgejo/secrets/secret_key",
    "/etc/factorio/rcon-password",
    "/etc/grafana/admin-password",
    "/etc/llama/api-key",
    "/var/lib/samba/private/passdb.tdb",
    "/etc/wireguard/wg0.conf",
]
SECRET_PLACES = sorted({posixpath.dirname(p) for p in SECRET_EXAMPLES} | {"/etc/shadow", "/etc/gshadow"})
# Files that say who may sign in or that keep the record of what ran.
PROTECTED_FILES = re.compile(
    r"authorized_keys|authorized_principals|trusted-user-ca|TrustedUserCAKeys|sudoers|sshd_config|/etc/pam\.d"
    r"|/etc/passwd|nb-agent-command|nb-log-tool-use|journald\.conf|/etc/systemd/journal|/etc/audit|/var/log/audit"
    r"|/var/log/journal|/run/log/journal|/nb-log-shipper|/etc/nullbill/shipper|/etc/rsyslog"
    r"|/(systemd-journald|auditd|rsyslog)[\w@.-]*\.(service|socket)"
)
# Devices that are not disks.
SAFE_DEVICES = re.compile(r"^/dev/(null|zero|u?random|stdin|stdout|stderr|tty|fd/[0-2])$")
DISK_DEVICE = re.compile(r"/dev/(sd|hd|vd|xvd|nvme|mmcblk|md|dm-|mapper/|disk/|loop|zd|zvol/|pve/|nb-)")
# Units whose stop would silence the record of what ran.
LOG_UNITS = ("systemd-journal", "auditd", "nb-log-shipper", "rsyslog", "syslog")
LOG_PROCESSES = re.compile(r"journal|audit|rsyslog|syslog|log-shipper")
LOG_PACKAGES = re.compile(r"^(auditd|rsyslog|systemd-journal-remote|systemd|libaudit.*)(:.*)?$")
# Programs that wipe or repartition disks.
DISK_WIPERS = {
    "wipefs",
    "blkdiscard",
    "shred",
    "mkswap",
    "sgdisk",
    "sfdisk",
    "gdisk",
    "cgdisk",
    "mke2fs",
    "pvcreate",
    "vgcreate",
    "srm",
}
# Programs that print what they read: kept from secrets and raw disks.
CONTENT_READERS = {
    "cat",
    "tac",
    "head",
    "tail",
    "strings",
    "od",
    "hexdump",
    "xxd",
    "grep",
    "egrep",
    "fgrep",
    "zgrep",
    "rgrep",
    "zcat",
    "bzcat",
    "xzcat",
    "zstdcat",
    "cmp",
    "diff",
    "base64",
    "base32",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "sha512sum",
    "b2sum",
    "cksum",
    "nl",
    "cut",
    "jq",
    "wc",
    "sort",
    "uniq",
    "column",
    "file",
    "awk",
    "sed",
}
# Read-only programs: whatever their arguments, they only read and print.
READERS = {
    "cat",
    "tac",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "zgrep",
    "zcat",
    "bzcat",
    "xzcat",
    "zstdcat",
    "ls",
    "stat",
    "df",
    "du",
    "free",
    "uptime",
    "uname",
    "id",
    "whoami",
    "groups",
    "ps",
    "lsblk",
    "lspci",
    "lsusb",
    "lscpu",
    "lsmod",
    "wc",
    "cut",
    "tr",
    "nl",
    "column",
    "jq",
    "file",
    "which",
    "type",
    "echo",
    "printf",
    "true",
    "false",
    "test",
    "[",
    "[[",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "sha512sum",
    "b2sum",
    "cksum",
    "base64",
    "findmnt",
    "sensors",
    "pveversion",
    "nproc",
    "vmstat",
    "iostat",
    "pidof",
    "pgrep",
    "last",
    "lastb",
    "lastlog",
    "w",
    "who",
    "diff",
    "cmp",
    "strings",
    "od",
    "blkid",
    "netstat",
    "dig",
    "nslookup",
    "host",
    "ping",
    "traceroute",
    "tracepath",
    "mtr",
    "lvs",
    "vgs",
    "pvs",
    "lvdisplay",
    "vgdisplay",
    "pvdisplay",
    "pvereport",
    "hexdump",
    "xxd",
    "printenv",
    "locale",
    "lsof",
    "numastat",
    "lsinitramfs",
    "sleep",
    "cd",
    "pwd",
    "pushd",
    "popd",
    "dirs",
    "wait",
    "exit",
    "return",
    ":",
    "seq",
    "date",
    "cal",
    "getconf",
    "lsns",
    "lslocks",
    "lsipc",
    "pvenode",
    "pveperf",
    "nvme-list",
    "vainfo",
    "clinfo",
    "vulkaninfo",
    "systemd-analyze",
    "systemd-detect-virt",
    "systemd-cgls",
    "systemd-cgtop",
    "busctl",
    "lshw",
    "hwinfo",
    "inxi",
    "ipcs",
    "mpstat",
    "pidstat",
    "sar",
    "top",
    "htop",
}
# Programs that only read unless a flag says otherwise.
WRITE_FLAGS = {
    "tree": ("-o",),
    "date": ("-s", "--set"),
    "iptables-save": ("-f", "--file"),
    "ip6tables-save": ("-f", "--file"),
    "dmidecode": ("--dump-bin",),
    "top": ("-w",),
    "ethtool": (
        "-s",
        "-K",
        "-A",
        "-G",
        "-C",
        "-L",
        "-E",
        "-N",
        "-U",
        "-X",
        "-f",
        "-t",
        "-r",
        "-W",
        "--change",
        "--features",
        "--pause",
        "--set-ring",
        "--coalesce",
        "--set-channels",
        "--change-eeprom",
        "--reset",
        "--flash",
        "--test",
        "--negotiate",
        "--set-dump",
        "--set-priv-flags",
        "--set-eee",
        "--set-fec",
        "--set-phy-tunable",
        "--set-module",
        "--config-nfc",
        "--config-ntuple",
        "--set-rxfh-indir",
        "--cable-test",
    ),
    "ss": ("-K", "--kill"),
    "arp": ("-d", "-s", "-f", "--delete", "--set", "--file"),
    "systemd-analyze": ("--image", "--root"),
    "busctl": ("call", "set-property", "emit"),
    "sensors": ("-s", "--set"),
}
READ_VERBS = {
    "timedatectl": {"", "status", "show", "list-timezones", "timesync-status", "show-timesync"},
    "hostnamectl": {"", "status", "hostname"},
    "resolvectl": {"", "status", "query", "statistics", "dns", "domain"},
    "loginctl": {"", "list-sessions", "list-users", "show-session", "show-user", "session-status", "user-status"},
    "networkctl": {"", "list", "status", "lldp", "label"},
    "crontab": {"-l"},
}
# Programs that only read with these flags alone (or none).
BARE_READERS = {
    "mount": {"-l", "--show-labels", "-t"},
    "swapon": {"-s", "--summary", "--show", "--bytes", "--noheadings"},
    "hostname": {"-f", "--fqdn", "-i", "-I", "-A", "-d", "--domain", "-s", "--short", "--all-ip-addresses"},
    "getent": set(),
}
# Wrappers that run the rest of the line as a command, the options of each
# that take a value, and how many plain arguments come before the command.
WRAPPERS = {
    "env": ({"-u", "-C", "--unset", "--chdir"}, 0),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "-n", "--class", "--classdata"}, 0),
    "nohup": (set(), 0),
    "sudo": ({"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-T", "-D", "--user", "--group"}, 0),
    "doas": ({"-u", "-C"}, 0),
    "runuser": ({"-u", "-g", "-G", "--user", "--group"}, 0),
    "stdbuf": ({"-i", "-o", "-e"}, 0),
    "unbuffer": (set(), 0),
    "xargs": ({"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a", "--arg-file", "--delimiter", "--max-args", "--max-procs"}, 0),
    "time": ({"-o", "-f", "--output", "--format"}, 0),
    "command": (set(), 0),
    "builtin": (set(), 0),
    "exec": ({"-a"}, 0),
    "chrt": (set(), 1),
    "taskset": (set(), 1),
    "setsid": (set(), 0),
    "flock": ({"-w", "-E", "--timeout", "--conflict-exit-code"}, 1),
    "watch": ({"-n", "--interval", "-d"}, 0),
    "chroot": (set(), 1),
    "nsenter": ({"-t", "-S", "-G", "--target", "--setuid", "--setgid"}, 0),
    "systemd-run": ({"-u", "-p", "-E", "-M", "-H", "--unit", "--property", "--setenv", "--uid", "--gid", "--slice"}, 0),
    "busybox": (set(), 0),
    "toybox": (set(), 0),
}
# Wrappers that change something even around a read: run it later, apart,
# or as another service.
WRAPPERS_THAT_CHANGE = {"nohup", "setsid", "systemd-run", "watch", "chroot", "nsenter", "xargs", "flock"}
# Programs that print names, not contents: a wildcard that could match a
# secret is fine for them.
LISTERS = {
    "ls",
    "stat",
    "du",
    "echo",
    "printf",
    "file",
    "wc",
    "find",
    "test",
    "[",
    "[[",
    "realpath",
    "readlink",
    "basename",
    "dirname",
    "lsattr",
    "namei",
    "getfacl",
}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "busybox-sh"}
INTERPRETERS = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|perl[0-9.]*|ruby[0-9.]*|irb|node|nodejs|deno|bun|php[0-9.]*|lua[0-9.]*|luajit|tclsh"
    r"|wish|expect|Rscript|R|gdb|ghci|guile|racket|osascript|pwsh|powershell|julia|ipython[0-9.]*|vim?|nvim|emacs|ed|ex)$"
)
EVALUATORS = {"eval", "source", ".", "trap", "enable", "hash", "coproc", "alias", "fc", "bind"}
KEYWORDS = {"{", "}", "!", "if", "then", "do", "else", "elif", "fi", "done", "while", "until", "esac", "in", "]]"}
WORD_LISTS = {"for", "select", "case"}
SIGN_IN = {
    "passwd",
    "chpasswd",
    "usermod",
    "useradd",
    "userdel",
    "adduser",
    "deluser",
    "gpasswd",
    "groupmod",
    "groupadd",
    "groupdel",
    "visudo",
    "vipw",
    "vigr",
    "chage",
    "chsh",
    "pam-auth-update",
    "ssh-keygen",
    "ssh-copy-id",
}
REMOTE = {"ssh", "scp", "sftp", "sshpass", "rsh", "mosh"}
OPERATORS = {"|", "||", "&&", ";", "&", "|&", ";;", ";&", ";;&"}
PUNCTUATION = re.compile(r"\|\||&&|;;&?|;&|\|&|&>>|&>|>>|>\||>&|<<<|<<-?|<>|<&|[|&;<>()]")
# Environment names that change which code runs.
DANGEROUS_NAMES = re.compile(
    r"^(LD_.*|BASH_ENV|ENV|PATH|PYTHON.*|PERL5.*|PERLLIB|RUBY.*|NODE_.*|IFS|PROMPT_COMMAND|SHELLOPTS|BASHOPTS"
    r"|GIT_.*|EDITOR|VISUAL|SUDO_.*|SSH_ASKPASS|LESS.*|MANPAGER|GCONV_PATH|HOSTALIASES|TMPDIR|HOME|SHELL|BASH_FUNC_.*)$"
)
SAFE_NAMES = re.compile(r"^(LANG|LANGUAGE|LC_[A-Z]+|TZ|COLUMNS|LINES|TERM|NO_COLOR|SYSTEMD_COLORS|SYSTEMD_LESS)$")
PAGER_NAMES = {"PAGER", "SYSTEMD_PAGER"}
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^]]*\])?\+?=(.*)$", re.S)
PROGRAM_NAME = re.compile(r"^[A-Za-z0-9_.+:@/-]+$|^\[\[?$|^:$")


def classify(command: str, protected: set[str] | None = None, depth: int = 0) -> Verdict:
    """The tier of one shell command line, as run by root on a machine.
    protected: VMIDs and guest names the agent must never touch (the
    NullBill container)."""
    protected = protected or set()
    text = command.strip()
    if not text:
        return blocked("is empty")
    if len(text) > 4000:
        return blocked("is longer than 4000 characters; split it up")
    if depth > MAX_DEPTH:
        return blocked("nests commands too deep to read ahead")
    verdict = raw_checks(text)
    if verdict.tier == BLOCKED:
        return verdict
    # Brace expansion makes words the text does not spell: read the words
    # bash makes, and never less than risky.
    try:
        expanded = braces(text)
    except ValueError:
        return blocked("its brace expansion makes more words than the page reads ahead; name them instead")
    if expanded is not None:
        inner = classify(expanded, protected, depth + 1)
        if inner.tier == BLOCKED:
            return inner
        return verdict.worse(inner).worse(
            risky("uses brace expansion ({a,b} or {1..3}), which makes words the command does not spell", "run")
        )
    for inner in substitutions(text):
        verdict = verdict.worse(change("runs a command inside a command"))
        verdict = verdict.worse(classify(inner, protected, depth + 1) if inner.strip() else read())
        if verdict.tier == BLOCKED:
            return verdict
    try:
        lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        raw = list(lexer)
    except ValueError as exc:
        return blocked(f"cannot be read ({exc})")
    tokens: list[str] = []
    for token in raw:
        tokens.extend(PUNCTUATION.findall(token) if token and set(token) <= set("|&;<>()") else [token])
    verdict = verdict.worse(secret_tokens(tokens))
    if verdict.tier == BLOCKED:
        return verdict
    segment: list[str] = []
    segments: list[list[str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in OPERATORS:
            if token == "&":
                verdict = verdict.worse(change("runs something in the background"))
            segments.append(segment)
            segment = []
        elif token in {"(", ")"}:
            verdict = verdict.worse(change("uses a subshell"))
            segments.append(segment)
            segment = []
        elif PUNCTUATION.fullmatch(token):
            # A redirection: the fd number, if any, is the end of segment.
            if segment and segment[-1].isdigit():
                segment.pop()
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
            verdict = verdict.worse(redirect(token, target, protected))
        else:
            segment.append(token)
        i += 1
    segments.append(segment)
    # Where relative names point: after cd or pushd, the folder it named;
    # before, root's home on Proxmox or / in a guest.
    cwd: str | None = None
    for argv in segments:
        if not argv:
            continue
        verdict = verdict.worse(program(argv, protected, depth))
        if verdict.tier == BLOCKED:
            return verdict
        verdict = verdict.worse(relative_names(argv, cwd, protected, depth))
        if verdict.tier == BLOCKED:
            return verdict
        cwd = moved_to(argv, cwd)
    for token in tokens:
        # Reading these is fine; an option's value names what a program
        # writes (--output=FILE) as often as what it reads.
        value = token.split("=", 1)[-1] if token.startswith("-") else token
        if PROTECTED_FILES.search(value) and (verdict.tier != READ or (token.startswith("-") and "/" in value)):
            return blocked(f"touches {token}, which says who may sign in or keeps the record of what ran")
    return verdict.worse(protected_ids(tokens, protected))


# What `cd` with no folder and relative names start from: root's home on
# Proxmox, / in a guest.
START = ("/root", "/")
UNKNOWN = "?"
# Programs whose -C or --directory names the folder they work in.
CHDIR = {"tar", "gtar", "bsdtar", "git", "make", "gmake", "env"}
# Programs that read whole trees, for which relative names are read even
# without a cd before them.
TREE_READERS = {"tar", "gtar", "bsdtar", "zip", "grep", "egrep", "fgrep", "zgrep", "rgrep", "cp", "rsync", "find"}


def bare(argv: list[str]) -> list[str]:
    while argv and argv[0] in KEYWORDS:
        argv = argv[1:]
    while argv and ASSIGNMENT.match(argv[0]):
        argv = argv[1:]
    return argv


def moved_to(argv: list[str], cwd: str | None) -> str | None:
    argv = bare(argv)
    if not argv:
        return cwd
    name = argv[0].rsplit("/", 1)[-1]
    if name == "popd":
        return UNKNOWN
    if name not in {"cd", "pushd"}:
        return cwd
    where = next((a for a in argv[1:] if not a.startswith("-") or a == "-"), "~")
    return joined(cwd, where)


def joined(cwd: str | None, where: str) -> str:
    if where == "-" or re.search(r"[$`*?[]", where):
        return UNKNOWN
    where = normalise(where)
    if where.startswith("/"):
        return where
    if cwd is None or cwd == UNKNOWN:
        return UNKNOWN if cwd == UNKNOWN else posixpath.join(START[0], where)
    return normalise(posixpath.join(cwd, where))


def chdirs(argv: list[str]) -> list[str]:
    name = argv[0].rsplit("/", 1)[-1]
    if name not in CHDIR:
        return []
    out = []
    args = argv[1:]
    for n, a in enumerate(args):
        following = args[n + 1] if n + 1 < len(args) else ""
        if a == "-C" or a in {"--git-dir", "--work-tree"}:
            out.append(following)
        elif a.startswith(("--git-dir=", "--work-tree=")):
            out.append(a.split("=", 1)[1])
        elif long_name(a, "--directory") and len(a.split("=", 1)[0]) >= 5 or long_name(a, "--chdir"):
            out.append(a.split("=", 1)[1] if "=" in a else following)
        elif a.startswith("-C") and len(a) > 2 and name != "git":
            out.append(a[2:])
    return [d for d in out if d]


def relative_names(argv: list[str], cwd: str | None, protected: set[str], depth: int) -> Verdict:
    """The command again with its relative names read from the folder they
    point into: `cd /root/.ssh && base64 id_ed25519` reads a key, and so
    does `tar c -C /root .ssh`."""
    argv = bare(argv)
    if len(argv) < 2:
        return read()
    name = argv[0].rsplit("/", 1)[-1]
    relative = [a for a in argv[1:] if a and not a.startswith(("/", "-", "~", "$")) and "://" not in a and not ASSIGNMENT.match(a)]
    if not relative:
        return read()
    extra = [joined(cwd, d) for d in chdirs(argv)]
    # Without a cd, only a tree a program reads whole is looked into: a
    # relative name may be a pattern (grep password) as often as a file.
    named = [cwd] if cwd else []
    implied = [*START] if not cwd and name in TREE_READERS else []
    verdict = read()
    for folder, said in [(f, True) for f in named + extra] + [(f, False) for f in implied]:
        if folder == UNKNOWN:
            verdict = verdict.worse(change("works on names in a folder the page cannot name"))
            continue
        moved = [argv[0]] + [posixpath.join(folder, a) if a in relative else a for a in argv[1:]]
        checks = [program(moved, protected, depth)]
        if said:
            checks += [secret_tokens(moved), protected_ids(moved, protected)]
        for found in checks:
            if found.tier == BLOCKED:
                return found
    return verdict


def braces(text: str) -> str | None:
    """text with bash's brace expansion done, or None when it has none.
    Raises ValueError when it makes more than BRACE_MAX words."""
    chars = shell_chars(text)
    out: list[str] = []
    word: list[tuple[str, bool]] = []
    found = False
    tick = False

    def flush() -> None:
        nonlocal found
        if word:
            words, made = expand(list(word))
            found = found or made
            out.append(" ".join("".join(c for c, _ in w) for w in words))
            word.clear()

    for c, active in chars:
        if active and c == "`":
            tick = not tick
        if active and not tick and (c.isspace() or c in "|&;<>()"):
            flush()
            out.append(c)
        else:
            word.append((c, active))
    flush()
    return "".join(out) if found else None


BRACE_MAX = 256


def shell_chars(text: str) -> list[tuple[str, bool]]:
    """Each character of text, and whether bash reads it as syntax: outside
    quotes, not escaped, not in a here-document's body."""
    out: list[tuple[str, bool]] = []
    quote = ""
    pending: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            out.append((c, False))
            quote = "" if c == "'" else quote
        elif c == "\\":
            out.append((c, False))
            if i + 1 < n:
                out.append((text[i + 1], False))
                i += 1
        elif quote == "$'":
            out.append((c, False))
            quote = "" if c == "'" else quote
        elif quote == '"':
            out.append((c, False))
            quote = "" if c == '"' else quote
        elif c in "'\"":
            out.append((c, False))
            quote = c
        elif c == "$" and text[i + 1 : i + 2] == "'":
            out += [(c, False), ("'", False)]
            quote = "$'"
            i += 1
        elif text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([^\s'\"|&;<>()]+)\1", text[i:])
            if m:
                pending.append(m.group(2))
            out.append((c, True))
        elif c == "\n" and pending:
            out.append((c, True))
            j = i + 1
            for delimiter in pending:
                while j < n:
                    end = text.find("\n", j)
                    end = n if end < 0 else end + 1
                    out += [(ch, False) for ch in text[j:end]]
                    line = text[j:end].rstrip("\n").lstrip("\t")
                    j = end
                    if line == delimiter:
                        break
            pending = []
            i = j
            continue
        else:
            out.append((c, True))
        i += 1
    return out


def closing(word: list[tuple[str, bool]], i: int) -> tuple[int | None, list[int]]:
    """Where the brace at i closes, and the commas at its own level."""
    depth = 0
    commas = []
    j = i
    while j < len(word):
        c, active = word[j]
        if active and c == "$" and j + 1 < len(word) and word[j + 1] == ("{", True):
            end, _ = closing(word, j + 1)
            if end is None:
                return None, []
            j = end + 1
            continue
        if active and c == "{":
            depth += 1
        elif active and c == "}":
            depth -= 1
            if depth == 0:
                return j, commas
        elif active and c == "," and depth == 1:
            commas.append(j)
        j += 1
    return None, []


def sequence(inner: list[tuple[str, bool]]) -> list[str] | None:
    if not all(active for _, active in inner):
        return None
    text = "".join(c for c, _ in inner)
    m = re.fullmatch(r"([-+]?\d+)\.\.([-+]?\d+)(?:\.\.([-+]?\d+))?", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        step = abs(int(m.group(3) or 1)) or 1
        if abs(b - a) // step + 1 > BRACE_MAX:
            raise ValueError("too many words")
        width = max(len(m.group(1)), len(m.group(2))) if re.match(r"[-+]?0\d", m.group(1) + " ") or re.match(r"[-+]?0\d", m.group(2)) else 0
        numbers = range(a, b + (1 if b >= a else -1), step if b >= a else -step)
        return [str(x).zfill(width) for x in numbers]
    m = re.fullmatch(r"([A-Za-z])\.\.([A-Za-z])(?:\.\.([-+]?\d+))?", text)
    if m:
        a, b = ord(m.group(1)), ord(m.group(2))
        step = abs(int(m.group(3) or 1)) or 1
        return [chr(x) for x in range(a, b + (1 if b >= a else -1), step if b >= a else -step)]
    return None


def expand(word: list[tuple[str, bool]]) -> tuple[list[list[tuple[str, bool]]], bool]:
    """bash's brace expansion of one word: the words it makes, and whether
    it made any."""
    i = 0
    while i < len(word):
        c, active = word[i]
        if active and c == "$" and i + 1 < len(word) and word[i + 1] == ("{", True):
            end, _ = closing(word, i + 1)
            if end is None:
                return [word], False
            i = end + 1
            continue
        if active and c == "{":
            end, commas = closing(word, i)
            if end is not None:
                if commas:
                    parts = []
                    start = i + 1
                    for k in [*commas, end]:
                        parts.append(word[start:k])
                        start = k + 1
                else:
                    made = sequence(word[i + 1 : end])
                    parts = [[(ch, False) for ch in x] for x in made] if made is not None else None
                if parts is not None:
                    out = []
                    for part in parts:
                        out += expand(word[:i] + part + word[end + 1 :])[0]
                        if len(out) > BRACE_MAX:
                            raise ValueError("too many words")
                    return out, True
        i += 1
    return [word], False


def raw_checks(text: str) -> Verdict:
    """What the text gives away before it is split: secrets, raw disk
    writes, wipers named anywhere (inside a string another program runs),
    and quoting the page cannot read ahead."""
    for word in re.split(r"[\s'\"=;|&()`<>]+", text):
        if is_secret(word):
            return blocked(f"touches {word}, which holds secrets; secrets never go to the model")
        name = word.rsplit("/", 1)[-1]
        if name in DISK_WIPERS or name.startswith("mkfs"):
            return blocked(f"names {name}, which erases disks; erase disks on the Setup page")
    if re.search(r"(of=|>\s*)" + DISK_DEVICE.pattern, text):
        return blocked("writes straight to a disk")
    if re.search(r"\b(kill|pkill|killall)\b.*(" + LOG_PROCESSES.pattern + ")", text):
        return blocked("ends the programs that keep the record of what ran")
    verdict = read()
    if re.search(r"\$'", text):
        verdict = verdict.worse(unseen("uses $'…' quoting"))
    if unquoted(text, r"\$[A-Za-z_{0-9@*#?$!-]"):
        verdict = verdict.worse(change("uses a shell variable, whose value the page cannot read ahead"))
    return verdict


def unquoted(text: str, pattern: str) -> bool:
    """Whether pattern occurs outside single quotes."""
    rx = re.compile(pattern)
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif rx.match(text, i):
            return True
        i += 1
    return False


def substitutions(text: str) -> list[str]:
    """The commands inside $( ), backticks, <( ) and >( ), outside single
    quotes."""
    out = []
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            i += 1
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif c == "`":
            end = text.find("`", i + 1)
            out.append(text[i + 1 : end if end >= 0 else len(text)])
            i = end if end >= 0 else len(text)
        elif c in "$<>" and text[i + 1 : i + 2] == "(" and (c == "$" or not quote):
            depth = 0
            j = i + 1
            while j < len(text):
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                if depth == 0:
                    break
                j += 1
            inner = text[i + 2 : j]
            # $(( )) is arithmetic; what is inside still counts.
            out.append(inner[1:-1] if inner.startswith("(") and inner.endswith(")") else inner)
            i = j
        i += 1
    return out


def normalise(token: str) -> str:
    if token.startswith("~"):
        user, _, rest = token[1:].partition("/")
        token = ("/root" if not user else f"/home/{user}") + ("/" + rest if rest else "")
    if token.startswith("/"):
        token = posixpath.normpath(token)
        token = "/" + token.lstrip("/")
    return token


def is_secret(word: str) -> bool:
    """Whether a word names a place that holds secrets."""
    path = normalise(word)
    if not path or path.endswith(".pub"):
        return False
    return bool(SECRET_PATHS.search(path) or ("/" in path and SECRET_NAMES.search(path)))


def secret_tokens(tokens: list[str]) -> Verdict:
    for token in tokens:
        for part in [token, *token.split("=", 1)[1:]]:
            if is_secret(part):
                return blocked(f"touches {part}, which holds secrets; secrets never go to the model")
    return read()


def wildcards(args: list[str]) -> Verdict:
    for arg in args:
        path = normalise(arg.split("=", 1)[-1])
        if re.search(r"[*?[]", path) and path.startswith("/") and wildcard_reaches_secret(path):
            return blocked(f"{arg} could match a file that holds secrets; name the files instead")
    return read()


def wildcard_reaches_secret(pattern: str) -> bool:
    parts = pattern.strip("/").split("/")
    for example in SECRET_EXAMPLES:
        wanted = example.strip("/").split("/")
        if len(parts) < len(wanted) and not parts[-1].endswith("*"):
            continue
        if all(w == "*" or fnmatch.fnmatchcase(w, p) for w, p in zip(wanted, parts, strict=False)):
            return True
    return False


def holds_secrets(path: str) -> bool:
    """Whether a directory holds a secret place, for recursive readers."""
    path = normalise(path)
    if path in {".", "..", "*", ""} or not path.startswith("/"):
        return path in {".", "..", "*", ""}
    return any(place == path or place.startswith(path.rstrip("/") + "/") for place in SECRET_PLACES)


def redirect(op: str, target: str, protected: set[str]) -> Verdict:
    if op in {"<<", "<<-", "<<<"}:
        return read()
    if op in {">&", "<&"} and (target.isdigit() or target == "-"):
        return read()
    if op == "<":
        if target.startswith("/dev/") and not SAFE_DEVICES.match(target):
            return blocked(f"reads {target} directly")
        return read("reads a file")
    if target in {"/dev/null", "/dev/stdout", "/dev/stderr"}:
        return read()
    return writes_file(target, protected)


def writes_file(target: str, protected: set[str]) -> Verdict:
    path = normalise(target)
    if PROTECTED_FILES.search(path):
        return blocked(f"writes {target}, which says who may sign in or keeps the record of what ran")
    if path.startswith("/dev/") and not SAFE_DEVICES.match(path):
        return blocked(f"writes straight to {target}")
    if path == "/proc/sysrq-trigger":
        return risky("writes the kernel's emergency keys, which can reboot or stop the machine at once", "reboot")
    if is_secret(path):
        return blocked(f"writes {target}, which holds secrets")
    if any(re.search(rf"/lxc/{re.escape(p)}\.conf$", path) for p in protected):
        return blocked(f"writes {target}, the NullBill container's settings")
    return change(f"writes {target}")


def program(argv: list[str], protected: set[str], depth: int = 0) -> Verdict:
    """The tier of one simple command."""
    while argv and argv[0] in KEYWORDS:
        argv = argv[1:]
    if not argv:
        return read()
    if argv[0] in WORD_LISTS:
        return read()
    if argv[0] == "function":
        return change("defines a function")
    verdict = read()
    while argv and ASSIGNMENT.match(argv[0]):
        verdict = verdict.worse(assignment(argv[0]))
        argv = argv[1:]
    if not argv:
        return verdict.worse(change("sets shell variables"))
    return verdict.worse(command(argv, protected, depth))


def assignment(token: str) -> Verdict:
    name, _index, value = ASSIGNMENT.match(token).groups()
    if DANGEROUS_NAMES.match(name):
        return unseen(f"sets {name}, which changes which code runs")
    if SAFE_NAMES.match(name) or (name in PAGER_NAMES and value in {"", "cat"}):
        return read()
    return change(f"sets {name}")


def command(argv: list[str], protected: set[str], depth: int) -> Verdict:
    word = argv[0]
    name = word.rsplit("/", 1)[-1]
    args = argv[1:]
    if not PROGRAM_NAME.match(word):
        return unseen(f"runs {word!r}, a name the shell builds as it runs")
    if name not in LISTERS:
        found = wildcards(args)
        if found.tier == BLOCKED:
            return found
    if name in {"export", "declare", "typeset", "local", "readonly"}:
        verdict = change(f"{name} sets shell variables")
        for a in args:
            if ASSIGNMENT.match(a):
                verdict = verdict.worse(assignment(a))
            elif not a.startswith("-") and DANGEROUS_NAMES.match(a):
                verdict = verdict.worse(unseen(f"{name} {a} changes which code runs"))
        return verdict if args else read()
    if name in WRAPPERS:
        return wrapper(name, args, protected, depth)
    if name in SHELLS:
        return shell(name, args, protected, depth)
    if name in {"su"}:
        if "-c" in args and args.index("-c") + 1 < len(args):
            return classify(args[args.index("-c") + 1], protected, depth + 1).worse(change("runs as another user"))
        return unseen("su opens a shell")
    if INTERPRETERS.match(name):
        if args in (["--version"], ["-V"], ["-v"]):
            return read()
        return unseen(f"runs {name}")
    if name in EVALUATORS:
        return unseen(f"{name} runs code from a string or a file")
    if name in CODE_RUNNERS:
        if args in (["--version"], ["-V"], ["-v"], ["--help"]):
            return read()
        return unseen(f"{name} runs the code of what it builds or installs")
    if name == "awk" or name in {"gawk", "mawk", "nawk", "busybox-awk"}:
        return r_awk(args)
    if name in SIGN_IN or name == "pveum" and not pveum_lists(args):
        return blocked(f"{name} changes who may sign in; do that yourself in the Proxmox shell")
    if name == "pveum":
        return read()
    if name in REMOTE:
        return risky(f"{name} reaches another machine; ask with where= to run on a guest", "connect")
    if name.startswith("lxc-"):
        return r_lxc(name, args, protected, depth)
    if name in CONTENT_READERS or name == "dd":
        verdict = content_reader(name, args)
        if verdict.tier == BLOCKED:
            return verdict
    rule = RULES.get(name)
    if rule is not None:
        return rule(args, protected, depth) if name in DEEP_RULES else rule(args, protected)
    if name.startswith("mkfs"):
        return blocked("formats a disk; erase disks on the Setup page")
    if name in DISK_WIPERS:
        return blocked(f"{name} erases or repartitions disks; erase disks on the Setup page")
    if name in WRITE_FLAGS:
        return change(f"{name} writes or changes something with these flags") if has(args, *WRITE_FLAGS[name]) else read()
    if name in READERS:
        return read()
    if name in READ_VERBS:
        plain = [a for a in args if not a.startswith("-")]
        v = args[0] if name == "crontab" and args else verb(args)
        extra = (name == "resolvectl" and len(plain) > 2) or (name == "hostnamectl" and len(plain) > 1)
        return read() if v in READ_VERBS[name] and not extra else change(f"{name} {v}".strip())
    if name in BARE_READERS:
        if name == "getent":
            return blocked("getent shadow reads password hashes") if {"shadow", "gshadow"} & set(args) else read()
        allowed = BARE_READERS[name]
        # mount -t TYPE alone lists the mounts of that type.
        plain = [a for n, a in enumerate(args) if not a.startswith("-") and not (name == "mount" and n and args[n - 1] == "-t")]
        flags = [a for a in args if a.startswith("-")]
        return read() if not plain and set(flags) <= allowed else change(f"{name} with these arguments changes the system")
    if name in {"reboot", "shutdown", "poweroff", "halt", "init", "telinit"}:
        return risky(f"{name} stops the machine", "reboot")
    return change(f"runs {name}, which the page does not know to be read-only")


def content_reader(name: str, args: list[str]) -> Verdict:
    """Readers that print what they read: never raw disks, never a tree
    that holds secrets."""
    paths = [a[3:] if a.startswith("if=") else a for a in args if not a.startswith("-") or a.startswith("if=")]
    for p in paths:
        if p.startswith("/dev/") and not SAFE_DEVICES.match(p):
            return blocked(f"{name} reads {p} directly")
    recursive = name == "rgrep" or (
        name in {"grep", "egrep", "fgrep", "zgrep"}
        and has(args, "-r", "-R", "--recursive", "--dereference-recursive", "-d", "--directories")
    )
    if recursive:
        plain = [a for a in args if not a.startswith("-")]
        roots = plain[1:] if len(plain) > 1 else ["."]
        for root in roots:
            if holds_secrets(root):
                return blocked(f"{name} -r over {root} would read files that hold secrets; search a narrower place")
    return read()


def wrapper(name: str, args: list[str], protected: set[str], depth: int) -> Verdict:
    takes_value, positionals = WRAPPERS[name]
    i = 0
    flags: list[str] = []
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        flags.append(a)
        i += 2 if a in takes_value else 1
    rest = args[i:]
    verdict = read()
    if name == "env":
        while rest and ASSIGNMENT.match(rest[0]):
            verdict = verdict.worse(assignment(rest[0]))
            rest = rest[1:]
        if has(flags, "-S", "--split-string"):
            return unseen("env -S splits a string into a command")
    if name in {"sudo", "doas"} and has(flags, "-s", "-i", "--shell", "--login"):
        return unseen(f"{name} opens a shell")
    if name == "sudo" and has(flags, "-e", "--edit"):
        return change("sudo -e edits a file")
    if name == "time" and has(flags, "-o", "--output"):
        verdict = verdict.worse(change("time -o writes a file"))
    if name == "nsenter" and has(flags, "-t", "--target", "-a", "--all"):
        return blocked("nsenter enters another process's namespaces, which may be the NullBill container's; use pct exec for a guest")
    if name in {"taskset", "chrt"} and has(flags, "-p", "--pid"):
        return change(f"{name} changes a running process")
    if name == "command" and has(flags, "-v", "-V"):
        return read()
    if name == "flock" and has(flags, "-c", "--command"):
        inner = rest[positionals:]
        return classify(inner[0], protected, depth + 1).worse(change("flock takes a lock")) if inner else change("flock")
    rest = rest[positionals:]
    if not rest:
        if name in {"time", "command", "env", "exec", "builtin"}:
            return verdict
        return change(f"runs {name} with nothing to run")
    inner = program(rest, protected, depth + 1)
    if name in WRAPPERS_THAT_CHANGE:
        inner = inner.worse(change(f"{name} runs it apart from this command"))
    return verdict.worse(inner)


def shell(name: str, args: list[str], protected: set[str], depth: int) -> Verdict:
    """sh -c 'string' is read ahead; a script or a shell reading its input
    is not."""
    command_mode = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in {"--rcfile", "--init-file"}:
            return unseen(f"{name} {a} runs a script")
        if a in {"-o", "+o", "-O", "+O"}:
            i += 2
            continue
        if a == "--":
            i += 1
            break
        if a.startswith("--") or (len(a) > 1 and a[0] in "-+"):
            command_mode = command_mode or (a[0] == "-" and not a.startswith("--") and "c" in a[1:])
            i += 1
            continue
        break
    if i >= len(args):
        return unseen(f"{name} runs commands from its input")
    if command_mode:
        return classify(args[i], protected, depth + 1)
    return unseen(f"{name} runs the script {args[i]}")


def r_awk(args: list[str]) -> Verdict:
    program_text = None
    i = 0
    files = []
    while i < len(args):
        a = args[i]
        if a in {"-F", "-v"}:
            i += 2
            continue
        if a.startswith(("-F", "-v")) and len(a) > 2:
            i += 1
            continue
        if a in {"-e", "--source"}:
            program_text = (program_text or "") + "\n" + (args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if a == "--":
            i += 1
            continue
        if a.startswith("-"):
            return unseen(f"awk {a} runs a program file or changes how awk runs")
        if program_text is None:
            program_text = a
        else:
            files.append(a)
        i += 1
    if program_text is None:
        return change("awk without a program")
    if re.search(r"system|getline|\||ENVIRON|PROCINFO|@load|@include|fflush|close", program_text):
        return unseen("the awk program runs commands or reads its environment")
    if ">" in re.sub(r">=?\s*-?[\d.$]", "", program_text):
        return unseen("the awk program may write files")
    for f in files:
        if f.startswith("/dev/") and not SAFE_DEVICES.match(f):
            return blocked(f"awk reads {f} directly")
    return read()


SED_OPTIONS = {"-n", "-E", "-r", "--quiet", "--silent", "-s", "--separate", "-u", "--unbuffered", "-z", "--null-data", "--posix"}
SED_ADDR = r"(\d+|\$|/[^/]*/)"
SED_SIMPLE = re.compile(rf"^\s*({SED_ADDR}(,\s*{SED_ADDR})?\s*!?)?\s*[pdq=]?\s*$")


def sed_script_reads(script: str) -> bool:
    for part in re.split(r"[;\n]", script):
        if SED_SIMPLE.match(part):
            continue
        m = re.match(rf"^\s*({SED_ADDR}(,\s*{SED_ADDR})?)?\s*s(.)", part)
        if not m:
            return False
        delim = m.group(m.lastindex)
        rest = part[m.end() :]
        pieces = re.split(rf"(?<!\\){re.escape(delim)}", rest)
        if len(pieces) != 3 or not re.fullmatch(r"[gpiI0-9]*\s*", pieces[2]):
            return False
    return True


def r_sed(args, _p):
    scripts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in {"-e", "--expression"}:
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-") and a != "-":
            if has([a], "-i", "--in-place"):
                return change("sed -i changes files")
            if a not in SED_OPTIONS and not set(a[1:]) <= set("nErsuz"):
                return change(f"sed {a} reads a script or writes files")
        elif not scripts:
            scripts.append(a)
        i += 1
    if not scripts:
        return change("sed without a script")
    if all(sed_script_reads(s) for s in scripts):
        return read()
    return change("sed can write files or run commands (its w and e commands)")


def long_name(arg: str, flag: str) -> bool:
    """Whether arg names the long option flag: in full, or by a prefix, as
    GNU getopt_long, curl and git's parse-options take one (`--out=` for
    `--output=`). An ambiguous prefix counts too: the program refuses it,
    and the page reads it as the worse of its meanings."""
    if not (flag.startswith("--") and arg.startswith("--")):
        return False
    name = arg.split("=", 1)[0]
    return len(name) >= 3 and flag.startswith(name)


def has(args: list[str], *flags: str) -> bool:
    return any(
        a == f
        or long_name(a, f)
        or (len(f) == 2 and f[0] == "-" and f[1] != "-" and a.startswith("-") and not a.startswith("--") and f[1] in a[1:])
        for a in args
        for f in flags
    )


def values(args: list[str], short: str = "", *longs: str) -> list[str]:
    """The values given to an option: `-o X`, `-oX`, `-abo X`, `--output X`,
    `--output=X` and a long option's prefixes."""
    out = []
    for n, a in enumerate(args):
        following = args[n + 1] if n + 1 < len(args) else ""
        if a == "--":
            break
        if longs and a.startswith("--"):
            if any(long_name(a, f) for f in longs):
                out.append(a.split("=", 1)[1] if "=" in a else following)
        elif short and a.startswith("-") and len(a) > 1 and short[1] in a[1:]:
            rest = a[a.index(short[1], 1) + 1 :]
            out.append(rest or following)
    return out


def operands(args: list[str], takes_value: set[str] = frozenset()) -> list[str]:
    """The plain arguments: `-` (standard input or output) is one, an
    option's value is not."""
    out = []
    skip = False
    ended = False
    for a in args:
        if skip:
            skip = False
        elif ended or a == "-" or not a.startswith("-"):
            out.append(a)
        elif a == "--":
            ended = True
        elif a in takes_value or (a.startswith("--") and "=" not in a and any(long_name(a, f) for f in takes_value if f.startswith("--"))):
            skip = True
    return out


def verb(args: list[str]) -> str:
    return next((a for a in args if not a.startswith("-")), "")


def r_dmesg(args, _p):
    return (
        change("clears or changes the kernel log")
        if has(args, "-C", "-c", "-D", "-E", "-n", "--clear", "--read-clear", "--console-off", "--console-on", "--console-level")
        else read()
    )


def r_journalctl(args, _p):
    if has(
        args,
        "--vacuum-size",
        "--vacuum-time",
        "--vacuum-files",
        "--rotate",
        "--flush",
        "--relinquish-var",
        "--smart-relinquish-var",
        "--setup-keys",
    ) or any(a.startswith("--vacuum") for a in args):
        return blocked("deletes or moves the journal, the record of what ran")
    if has(args, "--update-catalog", "--new-id128"):
        return change("journalctl changes its catalog")
    return read()


READ_VERBS_SYSTEMCTL = {
    "",
    "status",
    "show",
    "cat",
    "is-active",
    "is-enabled",
    "is-failed",
    "is-system-running",
    "list-units",
    "list-unit-files",
    "list-timers",
    "list-dependencies",
    "list-sockets",
    "list-jobs",
    "list-machines",
    "get-default",
    "show-environment",
    "help",
}


LOG_UNIT_NAMES = ("systemd-journald", "systemd-journald.socket", "systemd-journald-dev-log.socket", "auditd", "nb-log-shipper", "rsyslog")


def is_log_unit(unit: str) -> bool:
    if unit.startswith(LOG_UNITS) or LOG_PROCESSES.search(unit):
        return True
    # A pattern systemctl matches against unit names.
    return bool(re.search(r"[*?[]", unit)) and any(
        fnmatch.fnmatchcase(n, unit) or fnmatch.fnmatchcase(n + ".service", unit) for n in LOG_UNIT_NAMES
    )


SYSTEMCTL_VALUES = {"-H", "-M", "-p", "-P", "-t", "-s", "-n", "-o", "--host", "--machine", "--property", "--type", "--state"}
SYSTEMCTL_VALUES |= {"--signal", "--lines", "--output", "--root", "--what", "--kill-whom", "--job-mode", "--timestamp"}


def r_systemctl(args, _p):
    if has(args, "-H", "--host", "-M", "--machine"):
        return risky("systemctl -H or -M reaches another machine or container", "connect")
    words = operands(args, SYSTEMCTL_VALUES)
    v = words[0] if words else ""
    units = {a.removesuffix(".service") for a in words[1:]}
    if v in READ_VERBS_SYSTEMCTL:
        return read()
    if v in {"stop", "disable", "mask", "kill", "freeze", "edit", "set-property", "revert", "link", "unlink", "clean"} and any(
        is_log_unit(u) for u in units
    ):
        return blocked("stops or changes the record of what ran")
    if v in {"reboot", "poweroff", "halt", "kexec", "soft-reboot", "rescue", "emergency", "isolate", "suspend", "hibernate"}:
        return risky(f"systemctl {v} stops the machine", "reboot")
    if v in {"stop", "disable", "mask", "kill"} and units & {
        "pve-firewall",
        "ssh",
        "sshd",
        "pveproxy",
        "pve-cluster",
        "pvedaemon",
        "tailscaled",
    }:
        return risky(f"stops {', '.join(sorted(units))}, which you may need to get in", "stop")
    return change(f"systemctl {v}")


# iproute2 takes any prefix of an object or a command, the first in its own
# order that fits: `ip l s` is `ip link set`, `ip a s` is `ip address show`.
IP_OBJECTS = [
    "address", "addrlabel", "maddress", "route", "rule", "neighbor", "neighbour", "ntable", "ntbl", "link", "l2tp", "fou",
    "ila", "macsec", "tunnel", "tunl", "tuntap", "tap", "token", "tcpmetrics", "tcp_metrics", "monitor", "xfrm", "mroute",
    "mrule", "netns", "netconf", "vrf", "sr", "nexthop", "mptcp", "ioam", "help", "stats",
]  # fmt: skip
IP_SAME = {"neighbour": "neighbor", "ntbl": "ntable", "tunl": "tunnel", "tap": "tuntap", "tcp_metrics": "tcpmetrics"}
IP_VERBS = {
    "address": ["add", "change", "chg", "replace", "delete", "list", "show", "lst", "flush", "save", "showdump", "restore", "help"],
    "link": ["add", "change", "chg", "replace", "delete", "set", "show", "lst", "list", "xstats", "afstats", "property", "help"],
    "route": [
        "add", "change", "chg", "replace", "prepend", "append", "test", "delete", "list", "show", "lst", "get", "flush",
        "restore", "showdump", "save", "help",
    ],
    "neighbor": ["add", "change", "chg", "replace", "delete", "list", "show", "lst", "flush", "get", "help"],
    "rule": ["add", "delete", "list", "show", "lst", "flush", "save", "restore", "help"],
    "netns": ["list", "show", "lst", "list-id", "help", "add", "set", "delete", "identify", "pids", "exec", "monitor", "attach"],
    "xfrm": ["add", "update", "allocspi", "delete", "deleteall", "delall", "list", "show", "lst", "get", "flush", "count", "help"],
}  # fmt: skip
# Any other object: the commands its tools take, in a careful order.
IP_GENERIC = ["add", "change", "chg", "replace", "delete", "del", "set", "list", "show", "lst", "get", "flush", "save", "restore", "help"]
IP_READ = {"list", "show", "lst", "get", "help", "showdump", "count", "list-id", "identify", "pids", "xstats", "afstats"}
# Global options that take a value, with the shortest prefix ip reads as each.
IP_VALUE_OPTIONS = {"-netns": 2, "-family": 2, "-loops": 2, "-rcvbuf": 3}


def ip_word(word: str, names: list[str]) -> str | None:
    return next((n for n in names if n.startswith(word)), None) if word else None


def r_ip(args, protected, depth):
    # ip takes any prefix of a long option with one dash: -br is -brief.
    opts = [a[1:] if a.startswith("--") else a for a in args if a.startswith("-")]
    if any("-batch".startswith(a) and len(a) >= 2 for a in opts) or any("-force".startswith(a) and len(a) >= 3 for a in opts):
        return unseen("ip -batch runs commands from a file")
    i = 0
    every_netns = False
    while i < len(args) and args[i].startswith("-"):
        o = args[i][1:] if args[i].startswith("--") else args[i]
        every_netns = every_netns or (len(o) >= 2 and "-all".startswith(o))
        takes = any(len(o) >= n and f.startswith(o) for f, n in IP_VALUE_OPTIONS.items())
        i += 2 if takes else 1
    rest = args[i:]
    if not rest:
        return read()
    first = rest[0]
    # exec runs a command in a namespace or VRF; read conservatively, as if
    # the words could mean netns or vrf whatever ip makes of them.
    if len(rest) > 1 and ("netns".startswith(first) or "vrf".startswith(first)) and "exec".startswith(rest[1]):
        inner = rest[2:] if every_netns and "netns".startswith(first) else rest[3:]
        return program(inner, protected, depth + 1) if inner else change("ip exec without a command")
    found = ip_word(first, IP_OBJECTS)
    if found is None:
        return change(f"ip {first}")
    obj = IP_SAME.get(found, found)
    if obj == "monitor":
        return read()
    words = rest[1:]
    if obj == "xfrm":
        sub = ip_word(words[0], ["state", "policy", "monitor"]) if words else None
        if not words or sub == "monitor":
            return read()
        words = words[1:]
    if not words:
        return read()
    v = ip_word(words[0], IP_VERBS.get(obj, IP_GENERIC))
    if v is None:
        return change(f"ip {obj} {words[0]}")
    if v in IP_READ:
        return read()
    if v == "flush":
        return risky(f"ip {obj} flush removes the server's {obj} entries, which may cut it off", "flush")
    if v in {"delete", "deleteall", "delall", "del"}:
        return risky(f"ip {obj} delete removes what it names, which may cut the server off", "delete")
    if obj == "link" and v in {"set", "change", "chg"} and "down" in words:
        return risky("takes a network interface down, which may cut the server off", "down")
    return change(f"ip {obj} {v} changes the network")


PCT_READ = {"list", "config", "status", "pending", "df", "listsnapshot", "cpusets", "help", "fstrim-help", "showcmd"}
QM_AGENT_READ = re.compile(r"^(ping|info|get-.*|network-get-interfaces|exec-status)$")


def is_protected(word: str, protected: set[str]) -> bool:
    """Whether a word names the NullBill container: its id or name, also as
    `--name=ID` or with leading zeros."""
    word = word.split("=")[-1]
    return word in protected or (word.isdigit() and str(int(word)) in protected)


def guest_verbs(tool: str, args: list[str], protected: set[str], depth: int = 0) -> Verdict:
    v = verb(args)
    before = args[: args.index("--")] if "--" in args else args
    plain = [a for a in before if not a.startswith("-")]
    target = plain[1] if len(plain) > 1 else ""
    running = v == "exec" or (tool == "qm" and v == "guest" and len(plain) > 2 and plain[1] == "exec")
    if v in {"enter", "terminal", "monitor", "console", "vncproxy", "termproxy", "spiceproxy"}:
        return blocked(f"{tool} {v} is interactive; ask for one command at a time")
    if v in PCT_READ and (is_protected(target, protected) or v == "list"):
        return read()
    # The guest's id may also come after `--`, unless what follows is a command.
    words = plain[1:3] if running else [a for a in args if not a.startswith("-")][1:]
    named = [a for a in words if is_protected(a, protected)]
    if named:
        return blocked(f"touches {', '.join(named)}, the NullBill container this page and the agent run in")
    if running:
        if "--" in args:
            inner = args[args.index("--") + 1 :]
        else:
            # Without `--` the words after the guest's id are the command.
            at = 2 if v == "guest" else 1
            inner = args[args.index(plain[at]) + 1 :] if len(plain) > at else []
        return program(inner, protected, depth + 1) if inner else change(f"{tool} {v} without a command")
    if tool == "qm" and v == "guest":
        sub = plain[1] if len(plain) > 1 else ""
        if sub == "cmd":
            return read() if QM_AGENT_READ.match(plain[3] if len(plain) > 3 else "") else change("qm guest cmd")
        if sub == "exec-status":
            return read()
        if sub == "passwd":
            return blocked("qm guest passwd changes who may sign in to a guest; do that yourself")
        return change(f"qm guest {sub}")
    if tool == "qm" and v == "agent":
        return read() if QM_AGENT_READ.match(plain[2] if len(plain) > 2 else "") else change("qm agent")
    if v in PCT_READ:
        return read()
    if v in {"destroy", "rollback", "delsnapshot", "unlink", "disk"} or (v == "restore" and has(args, "--force")):
        return risky(f"{tool} {v} {target} loses the guest's data or state", target or v)
    if v in {"set", "create", "restore"}:
        found = guest_settings(tool, args, protected)
        if found.tier != READ:
            return found
    return change(f"{tool} {v} {target}".strip())


def guest_settings(tool: str, args: list[str], protected: set[str]) -> Verdict:
    """What a guest's settings let it reach: a folder of the host mounted
    into it, a script the host runs at its start, raw arguments to its
    emulator, or no user namespace."""
    for n, a in enumerate(args):
        if not a.startswith("-"):
            continue
        key = a.lstrip("-").split("=", 1)[0]
        value = a.split("=", 1)[1] if "=" in a else (args[n + 1] if n + 1 < len(args) else "")
        if re.fullmatch(r"mp\d+|rootfs", key):
            source = value.split(",", 1)[0]
            if source.startswith("/") and (
                holds_secrets(source) or is_secret(source) or protected_ids([source], protected).tier == BLOCKED
            ):
                return blocked(f"mounts {source} of the host into a guest, and it holds secrets")
            if source.startswith("/"):
                return risky(f"mounts {source} of the host into a guest, which then reaches it", "mount")
        if key == "hookscript":
            return risky(f"{tool} --hookscript has Proxmox run a script as root at each start of the guest", "hook")
        if key == "args" or key.startswith("lxc."):
            return risky(f"{tool} --{key} hands raw settings to the guest's emulator or container", "raw")
        if key == "unprivileged" and value.strip() in {"0", "false", "no"}:
            return risky("a privileged container's root is the host's root", "privileged")
    return read()


def r_pct(args, p, depth):
    return guest_verbs("pct", args, p, depth)


def r_qm(args, p, depth):
    return guest_verbs("qm", args, p, depth)


def r_lxc(name: str, args: list[str], protected: set[str], depth: int) -> Verdict:
    before = args[: args.index("--")] if "--" in args else args
    if any(
        is_protected(a, protected) or (a.startswith("-n") and not a.startswith("--") and is_protected(a[2:], protected)) for a in before
    ):
        if name in {"lxc-info", "lxc-ls", "lxc-config", "lxc-checkconfig"}:
            return read()
        return blocked("touches the NullBill container this page and the agent run in")
    if name in {"lxc-info", "lxc-ls", "lxc-config", "lxc-checkconfig", "lxc-top", "lxc-monitor"}:
        return read()
    if name == "lxc-attach":
        inner = args[args.index("--") + 1 :] if "--" in args else []
        return program(inner, protected, depth + 1) if inner else blocked("lxc-attach without a command is interactive")
    if name in {"lxc-console"}:
        return blocked(f"{name} is interactive; ask for one command at a time")
    if name in {"lxc-destroy"}:
        return risky("lxc-destroy deletes a container", "destroy")
    return change(f"runs {name}")


def r_pvesm(args, _p):
    v = verb(args)
    if v in {"status", "list", "path", "scan", "help", "export", "extractconfig"}:
        return read()
    if v == "prune-backups" or (v in {"free", "remove"} and any("pbs" in a or "backup" in a for a in args)):
        return blocked("deletes backups")
    if v in {"free", "remove"}:
        return risky(f"pvesm {v} deletes storage", v)
    return change(f"pvesm {v}")


def r_pvesh(args, protected):
    v = verb(args)
    paths = [a for a in args[args.index(v) + 1 :] if not a.startswith("-")] if v else []
    # pvesh takes the path with or without its slash, and with // or ..
    path = normalise("/" + paths[0]) if paths else ""
    if v in {"get", "ls", "usage", "help"}:
        if re.search(r"/agent/file-read|/file-restore/", path):
            return risky("reads a file inside a guest or a backup, which the page cannot check for secrets", "file")
        return read()
    if path.startswith("/access"):
        return blocked("changes who may sign in to Proxmox; do that yourself")
    if v in {"delete", "create", "set"} and re.search(r"prune|/gc\b|garbage|snapshots|/content/.*(backup|vzdump)|pbs", " ".join(args)):
        return blocked("deletes or changes backups")
    for p in protected:
        if re.search(rf"/lxc/0*{re.escape(p)}(/|$)", path):
            return blocked(f"touches {p}, the NullBill container this page and the agent run in")
    if re.search(r"/agent/(exec|file-write)\b|/execute$", path):
        return risky("pvesh runs a command or writes a file through the API, which the page cannot read ahead", "run")
    if v == "delete":
        return risky("pvesh delete removes what it names", "delete")
    return change(f"pvesh {v}")


def r_backup(args, _p):
    words = []
    for a in args:
        if a.startswith("-"):
            if words:
                break
            continue
        if not re.fullmatch(r"[a-z][a-z-]*", a) or len(words) == 3:
            break
        words.append(a)
    v = " ".join(words)
    if re.search(r"\b(forget|prune|remove|delete|destroy|garbage-collection|gc|purge|change-owner)\b", v):
        return blocked("deletes backups")
    if not v or re.search(r"\b(list|status|show|snapshot list|snapshots|version|help|task log|key show|catalog|files|benchmark)\b", v):
        return read()
    return change("changes backups")


def r_apt(args, _p):
    v = verb(args)
    if v in {
        "list",
        "policy",
        "show",
        "search",
        "changelog",
        "depends",
        "rdepends",
        "showpkg",
        "madison",
        "stats",
        "help",
        "showsrc",
        "pkgnames",
        "check",
    }:
        return read() if not has(args, "-o", "--option", "-c", "--config-file") else change(f"apt {v} with options")
    if v in {"remove", "purge", "autoremove", "autopurge"}:
        if any(LOG_PACKAGES.match(a) for a in args if not a.startswith("-")):
            return blocked("removes the programs that keep the record of what ran")
        return risky(f"apt {v} uninstalls packages", "remove")
    if has(args, "-o", "--option", "-c", "--config-file"):
        return unseen("apt with -o can run commands of its choosing")
    names = [a for a in args[args.index(v) + 1 :] if not a.startswith("-")] if v else []
    if v in {"install", "reinstall"} and any(a.endswith("-") and LOG_PACKAGES.match(a[:-1]) for a in names):
        return blocked("removes the programs that keep the record of what ran")
    if v in {"install", "reinstall"} and any(a.endswith("-") for a in names):
        return risky(f"apt {v} pkg- uninstalls packages", "remove")
    if v in {"install", "reinstall"} and any("/" in a or a.endswith(".deb") for a in names):
        return unseen("apt installs a package file, whose scripts run as root")
    if has(args, "--allow-remove-essential"):
        return risky("apt may remove essential packages", "remove")
    return change(f"apt {v}")


def r_dpkg(args, _p):
    if has(args, "-r", "-P", "--remove", "--purge"):
        if any(LOG_PACKAGES.match(a) for a in args if not a.startswith("-")):
            return blocked("removes the programs that keep the record of what ran")
        return risky("dpkg removes packages", "remove")
    if has(args, "-i", "--install", "--unpack", "--configure", "--triggers-only", "-R", "--recursive", "--set-selections"):
        return unseen("dpkg installs a package file, whose scripts run as root")
    if has(args, "-x", "-X", "--extract", "--vextract", "--force", "--root", "--admindir", "--instdir", "-b", "--build"):
        return change("dpkg writes files")
    if has(
        args,
        "-l",
        "-L",
        "-s",
        "-S",
        "-p",
        "--list",
        "--listfiles",
        "--status",
        "--search",
        "--print-avail",
        "--get-selections",
        "--print-architecture",
        "--version",
        "--audit",
        "-C",
    ):
        return read()
    return change("dpkg changes packages")


def r_smartctl(args, _p):
    return (
        change("starts a disk test or changes the disk's settings")
        if has(args, "-t", "-s", "-o", "-S", "-X", "--test", "--smart", "--offlineauto", "--saveauto", "--abort", "--set")
        else read()
    )


def r_nb_power(args, _p):
    """The host's switch for what runs on demand (nullbill/power.py)."""
    verb = next((a for a in args if not a.startswith("-")), "")
    if verb == "status":
        return read()
    if verb in ("start", "stop", "keep"):
        return change("starts or stops a workload that runs on demand, or keeps it on")
    return change("changes what the host runs on demand")


def r_nvidia(args, _p):
    return (
        change("changes the graphics card's settings or writes a file")
        if has(
            args,
            "-r",
            "-pm",
            "-pl",
            "-ac",
            "-rac",
            "-e",
            "-c",
            "-mig",
            "-f",
            "--filename",
            "--gpu-reset",
            "--persistence-mode",
            "--power-limit",
            "-lgc",
            "-rgc",
            "-lmc",
            "-rmc",
        )
        else read()
    )


def r_nvme(args, _p):
    v = verb(args)
    if re.fullmatch(
        r"list(-subsys|-ns|-ctrl)?|id-.*|smart-log|error-log|fw-log|effects-log|get-log|get-feature|show-regs|version|help|ns-descs|.*-log",
        v or "list",
    ):
        return read()
    if re.fullmatch(
        r"format|sanitize|write.*|ns-delete|delete-ns|detach-ns|create-ns|attach-ns|fw-.*|dsm|reset|subsystem-reset|security-.*|set-feature|admin-passthru|io-passthru|io-mgmt-send|.*-zone.*|copy|flush",
        v,
    ):
        return blocked(f"nvme {v} erases or rewrites a disk")
    return change(f"nvme {v}")


def r_parted(args, _p):
    plain = [a for a in args if not a.startswith("-")]
    if has(args, "-l", "--list") and not plain:
        return read()
    if plain[1:] == ["print"] or plain[1:] in (["unit", "s", "print"], ["unit", "B", "print"], ["unit", "MiB", "print"]):
        return read()
    return blocked("parted repartitions disks; erase disks on the Setup page")


def r_fdisk(args, _p):
    if has(args, "-l", "--list", "-s", "--getsz") or (not args):
        return read() if args else blocked("fdisk without -l is interactive and repartitions disks")
    return blocked("fdisk repartitions disks; erase disks on the Setup page")


def r_hdparm(args, _p):
    if any(
        a.startswith(("--security", "--trim-sector", "--write-sector", "--dco", "--make-bad-sector", "--fwdownload", "--user-master"))
        for a in args
    ):
        return blocked("hdparm erases or rewrites a disk")
    flags = {a for a in args if a.startswith("-")}
    if flags and flags <= {"-I", "-i", "-t", "-T", "-g", "-C", "--direct", "-tT", "-Tt"}:
        return read()
    return change("hdparm changes a disk's settings")


def r_cryptsetup(args, _p):
    v = verb(args)
    if v in {
        "luksFormat",
        "erase",
        "luksErase",
        "luksKillSlot",
        "luksRemoveKey",
        "reencrypt",
        "luksAddKey",
        "luksChangeKey",
        "token",
    } or has(args, "--dump-master-key", "--dump-volume-key", "--dump-volume-key-file"):
        return blocked(f"cryptsetup {v} erases a disk's keys or shows them")
    if v in {"status", "luksDump", "isLuks", "luksUUID", "benchmark", "--version", "tcryptDump"}:
        return read()
    return change(f"cryptsetup {v}")


def r_find(args, protected, depth):
    if "-delete" in args:
        return risky("find -delete removes every file it matches", "delete")
    starts = []
    for a in args:
        if a.startswith("-") or a in {"(", ")", "!"}:
            break
        starts.append(a)
    verdict = read()
    execs = [i for i, a in enumerate(args) if a in {"-exec", "-execdir", "-ok", "-okdir"}]
    for start in execs:
        start += 1
        end = next((i for i in range(start, len(args)) if args[i] in {";", "+", "\\;"}), len(args))
        inner = [a for a in args[start:end] if a != "{}"]
        if not inner:
            verdict = verdict.worse(change("find -exec"))
            continue
        name = inner[0].rsplit("/", 1)[-1]
        if name in CONTENT_READERS and any(holds_secrets(s) for s in (starts or ["."])):
            return blocked(f"find -exec {name} over {' '.join(starts) or '.'} would read files that hold secrets")
        verdict = verdict.worse(program(inner, protected, depth + 1))
    if {"-fprint", "-fprintf", "-fls", "-fprint0"} & set(args):
        verdict = verdict.worse(change("find writes a file"))
    return verdict


CURL_WRITES = (
    "-o",
    "-O",
    "-T",
    "-d",
    "-F",
    "-D",
    "-c",
    "-K",
    "--output",
    "--remote-name",
    "--remote-name-all",
    "--upload-file",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--data-ascii",
    "--form",
    "--form-string",
    "--json",
    "--config",
    "--dump-header",
    "--cookie-jar",
    "--trace",
    "--trace-ascii",
    "--stderr",
    "--libcurl",
    "--hsts",
    "--alt-svc",
    "--etag-save",
    "--output-dir",
    "--create-dirs",
)


def r_curl(args, _p):
    for a in args:
        scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", a)
        if scheme and scheme.group(1).lower() not in {"http", "https"}:
            return unseen(f"curl {scheme.group(1)}:// reaches beyond the web")
    if has(args, *CURL_WRITES):
        return change("curl sends data or writes a file")
    if any(m.upper() not in {"GET", "HEAD"} for m in values(args, "-X", "--request")):
        return change("curl sends a request that may change something")
    if has(args, "--unix-socket", "--abstract-unix-socket"):
        return change("curl talks to a local socket")
    if any(
        a.startswith("-X")
        and not a.startswith("--")
        and a[2:].upper() not in {"", "GET", "HEAD"}
        or a.startswith("--request=")
        and not a.upper().endswith(("GET", "HEAD"))
        for a in args
    ):
        return change("curl sends a request that may change something")
    return read()


WGET_WRITES = (
    "-o",
    "-a",
    "-e",
    "-i",
    "-P",
    "-x",
    "-r",
    "-m",
    "-N",
    "-c",
    "--output-file",
    "--append-output",
    "--method",
    "--post-data",
    "--post-file",
    "--body-data",
    "--body-file",
    "--execute",
    "--input-file",
    "--directory-prefix",
    "--force-directories",
    "--recursive",
    "--mirror",
    "--timestamping",
    "--continue",
    "--config",
)


def r_wget(args, _p):
    joined = " ".join(args)
    to_stdout = re.search(r"(^|\s)(-[a-zA-Z]*O\s*-|--output-document=?\s*-|--spider)(\s|$)", joined)
    flags = [a for a in args if a.startswith("-") and not re.fullmatch(r"-[a-zA-Z]*O-?", a)]
    if to_stdout and not has(flags, *WGET_WRITES):
        return read()
    return change("wget writes a file or sends data")


OPENSSL_OUTPUTS = ("-out", "-writerand", "-keyout", "-sess_out", "-keylogfile", "-msgfile", "-CAserial", "-certout", "-reqout")


def r_openssl(args, protected):
    v = verb(args)
    # openssl's options take one dash and no prefixes.
    if any(a in {"-engine", "-provider", "-provider-path", "-config", "-conf"} for a in args):
        return unseen("openssl loads a module or a config that may load one")
    verdict = read()
    for n, a in enumerate(args):
        if a in OPENSSL_OUTPUTS and n + 1 < len(args):
            verdict = verdict.worse(writes_file(args[n + 1], protected))
    if verdict.tier == BLOCKED:
        return verdict
    if (
        v in {"x509", "s_client", "verify", "version", "crl", "asn1parse", "ciphers", "req", "dgst"}
        and verdict.tier == READ
        and not any(a in {"-signkey", "-new", "-newkey", "-x509", "-trace", "-CAcreateserial"} for a in args)
    ):
        return read()
    return verdict.worse(change(f"openssl {v}"))


def r_rm(args, _p):
    paths = [a for a in args if not a.startswith("-")]
    recursive = has(args, "-r", "-R", "--recursive")
    # A folder right in /tmp or /var/tmp is scratch, not a system directory:
    # one plain name, so no expansion, `..` or link inside it reaches out.
    scratch = [p for p in paths if re.fullmatch(r"/(var/)?tmp/[A-Za-z0-9_-][A-Za-z0-9._-]*", p) and p.rsplit("/", 1)[1] not in {".", ".."}]
    shallow = [
        p
        for p in paths
        if p not in scratch
        and ((normalise(p).rstrip("/").count("/") <= 2 and normalise(p).startswith("/")) or p in {"/", "/*", "*", ".", ".."})
    ]
    if recursive and shallow:
        return blocked(f"rm -r {' '.join(shallow)} would take out a whole system directory")
    if any("pbs" in p or "vzdump" in p or "/dump/" in p for p in paths):
        return blocked("deletes backups")
    if recursive and paths and len(scratch) == len(paths):
        return change("rm -r deletes a scratch folder")
    if recursive:
        return risky("rm -r deletes whole directories", "delete")
    return change("rm deletes files")


def r_lvm_remove(args, _p):
    if any("pbs" in a for a in args):
        return blocked("removes the backup disk")
    return risky("removes an LVM volume and its data", "remove")


def r_zpool(args, _p):
    v = verb(args)
    if v in {"status", "list", "iostat", "get", "history", "events", "version", "help"}:
        return read()
    if v in {"destroy", "labelclear", "create", "add", "attach", "replace", "initialize", "split"}:
        return blocked(f"zpool {v} erases or takes over disks")
    return change(f"zpool {v}")


def r_zfs(args, _p):
    v = verb(args)
    if v in {"list", "get", "version", "holds", "help"}:
        return read()
    if v == "destroy":
        return risky("zfs destroy deletes a dataset", "destroy")
    return change(f"zfs {v}")


def r_nft(args, _p):
    args = [w for a in args for w in (a.split() if not a.startswith("-") else [a])]
    v = verb(args)
    if has(args, "-f", "--file", "-i", "--interactive"):
        return change("nft reads rules from a file") if not has(args, "-c", "--check") else read()
    if v in {"list", "monitor"} or has(args, "-c", "--check"):
        return read()
    if v == "flush":
        return risky("nft flush removes firewall rules", "flush")
    return change("nft changes firewall rules")


def r_iptables(args, _p):
    if has(args, "-F", "--flush", "-X", "-P", "--policy"):
        return risky("iptables removes firewall rules", "flush")
    if has(
        args,
        "-A",
        "-D",
        "-I",
        "-R",
        "-N",
        "-E",
        "-Z",
        "--append",
        "--delete",
        "--insert",
        "--replace",
        "--new-chain",
        "--rename-chain",
        "--zero",
    ):
        return change("iptables changes firewall rules")
    if has(args, "-L", "-S", "--list", "--list-rules", "-C", "--check"):
        return read()
    return change("iptables changes firewall rules")


def r_pve_firewall(args, _p):
    v = verb(args)
    if v in {"status", "compile", "simulate", "localnet", "help"}:
        return read()
    if v == "stop":
        return risky("pve-firewall stop opens every guest and the host", "stop")
    return change(f"pve-firewall {v}")


def r_tailscale(args, _p):
    v = verb(args)
    if v in {"status", "ip", "netcheck", "version", "ping", "whois", "dns", "exit-node"}:
        return read()
    if v == "lock":
        return read() if "status" in args else change("tailscale lock")
    if v in {"logout", "down"}:
        return risky(f"tailscale {v} cuts the server off your tailnet", v)
    return change(f"tailscale {v}")


def r_auditctl(args, _p):
    flags = [a for a in args if a.startswith("-")]
    if flags and set(flags) <= {"-l", "-s", "--list", "--status"}:
        return read()
    return blocked("changes what auditd records")


LOG_PROGRAMS = ("/usr/lib/systemd/systemd-journald", "/sbin/auditd", "/usr/sbin/rsyslogd", "/usr/local/sbin/nb-log-shipper")


def r_kill(args, _p):
    targets = args[args.index("--") + 1 :] if "--" in args else [a for a in args if not a.startswith("-")]
    if any(a in {"1", "-1", "init", "systemd"} for a in targets) or any(LOG_PROCESSES.search(a) for a in args):
        return blocked("ends init or the programs that keep the record of what ran")
    for pattern in targets:
        if pattern.isdigit():
            continue
        for name in LOG_PROGRAMS:
            try:
                hit = re.search(pattern, name) or fnmatch.fnmatchcase(name.rsplit("/", 1)[1], pattern)
            except re.error:
                return change("ends processes by a pattern the page cannot read")
            if hit:
                return blocked("ends the programs that keep the record of what ran")
    return change("ends processes")


def r_badblocks(args, _p):
    if has(args, "-w"):
        return blocked("badblocks -w erases the disk")
    return change("badblocks -n rewrites the disk in place") if has(args, "-n") else read()


def r_dd(args, protected):
    out = next((a[3:] for a in args if a.startswith("of=")), "")
    if out:
        verdict = writes_file(out, protected)
        if verdict.tier == BLOCKED or out in {"/dev/null", "/dev/stdout"}:
            return verdict if verdict.tier == BLOCKED else read()
        return verdict.worse(change(f"dd writes {out}"))
    return read()


def r_pveam(args, _p):
    return read() if verb(args) in {"list", "available", "help"} else change(f"pveam {verb(args)}")


def r_pvecm(args, _p):
    return (
        read()
        if verb(args) in {"status", "nodes", "help", "qdevice"} and "remove" not in args
        else risky(f"pvecm {verb(args)} changes the cluster", "cluster")
    )


def r_tee(args, protected):
    files = [a for a in args if not a.startswith("-")]
    verdict = read("prints its input")
    for f in files:
        verdict = verdict.worse(writes_file(f, protected))
    return verdict


def r_copy(args, protected):
    """cp, mv, install, ln, rsync: the destination is written."""
    if has(args, "-e", "--rsh", "--rsync-path"):
        return unseen("rsync -e or --rsync-path runs a command of its own")
    plain = [a for a in args if not a.startswith("-")]
    target = next((a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")), "")
    if not target and "-t" in args and args.index("-t") + 1 < len(args):
        target = args[args.index("-t") + 1]
    target = target or (plain[-1] if plain else "")
    verdict = writes_file(target, protected) if target else change("copies files")
    if has(args, "-r", "-R", "-a", "--recursive", "--archive") and any(holds_secrets(p) for p in plain[:-1] if p.startswith("/")):
        return blocked("copies a tree that holds secrets")
    return verdict


def r_chmod(args, protected):
    return r_paths([a for a in args if not a.startswith("-")][1:], "changes file permissions", protected)


def r_touch(args, protected):
    return r_paths([a for a in args if not a.startswith("-")], "makes or touches files", protected)


def r_paths(paths, why, protected):
    for p in paths:
        verdict = writes_file(p, protected)
        if verdict.tier == BLOCKED:
            return verdict
    return change(why)


def r_truncate(args, protected):
    files = [
        a for a, prev in zip(args, ["", *args], strict=False) if not a.startswith("-") and prev not in {"-s", "--size", "-r", "--reference"}
    ]
    verdict = risky("truncate empties files", "truncate")
    for f in files:
        verdict = verdict.worse(writes_file(f, protected))
    return verdict


def r_vzdump(args, _p):
    if has(args, "--prune-backups") or any(
        (a == "--remove" and args[n + 1 : n + 2] != ["0"]) or (a.startswith("--remove=") and a != "--remove=0") for n, a in enumerate(args)
    ):
        return blocked("deletes old backups")
    return change("makes a backup")


def r_sort(args, protected):
    if has(args, "--compress-program"):
        return unseen("sort --compress-program runs a program of its choosing")
    verdict = read()
    for out in values(args, "-o", "--output"):
        verdict = verdict.worse(writes_file(out, protected))
    return verdict


def r_uniq(args, protected):
    """uniq [INPUT [OUTPUT]]: `-` is the standard input, so the output is
    the second operand either way."""
    found = operands(args, {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})
    return writes_file(found[1], protected) if len(found) > 1 and found[1] != "-" else read()


XXD_VALUES = {"-c", "-cols", "-g", "-groupsize", "-l", "-len", "-s", "-seek", "-o", "-offset", "-n", "-name"}


def r_xxd(args, protected):
    """xxd [INFILE [OUTFILE]]: with -r it writes bytes, without it a dump;
    either way OUTFILE is written."""
    found = operands(args, XXD_VALUES)
    return writes_file(found[1], protected) if len(found) > 1 and found[1] != "-" else read()


# git settings that only change how git prints. Any other -c may name a
# command git runs (core.pager, core.sshCommand, core.editor, diff.external,
# core.fsmonitor, alias.*, a filter or a credential helper).
GIT_SAFE_CONFIG = re.compile(
    r"^(color\..*|core\.quotepath|safe\.directory|log\..*|advice\..*|column\.ui|status\.[a-z]+|i18n\..*"
    r"|diff\.(renames|noprefix|mnemonicprefix|relative|context|interhunkcontext|algorithm))$",
    re.IGNORECASE,
)
GIT_READ = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe", "ls-files", "blame", "shortlog"}
GIT_VALUE_OPTIONS = {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--attr-source", "--list-cmds"}
GIT_BRANCH_VALUES = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--format", "--sort", "--abbrev"}


def r_git(args, _p):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-c" or (a.startswith("-c") and not a.startswith("--")):
            setting = a[2:] or (args[i + 1] if i + 1 < len(args) else "")
            key = setting.split("=", 1)[0]
            if not GIT_SAFE_CONFIG.match(key):
                return unseen(f"git -c {key} may name a command git runs")
            i += 1 if a[2:] else 2
            continue
        if long_name(a, "--config-env"):
            return unseen("git --config-env takes a setting from the environment, which may name a command")
        if long_name(a, "--exec-path") and "=" in a:
            return unseen("git --exec-path= runs git's commands from another folder")
        i += 2 if a in GIT_VALUE_OPTIONS else 1
    rest = args[i:]
    v = rest[0] if rest else ""
    sub = rest[1:]
    if v not in GIT_READ:
        return change(f"git {v}".strip())
    if has(sub, "--output", "--ext-diff", "--exec-path"):
        return change(f"git {v} writes a file or runs a program with these options")
    if v == "branch":
        if has(
            sub, "-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "-t", "--delete", "--move", "--copy", "--force",
            "--set-upstream-to", "--unset-upstream", "--edit-description", "--track", "--no-track", "--create-reflog",
        ):  # fmt: skip
            return change("git branch makes, moves or deletes a branch")
        if operands(sub, GIT_BRANCH_VALUES) and not has(sub, "-l", "--list"):
            return change("git branch makes a branch")
    if v == "remote" and next((a for a in sub if not a.startswith("-")), "") not in {"", "show", "get-url"}:
        return change("git remote changes the remotes")
    return read()


# tar options that run a program, and those that take a value.
TAR_RUNS = (
    "--to-command",
    "--use-compress-program",
    "--checkpoint-action",
    "--info-script",
    "--new-volume-script",
    "--rsh-command",
    "--rmt-command",
    "-I",
    "-F",
)
TAR_VALUES = {
    "-f", "-C", "-T", "-X", "-b", "-H", "-K", "-L", "-N", "-V", "-g", "--file", "--directory", "--files-from", "--exclude-from",
    "--blocking-factor", "--format", "--starting-file", "--tape-length", "--newer", "--label", "--listed-incremental",
    "--exclude", "--transform", "--owner", "--group", "--mode", "--mtime",
}  # fmt: skip


def packs_secrets(paths: list[str], protected: set[str]) -> str:
    """The first of paths that holds secrets or the NullBill container."""
    return next((a for a in paths if holds_secrets(a) or is_secret(a) or protected_ids([a], protected).tier == BLOCKED), "")


def r_tar(args, protected):
    old = args[0] if args and re.fullmatch(r"[A-Za-z]+", args[0]) else ""
    if has(args, *TAR_RUNS) or any(c in old for c in "IF"):
        return unseen("tar runs a program it is given (--to-command, -I, --use-compress-program, --checkpoint-action, -F)")
    reading = any(c in old for c in "tx") or has(args, "-t", "-x", "--list", "--extract", "--get", "-d", "--diff", "--compare")
    found = "" if reading else packs_secrets(operands(args[1:] if old else args, TAR_VALUES), protected)
    if found:
        return blocked(f"tar would pack {found}, which holds secrets; pack a narrower place")
    return change("runs tar")


def r_zip(args, protected):
    if has(args, "-TT", "--unzip-command"):
        return unseen("zip -TT runs a command of its choosing")
    found = packs_secrets(operands(args, {"-b", "-n", "-t", "-tt", "-x", "-i", "-P", "-O", "--password"}), protected)
    if found:
        return blocked(f"zip would pack {found}, which holds secrets; pack a narrower place")
    return change("runs zip")


PS_VALUES = {"-C", "-G", "-g", "-p", "-q", "-s", "-t", "-U", "-u", "-o", "-O", "-k", "--format", "--pid", "--ppid", "--sort"}
PS_VALUES |= {"--user", "--group", "--sid", "--tty", "--cols", "--columns", "--rows", "--width", "--quick-pid"}


def r_ps(args, _p):
    """ps e and the environ field show each process's environment, where
    the agent CLI's own credential lives."""
    value = False
    for a in args:
        if "environ" in a.lower():
            return blocked("ps would show each process's environment, which holds secrets")
        if value:
            value = False
            continue
        if a in PS_VALUES:
            value = True
        elif re.fullmatch(r"[A-Za-z]+", a):
            if "e" in a:
                return blocked("ps e shows each process's environment, which holds secrets")
            value = a[-1] in "oOkpqstUuGgC"
        elif re.fullmatch(r"-[A-Za-z]+", a):
            value = a[-1] in "oOkpqstUuGgC"
    return read()


def r_wg(args, _p):
    v = verb(args)
    if v == "showconf":
        return blocked("wg showconf prints the tunnel's private key")
    if v in {"", "show"}:
        if {"dump", "private-key", "preshared-keys"} & set(args):
            return blocked("prints the tunnel's keys")
        return read()
    if v in {"genkey", "genpsk", "pubkey", "--version", "--help", "help"}:
        return read()
    return change(f"wg {v} changes a tunnel")


def r_pdbedit(args, _p):
    flags = {a for a in args if a.startswith("-")}
    if flags and flags <= {"-L", "--list", "-v", "--verbose", "-u", "--user", "-d", "--debuglevel"}:
        return read()
    return blocked("pdbedit shows or changes Samba's passwords; do that yourself")


def r_man(args, _p):
    if has(args, "-P", "--pager", "-H", "--html", "-X", "--gxditview", "-p", "--preprocessor"):
        return unseen("man runs the program it is given")
    return change("runs man")


# Programs that run the code of the folder or package they are given.
CODE_RUNNERS = {"make", "gmake", "ninja", "npm", "npx", "yarn", "pnpm", "pip", "pip3", "pipx", "cargo", "gem", "bundle", "bundler"}


def pveum_lists(args: list[str]) -> bool:
    plain = [a for a in args if not a.startswith("-")]
    return (
        bool(plain)
        and (plain[-1] in {"list", "permissions"} or plain[:2] in (["user", "list"], ["user", "permissions"]))
        and all(
            w in {"user", "group", "role", "acl", "pool", "realm", "tfa", "token", "list", "permissions"} or not w.isalpha() for w in plain
        )
    )


# Another process's view of the file system: it may be the NullBill
# container's, whose id the path does not say.
PROCESS_ROOT = re.compile(r"/proc/[^/\s]+/(task/[^/\s]+/)?(root|cwd|map_files)(/|$)")


def protected_ids(tokens: list[str], protected: set[str]) -> Verdict:
    """The NullBill container by every spelling of its files: its config,
    its root file system while it runs, its volumes and its backups."""
    for token in tokens:
        if token.startswith(("/nodes/", "nodes/")):
            continue  # pvesh's API paths, which r_pvesh reads
        path = normalise(token.split("=", 1)[-1])
        if PROCESS_ROOT.search(path):
            return blocked(f"touches {token}, another process's files, which may be the NullBill container's")
        for p in protected:
            if not p.isdigit():
                continue
            if re.search(rf"/lxc/0*{p}(\.conf|/|$)|(subvol|vm|base)-{p}-disk|vzdump-(lxc|qemu)-{p}-|/(images|private)/{p}(/|$)", path):
                return blocked(f"touches {token}, the NullBill container this page and the agent run in")
    return read()


DEEP_RULES = {"ip", "pct", "qm", "find"}
RULES = {
    "dmesg": r_dmesg,
    "journalctl": r_journalctl,
    "systemctl": r_systemctl,
    "ip": r_ip,
    "pct": r_pct,
    "qm": r_qm,
    "pvesm": r_pvesm,
    "pvesh": r_pvesh,
    "proxmox-backup-client": r_backup,
    "proxmox-backup-manager": r_backup,
    "proxmox-backup-debug": r_backup,
    "apt": r_apt,
    "apt-get": r_apt,
    "apt-cache": r_apt,
    "dpkg": r_dpkg,
    "smartctl": r_smartctl,
    "nvidia-smi": r_nvidia,
    "nb-power": r_nb_power,
    "nvme": r_nvme,
    "parted": r_parted,
    "fdisk": r_fdisk,
    "hdparm": r_hdparm,
    "cryptsetup": r_cryptsetup,
    "find": r_find,
    "curl": r_curl,
    "wget": r_wget,
    "openssl": r_openssl,
    "rm": r_rm,
    "rmdir": lambda a, p: change("removes an empty directory"),
    "lvremove": r_lvm_remove,
    "vgremove": r_lvm_remove,
    "pvremove": r_lvm_remove,
    "lvreduce": r_lvm_remove,
    "zpool": r_zpool,
    "zfs": r_zfs,
    "nft": r_nft,
    "iptables": r_iptables,
    "ip6tables": r_iptables,
    "pve-firewall": r_pve_firewall,
    "tailscale": r_tailscale,
    "auditctl": r_auditctl,
    "dd": r_dd,
    "pveam": r_pveam,
    "pvecm": r_pvecm,
    "tee": r_tee,
    "sed": r_sed,
    "cp": r_copy,
    "mv": r_copy,
    "install": r_copy,
    "ln": r_copy,
    "rsync": r_copy,
    "chmod": r_chmod,
    "chown": r_chmod,
    "chgrp": r_chmod,
    "chattr": r_chmod,
    "setfacl": r_chmod,
    "touch": r_touch,
    "mkdir": r_touch,
    "badblocks": r_badblocks,
    "truncate": r_truncate,
    "vzdump": r_vzdump,
    "kill": r_kill,
    "pkill": r_kill,
    "killall": r_kill,
    "logger": lambda a, p: change("writes to the journal"),
    "sort": r_sort,
    "uniq": r_uniq,
    "xxd": r_xxd,
    "git": r_git,
    "tar": r_tar,
    "gtar": r_tar,
    "bsdtar": r_tar,
    "zip": r_zip,
    "ps": r_ps,
    "wg": r_wg,
    "pdbedit": r_pdbedit,
    "man": r_man,
}


def summarize_choices(before: dict, after: dict) -> list[str]:
    """What a choices change does, line by line, for the owner's card."""
    out = []
    added = sorted(set(after.get("workloads", [])) - set(before.get("workloads", [])))
    removed = sorted(set(before.get("workloads", [])) - set(after.get("workloads", [])))
    if added:
        out.append(f"Add {', '.join(added)}")
    if removed:
        out.append(f"Remove {', '.join(removed)} (setup stops before deleting their guests and asks you)")
    for key, label in (("backups", "Nightly backups"), ("console", "On-site screen")):
        if key in after and bool(after[key]) != bool(before.get(key)):
            out.append(f"{label}: {'on' if after[key] else 'off'}")
    if after.get("owner_login") and after.get("owner_login") != before.get("owner_login"):
        out.append(f"Tailscale login: {after['owner_login']}")
    for pci, gb in (after.get("gpus") or {}).items():
        out.append(f"Graphics card {pci}: {gb} GB")
    return out or ["No change"]
