"""The dispatcher's bookkeeping, against stand-ins for PVE and Forgejo.
The container check (ansible/roles/runner_dispatcher/tests) runs it end to
end with a real Forgejo; these cover the paths a happy job never takes."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "ansible/roles/runner_dispatcher/files/nb-dispatcher"


def load():
    loader = importlib.machinery.SourceFileLoader("nb_dispatcher", str(SCRIPT))
    spec = importlib.util.spec_from_loader("nb_dispatcher", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


nb = load()
POOL = {
    "name": "linux-runner",
    "template": 2030,
    "vmids": [6000, 6001],
    "labels": ["linux"],
    "runner_labels": ["linux:host"],
    "max_job_seconds": 600,
}


class Pve:
    def __init__(self, vms):
        self.vms = vms
        self.calls = []

    def clones(self):
        return {v: dict(vm) for v, vm in self.vms.items()}

    def clone(self, template, vmid, name, nonce):
        self.calls.append(("clone", vmid))
        self.vms[vmid] = {"vmid": vmid, "status": "running", "uptime": 0}

    def stop(self, vmid):
        self.calls.append(("stop", vmid))

    def destroy(self, vmid):
        self.calls.append(("destroy", vmid))
        del self.vms[vmid]


class Forgejo:
    url = "https://forgejo.example.internal"

    def __init__(self, jobs=(), runners=()):
        self.jobs = list(jobs)
        self.registered = list(runners)
        self.deleted = []

    def waiting(self, labels):
        return self.jobs

    def register(self, name, description):
        runner = {"id": 100 + len(self.registered), "uuid": "u", "token": "t", "name": name, "ephemeral": True}
        self.registered.append(runner)
        return runner

    def runners(self):
        return [r for r in self.registered if r["id"] not in self.deleted]

    def unregister(self, runner_id):
        self.deleted.append(runner_id)


def job(n):
    return {"id": n, "attempt": 1, "handle": f"h{n}", "name": f"job{n}", "status": "waiting"}


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(nb.time, "monotonic", lambda: now[0])
    return now


def dispatcher(pve, forgejo):
    return nb.Dispatcher({"pools": [POOL]}, pve, forgejo)


def test_one_clone_per_job_and_none_past_the_range(clock):
    pve, forgejo = Pve({}), Forgejo([job(1), job(2), job(3)])
    d = dispatcher(pve, forgejo)
    d.step()
    assert pve.calls == [("clone", 6000), ("clone", 6001)]
    d.step()  # the same jobs are still waiting: no new clones
    assert len(pve.calls) == 2 and len(forgejo.registered) == 2


def test_handoff_is_served_once(clock):
    pve, forgejo = Pve({}), Forgejo([job(1)])
    d = dispatcher(pve, forgejo)
    d.step()
    (nonce,) = d.handoffs
    assert d.take(nonce)["handle"] == "h1"
    assert d.take(nonce) is None


def test_stopped_clone_is_destroyed_once_its_job_was_fetched(clock):
    pve, forgejo = Pve({}), Forgejo([job(1)])
    d = dispatcher(pve, forgejo)
    d.step()
    pve.vms[6000]["status"] = "stopped"
    forgejo.jobs = []
    d.step()  # stopped before fetching, still young: maybe still starting
    assert ("destroy", 6000) not in pve.calls
    d.take(next(iter(d.handoffs)))
    d.step()
    assert ("destroy", 6000) in pve.calls and 100 in forgejo.deleted and not d.clones


def test_clone_that_never_fetches_is_stopped_then_destroyed_and_the_job_retried(clock):
    pve, forgejo = Pve({}), Forgejo([job(1)])
    d = dispatcher(pve, forgejo)
    d.step()
    clock[0] += nb.BOOT_GRACE_S + 1
    d.step()
    assert ("stop", 6000) in pve.calls
    pve.vms[6000]["status"] = "stopped"
    d.step()
    assert ("destroy", 6000) in pve.calls and 100 in forgejo.deleted
    assert pve.calls[-1] == ("clone", 6000)  # the job is still waiting: a fresh clone


def test_clone_past_the_time_limit_is_stopped(clock):
    pve, forgejo = Pve({6000: {"vmid": 6000, "status": "running", "uptime": 601}}), Forgejo()
    dispatcher(pve, forgejo).step()
    assert pve.calls == [("stop", 6000)]


def test_after_a_restart_stopped_clones_go_and_running_ones_finish(clock):
    vms = {
        6000: {"vmid": 6000, "status": "stopped"},
        6001: {"vmid": 6001, "status": "running", "uptime": 30},
        2017: {"vmid": 2017, "status": "stopped"},  # in the pool but outside the range: not ours
    }
    runners = [{"id": 1, "name": "nb-linux-runner-6000", "ephemeral": True}, {"id": 2, "name": "nb-linux-runner-6001", "ephemeral": True}]
    pve, forgejo = Pve(vms), Forgejo(runners=runners)
    dispatcher(pve, forgejo).step()
    assert pve.calls == [("destroy", 6000)]
    assert forgejo.deleted == [1]
