"""Closing the home network (LAN) to Proxmox's web UI and SSH, safely (#9).

Until the owner closes it, the LAN keeps PVE's default access to 8006 and
22 (firewall.py's cluster-lan-open.fw). Closing installs the cluster.fw
whose management ipset holds only the vpn guest, but first arms a timer on
the host that puts the open file back after REVERT_AFTER_S. The owner
cancels it by pressing "Keep it closed" on the page, which the page accepts
only from the vpn guest's address: the press itself shows the tailnet
reaches NullBill. A wrong policy therefore locks nobody out for longer than
the timer.

The state lives on the controller in $NULLBILL_HOME/lan.json:
open (the default), closing (the timer is armed) or closed. While it is
closing or closed, the controller reaches the host at its mgmt address,
which the closed firewall still admits, and the page refuses requests from
outside the NullBill network once it is closed.
"""

import ipaddress
import json
import time
from pathlib import Path

FILE = "lan.json"
OPEN, CLOSING, CLOSED = "open", "closing", "closed"
REVERT_UNIT = "nb-lan-revert"
REVERT_AFTER_S = 15 * 60
# The open cluster.fw, kept on the host's own disk for the timer to restore.
SAVED_OPEN = "/root/nullbill/cluster-lan-open.fw"
CLUSTER_FW = "/etc/pve/firewall/cluster.fw"


def read(home: Path) -> dict:
    path = home / FILE
    if not path.exists():
        return {"state": OPEN}
    return json.loads(path.read_text())


def write(home: Path, state: str, deadline: float | None = None) -> None:
    data = {"state": state} | ({"deadline": deadline} if deadline is not None else {})
    path = home / FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data) + "\n")
    tmp.replace(path)


def state(home: Path) -> str:
    return read(home)["state"]


def arm_command() -> str:
    """Start the timer that restores the open file; a timer left from an
    earlier close is replaced."""
    restore = f"/bin/cp {SAVED_OPEN} {CLUSTER_FW}"
    return (
        f"systemctl stop {REVERT_UNIT}.timer {REVERT_UNIT}.service 2>/dev/null; "
        f"systemctl reset-failed {REVERT_UNIT}.service 2>/dev/null; "
        f"systemd-run --quiet --unit={REVERT_UNIT} --on-active={REVERT_AFTER_S} --timer-property=AccuracySec=5s {restore}"
    )


ARMED = f"systemctl is-active --quiet {REVERT_UNIT}.timer && echo armed || echo gone"
DISARM = f"systemctl stop {REVERT_UNIT}.timer"


def deadline(now: float | None = None) -> float:
    return (now if now is not None else time.time()) + REVERT_AFTER_S


def inside(address: str, base: str) -> bool:
    """Whether a client address is in the NullBill network: the vpn guest,
    the on-site screen through the host's mgmt address, or a guest."""
    try:
        ip = ipaddress.ip_address(address.removeprefix("::ffff:"))
    except ValueError:
        return False
    return ip in ipaddress.ip_network(base)
