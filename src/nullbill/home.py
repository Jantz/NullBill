"""Locate $NULLBILL_HOME and refuse unsafe places to run."""

import os
from pathlib import Path

from nullbill.errors import NullBillError

REPO_ROOT = Path(__file__).resolve().parents[2]


def refuse_on_pve_host() -> None:
    # NullBill runs from a controller over SSH, never on the host it manages.
    if Path("/etc/pve").exists():
        raise NullBillError("this looks like a Proxmox VE host (/etc/pve exists); run nullbill from a controller")


def nullbill_home() -> Path:
    raw = os.environ.get("NULLBILL_HOME")
    if not raw:
        raise NullBillError("NULLBILL_HOME is not set; point it at the private, backed-up directory for owner data")
    home = Path(raw).expanduser().resolve()
    if not home.is_dir():
        raise NullBillError(f"NULLBILL_HOME {home} is not a directory")
    if home == REPO_ROOT or REPO_ROOT in home.parents:
        raise NullBillError(f"NULLBILL_HOME {home} is inside the repository; owner data must live outside it")
    return home
