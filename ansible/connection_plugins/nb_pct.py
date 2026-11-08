# community.proxmox's proxmox_pct_remote with pipelining: each module goes
# to the container on the stdin of one `pct exec` instead of four (make a
# temporary directory, copy the module, chmod, run). Each `pct exec` starts
# PVE's Perl tooling on the host anew, and on a rerun few tasks change
# anything, so the calls themselves are what a task costs.
#
# Its options and variables stay the collection's: Ansible reads them from
# the module's DOCUMENTATION, which this one takes over.
# apply points ANSIBLE_CONNECTION_PLUGINS here and sets ANSIBLE_PIPELINING
# and ANSIBLE_PARAMIKO_PTY=false: the module arrives on stdin, which a pty
# would mangle, and the containers are reached as root, so no sudo needs one.

from __future__ import annotations

from ansible_collections.community.proxmox.plugins.connection import proxmox_pct_remote as base

DOCUMENTATION = base.DOCUMENTATION


class Connection(base.Connection):
    # On when ANSIBLE_PIPELINING is: pct_remote defines no option of its own.
    has_pipelining = True
