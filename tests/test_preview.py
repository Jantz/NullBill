"""The page's preview of a setup run (preview.py): what the run does to each
machine, worked out from the controller's record of the last runs."""

# ruff: noqa: F811 (the fixtures imported from test_apply are taken by name)

import copy
import subprocess

import pytest
from fixturedata import custom_config, fixture_config, games_config, legacy_config
from test_apply import FakeReach, ctx, passing_verify, tailnet, with_client  # noqa: F401 (fixtures)

from nullbill import applied, apply, ci, custom, preview
from nullbill.apply import ApplyError, run_apply
from nullbill.artifacts import render_all
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.web import review

CONFIGS = {
    "small": lambda: fixture_config("small"),
    "mid": lambda: fixture_config("mid"),
    "large": lambda: fixture_config("large"),
    "kept disk": legacy_config,
    "games": games_config,
    "own services": custom_config,
}


def quiet(c):
    """The context's plan as the page's preview takes it up: nothing in it reaches the server."""
    return preview.context(c.plan, c.model, c.home, c.secrets.data, c.files, c.lan_host)


def base(c, **kw):
    return preview.Baseline(c.secrets.data.get("applied_key"), applied.record(c.home), **kw)


def built(ctx, monkeypatch, config="small", script=None):
    """A server a full setup run has built, with the stand-in host."""
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "run_verify", passing_verify)
    c = with_client(ctx(config if config in ("small", "mid") else "large", config=CONFIGS[config]()))
    c.secrets.set("ntfy_topic", "nullbill-topic")
    c.secrets.set("claude_oauth_token", "sk-ant-oat01-token")
    (c.home / "web").mkdir(exist_ok=True)
    if script:
        path = c.home / custom.DIR / script / custom.SCRIPT
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\napt-get install -y paperless\n")
    run_apply(c)
    return c


def chosen(c, config, facts):
    """The same server with other choices saved: (context of the new plan, review's guest rows)."""
    model = build_model(config)
    plan = build_plan(model, facts("large"))
    was, now = strip_notes(c.plan), strip_notes(plan)
    new = preview.context(plan, model, c.home, c.secrets.data, render_all(plan, model), c.lan_host)
    return new, review.guests(now, was, set(was["guests"]), {})


def rows(c, guests=None, **kw):
    guests = review.guests(c.bare, c.bare, set(c.bare["guests"]), {}) if guests is None else guests
    found = preview.machines(quiet(c), base(c, **kw), guests)
    return found, {m["name"]: m for m in found["machines"]}


@pytest.mark.parametrize("config", CONFIGS)
def test_after_a_setup_run_the_preview_finds_every_step_as_the_run_left_it(ctx, monkeypatch, tailnet, config):
    c = built(ctx, monkeypatch, config, script="paperless" if config == "own services" else None)
    steps, unknown = preview.steps(quiet(c))
    assert unknown == {}
    record = applied.record(c.home)
    # Each playbook the run made on a machine that keeps a mark is foreseen, and no other.
    marked = {entry for entry in record if not entry.endswith("/" + applied.IMAGE)}
    assert {applied.entry(s.machine, s.playbook) for s in steps} == marked
    key = c.secrets.data["applied_key"]
    for s in steps:
        last = record[applied.entry(s.machine, s.playbook)]
        # The run's inputs are the foreseen ones and those it read itself, exactly.
        assert {applied.whole(name) for name in last["inputs"]} == set(s.inputs) | set(s.read), (s.playbook, s.machine)
        assert preview.compare(key, s, last).does == preview.SAME, (s.playbook, s.machine)
    # So the page says the next run leaves every machine alone.
    found, by_name = rows(c)
    assert found["known"] and not found["full"]
    assert found["counts"]["alone"] == len(found["machines"]) == len(c.bare["guests"]) + 1
    assert by_name[preview.HOST]["host"] and by_name[preview.HOST]["steps"]


def test_every_stage_says_which_playbooks_it_runs():
    """A new stage declares its playbook runs in apply.FORESEEN (None: it
    runs none on a machine that keeps a mark), or the page's preview would
    call its machines left alone."""
    assert list(apply.FORESEEN) == [name for name, _ in apply.STAGES]


def test_the_preview_cannot_reach_the_server_or_save_anything(ctx, monkeypatch):
    c = quiet(ctx("small"))
    before = sorted(p.relative_to(c.home) for p in c.home.rglob("*"))
    for call in (lambda: c.reach.host("pct list"), lambda: c.reach.playbook("x.yml", {}, {}), lambda: c.reach.local(["tofu"], c.home, {})):
        with pytest.raises(preview.Unforeseen):
            call()
    # A secret a run would make is not made, and nothing is written.
    assert c.secrets.ensure("postgres_forgejo", lambda: 1 / 0) == preview.MADE_BY_THE_RUN
    with pytest.raises(preview.Unforeseen):
        c.secrets.set("x", "y")
    assert sorted(p.relative_to(c.home) for p in c.home.rglob("*")) == before
    # A stage whose inputs need the server is not guessed at: it is named.
    monkeypatch.setitem(apply.FORESEEN, "router", lambda ctx: [ctx.reach.host("uname -r")])
    steps, unknown = preview.steps(c)
    assert list(unknown) == ["router"] and "uname -r" in unknown["router"]
    assert "router.yml" not in {s.playbook for s in steps} and "egress-proxy.yml" in {s.playbook for s in steps}


def test_a_changed_choice_sets_up_again_only_what_takes_it(ctx, monkeypatch, tailnet, facts):
    c = built(ctx, monkeypatch, "games")
    config = games_config()
    config["games"] = copy.deepcopy(config["games"])
    config["games"]["minecraft"]["settings"]["difficulty"] = "peaceful"
    new, guests = chosen(c, config, facts)
    found = preview.machines(new, base(c), guests)
    by_name = {m["name"]: m for m in found["machines"]}
    assert [m["name"] for m in found["machines"] if m["what"] != "alone"] == ["minecraft"]
    minecraft = by_name["minecraft"]
    assert minecraft["what"] == "again" and minecraft["why"] == ["Changed: settings (difficulty)."]
    assert minecraft["restarts"] == [{"what": "minecraft", "how": "restart"}] and minecraft["restart_note"] == "Minecraft may restart."
    # The row's few words, which the page shows before anything is opened.
    assert minecraft["effect"] == "Minecraft may restart" and minecraft["interrupts"] is True
    # How long its setup took, and how long players waited after it, the last time.
    assert minecraft["last"]["took_s"] >= 0 and minecraft["last"]["at"] > 0
    assert by_name["factorio"]["what"] == "alone" and by_name["factorio"]["restarts"] == []


def test_a_new_and_a_dropped_workload_are_created_and_deleted_and_their_neighbours_set_up_again(ctx, monkeypatch, tailnet, facts):
    c = built(ctx, monkeypatch, "games")
    config = games_config()
    workloads = [w for w in config["intent"]["workloads"] if w != "terraria"]
    config = {**config, "intent": {**config["intent"], "workloads": [*workloads, "media"]}}
    new, guests = chosen(c, config, facts)
    found = preview.machines(new, base(c), guests)
    by_name = {m["name"]: m for m in found["machines"]}
    assert by_name["jellyfin"]["what"] == "create" and by_name["jellyfin"]["why"] == [preview.CREATED]
    assert by_name["jellyfin"]["steps"] == [] and by_name["jellyfin"]["restarts"] == []
    assert by_name["jellyfin"]["effect"] == "New guest" and by_name["terraria"]["effect"] == "Deletes the guest and everything on its disk"
    assert by_name["terraria"]["interrupts"] is True and by_name["minecraft"]["effect"] == ""
    assert by_name["terraria"]["what"] == "delete" and by_name["terraria"]["why"]
    # A new zone gives the router a leg more: its interfaces change, so it may restart whole.
    router = by_name["router"]
    assert router["what"] == "again" and preview.IN_PLACE in router["why"]
    assert {"what": "the whole machine", "how": "reboot"} in router["restarts"] and router["effect"] == "The whole machine may restart"
    assert any("/etc/network/interfaces" in line for line in router["why"])
    # The proxy's allow-list changed; nothing changed for a game that stays.
    assert by_name["egress-proxy"]["restarts"] == [{"what": "squid", "how": "restart"}]
    assert by_name["minecraft"]["what"] == "alone" and by_name["factorio"]["what"] == "alone"
    assert found["counts"]["create"] == 1 and found["counts"]["delete"] == 1


def test_only_rules_changing_restarts_the_routers_services_not_the_router(ctx, monkeypatch, tailnet, facts):
    c = built(ctx, monkeypatch, "games")
    config = games_config()
    workloads = [w for w in config["intent"]["workloads"] if w != "terraria"]
    new, guests = chosen(c, {**config, "intent": {**config["intent"], "workloads": workloads}}, facts)
    by_name = {m["name"]: m for m in preview.machines(new, base(c), guests)["machines"]}
    router = by_name["router"]
    # The games zone stays, so the router keeps its legs: rules and names only.
    assert router["what"] == "again" and any("/etc/nftables.conf" in line for line in router["why"])
    assert {r["how"] for r in router["restarts"]} == {"restart"} and {"what": "nftables", "how": "restart"} in router["restarts"]
    assert by_name["monitoring"]["restarts"] == [{"what": "prometheus", "how": "restart"}]
    # The operator's list of guests changed, which restarts nothing there.
    assert by_name["operator"]["what"] == "again" and by_name["operator"]["restarts"] == []
    assert by_name["operator"]["restart_note"].startswith("Nothing in it restarts")
    assert by_name["operator"]["effect"] == "Nothing restarts" and by_name["operator"]["interrupts"] is False
    assert router["effect"] == "Nftables and 2 more may restart" and router["interrupts"] is True
    assert by_name["factorio"]["what"] == "alone"


def test_giving_a_person_a_service_more_needs_no_machine_set_up_again(ctx, monkeypatch, tailnet, facts):
    """A shared port is open from the vpn guest whoever holds it: the
    Tailscale policy alone changes, which the tailnet stage pushes."""
    c = built(ctx, monkeypatch, "games")
    config = games_config()
    people = {**config["people"], "friend-c": {**config["people"]["friend-c"], "services": ["minecraft", "terraria", "factorio"]}}
    new, guests = chosen(c, {**config, "people": people}, facts)
    found = preview.machines(new, base(c), guests)
    assert found["counts"]["alone"] == len(found["machines"])


def test_a_pools_image_is_baked_again_when_it_is_a_week_old_or_its_settings_changed(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "small")
    image = applied.record(c.home)["linux-runner/" + applied.IMAGE]
    assert image["stage"] == "ci" and image["took_s"] >= 0 and image["settings"] == apply.image_print(c, "linux-runner")
    found, by_name = rows(c)
    assert by_name["linux-runner"]["what"] == "alone" and by_name["runner-dispatcher"]["what"] == "alone"
    # A week on, with nothing else changed.
    found = preview.machines(
        quiet(c), base(c), review.guests(c.bare, c.bare, set(c.bare["guests"]), {}), at=image["baked"] + ci.REBAKE_AFTER_S
    )
    by_name = {m["name"]: m for m in found["machines"]}
    pool, dispatcher = by_name["linux-runner"], by_name["runner-dispatcher"]
    assert pool["what"] == "again" and pool["why"] == ["Its image is over 7 days old, so the run bakes it again."]
    assert pool["restart_note"] == preview.BAKES and pool["last"]["took_s"] == image["took_s"]
    assert pool["effect"] == preview.BAKES_SHORT and pool["interrupts"] is True
    assert dispatcher["effect"] == "Nb-dispatcher may restart"
    assert dispatcher["what"] == "again" and dispatcher["restarts"] == [{"what": "nb-dispatcher", "how": "restart"}]
    assert dispatcher["why"] == ["It stops taking jobs while the image of linux-runner is baked again."]
    assert found["counts"]["again"] == 2
    # Baked with other settings: at once.
    applied.note(c.home, "linux-runner", applied.IMAGE, settings="0" * 32)
    assert rows(c)[1]["linux-runner"]["why"] == ["Its image is baked with other settings now, so the run bakes it again."]
    # An image no run has written down is not called left alone.
    applied.note(c.home, "linux-runner", applied.IMAGE, baked=None)
    assert rows(c)[1]["linux-runner"]["what"] == "unknown"


def test_a_run_that_finds_the_image_baked_writes_that_down(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "small")
    first = applied.record(c.home)["linux-runner/" + applied.IMAGE]
    applied.note(c.home, "linux-runner", applied.IMAGE, baked=None, settings=None)
    run_apply(c)
    again = applied.record(c.home)["linux-runner/" + applied.IMAGE]
    # When it was baked is the host's word, not this run's time; how long the bake took stays.
    assert again["settings"] == first["settings"] and again["took_s"] == first["took_s"]
    assert abs(again["baked"] - first["baked"]) < 5


def test_failed_checks_send_the_next_run_through_every_step_and_restart_nothing(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "small")
    found, by_name = rows(c, full=True)
    # All but the pool's image, which is baked by its age and not by a mark.
    assert found["full"] and found["counts"]["again"] == len(found["machines"]) - 1 and by_name["linux-runner"]["what"] == "alone"
    forgejo = by_name["forgejo"]
    assert forgejo["why"] == [preview.ALL_AGAIN] and forgejo["restarts"] == []
    assert forgejo["restart_note"].startswith("Nothing in it restarts")


def test_a_setup_that_did_not_finish_runs_from_the_start(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "small")
    c.reach.fail_playbook = "forgejo.yml"
    c.full = True
    with pytest.raises(ApplyError):
        run_apply(c)
    last = applied.record(c.home)["forgejo/forgejo.yml"]
    # As with the mark on the guest: gone while it runs, back only when it ends well.
    assert "digest" not in last and last["took_s"] >= 0
    found, by_name = rows(c)
    forgejo = by_name["forgejo"]
    assert forgejo["what"] == "again" and forgejo["why"] == ["Its last setup did not finish, so it runs from the start."]
    # Nothing says which step it stopped at, so every program its setup can restart is named.
    assert {"what": "forgejo", "how": "restart"} in forgejo["restarts"] and "could not be followed" in forgejo["restart_note"]


def test_a_server_no_run_has_written_down_is_not_called_left_alone(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "small")
    (c.home / applied.RECORD).unlink()
    found, by_name = rows(c)
    assert found["known"] is False and found["counts"]["alone"] == 0
    assert by_name["forgejo"]["what"] == "unknown" and by_name["forgejo"]["why"] == []
    assert by_name["linux-runner"]["what"] == "unknown"


def test_a_stage_that_needs_the_server_leaves_its_machines_unknown(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "small")
    monkeypatch.setitem(apply.FORESEEN, "postgres", lambda ctx: [ctx.reach.host("cat /etc/hostname")])
    found, by_name = rows(c)
    assert by_name["postgres"]["what"] == "unknown" and "could not be worked out" in by_name["postgres"]["why"][0]
    assert by_name["forgejo"]["what"] == "alone"


def test_the_record_keeps_each_input_apart_and_says_when_a_playbook_last_ran(ctx, monkeypatch, tailnet):
    c = built(ctx, monkeypatch, "games")
    record = applied.record(c.home)
    router = record["router/router.yml"]
    # A table of files entry by entry, so the page can name the file that changed.
    assert "router_files" in router["inputs"] and "router_files[/etc/nftables.conf]" in router["inputs"]
    assert router["stage"] == "router" and router["source"] == applied.source("router.yml") and router["took_s"] >= 0
    # No secret is in it: fingerprints only.
    text = (c.home / applied.RECORD).read_text()
    assert all(value not in text for value in c.secrets.data.values())
    assert (c.home / applied.RECORD).stat().st_mode & 0o777 == 0o600
    # The game answered after its role had run: how long players waited.
    assert record["minecraft/game-minecraft.yml"]["answered_s"] >= 0
    # A run that leaves the playbook out says it saw the same inputs, and keeps when it last ran.
    ran_at = router["at"]
    applied.note(c.home, "router", "router.yml", at=ran_at - 500, seen=ran_at - 500)
    run_apply(c)
    again = applied.record(c.home)["router/router.yml"]
    assert again["at"] == ran_at - 500 and again["seen"] >= ran_at and again["took_s"] == router["took_s"]


# --- What a playbook's roles restart ------------------------------------------


class Files:
    """ansible/ as a table of files."""

    def __init__(self, files):
        self.files = files
        self.paths = sorted(files)

    def read(self, path):
        return self.files.get(path)


TASKS = """
- name: Config
  ansible.builtin.template:
    src: web.conf.j2
    dest: /etc/web.conf
  notify: Restart web
- name: Tool
  ansible.builtin.copy:
    src: nb-tool
    dest: /usr/local/bin/nb-tool
- name: Pages
  ansible.builtin.template:
    src: "{{ item }}.j2"
    dest: "/etc/{{ item }}"
  loop: [a.conf, b.conf]
  notify: Reload proxy
- name: Certificate
  ansible.builtin.copy:
    content: "{{ web_cert }}"
    dest: /etc/web.crt
  register: web_cert_file
- name: Services
  when: web_manage | bool
  block:
    - name: Take up a new certificate
      ansible.builtin.systemd_service:
        name: web
        state: reloaded
      when: web_cert_file.changed
- name: Fetch
  ansible.builtin.include_role:
    name: downloads
    tasks_from: fetch
  vars:
    downloads_url: "{{ web_url }}"
"""
TREE = {
    "site.yml": "- hosts: all\n  roles:\n    - web\n",
    "connection_plugins/nb_pct.py": "# how Ansible gets there\n",
    "roles/web/tasks/main.yml": TASKS,
    "roles/web/handlers/main.yml": (
        "- name: Restart web\n  ansible.builtin.systemd_service: {name: web, state: restarted}\n"
        "- name: Reload proxy\n  ansible.builtin.systemd_service: {name: nginx, state: reloaded}\n"
    ),
    "roles/web/defaults/main.yml": (
        'web_port: 80\nweb_listen: "0.0.0.0:{{ web_port }}"\nweb_version: "1"\nweb_url: "https://example.internal/{{ web_version }}"\n'
    ),
    "roles/web/templates/web.conf.j2": "listen {{ web_listen }}\n{% include 'common.j2' %}\n",
    "roles/web/templates/common.j2": "name {{ web_name }}\n",
    "roles/web/templates/a.conf.j2": "a\n",
    "roles/web/templates/b.conf.j2": "b\n",
    "roles/web/files/nb-tool": "#!/bin/sh\n",
    "roles/web/files/rules.test.yml": "tests: []\n",
    "roles/web/tests/check.sh": "#!/bin/sh\n",
    "roles/downloads/tasks/fetch.yml": (
        '- name: Download\n  ansible.builtin.get_url:\n    url: "{{ downloads_url }}"\n    dest: /opt/x\n  register: downloads_fetched\n'
        "- name: Start what was fetched\n  ansible.builtin.command: systemctl restart fetched\n  when: downloads_fetched.changed\n"
    ),
    "roles/other/tasks/main.yml": "- name: Elsewhere\n  ansible.builtin.reboot:\n",
}
WEB, PROXY, TOOK, FETCHED = ("web", "restart"), ("nginx", "reload"), ("web", "reload"), ("fetched", "restart")


def restarts(inputs=(), changed=(), tree=None, was=None):
    found = preview.effects(Files(tree or TREE), "site.yml", set(inputs), None if changed is None else set(changed), was and Files(was))
    return [(r.what, r.how) for r in found.restarts], found.followed


def test_a_playbook_reaches_its_roles_files_but_not_their_checks():
    roles, files = preview.reach(Files(TREE), "site.yml")
    assert sorted(roles) == ["downloads", "web"]
    assert "roles/web/tests/check.sh" not in files and "roles/other/tasks/main.yml" not in files
    assert {"site.yml", "connection_plugins/nb_pct.py", "roles/downloads/tasks/fetch.yml"} <= set(files)


def test_the_preview_reads_the_same_sources_apply_digests():
    checkout = preview.Checkout(applied.ANSIBLE.parent)
    for playbook in sorted(p.name for p in applied.ANSIBLE.glob("*.yml")):
        want = sorted(str(p.relative_to(applied.ANSIBLE)) for p in applied.sources(playbook))
        assert sorted(preview.reach(checkout, playbook)[1]) == want, playbook


def test_a_script_one_role_takes_from_another_is_among_its_sources():
    """host_baseline places agent_ssh's nb-agent-command: a change to it must run the host's playbook again."""
    names = {str(p.relative_to(applied.ANSIBLE)) for p in applied.sources("host-baseline.yml")}
    assert "roles/agent_ssh/files/nb-agent-command" in names


@pytest.mark.parametrize(
    ("inputs", "changed", "want"),
    [
        # An input a task places and registers: the task that asks whether it changed.
        (["web_cert"], [], [TOOK]),
        # Through a default into a template, and through a template another includes.
        (["web_port"], [], [WEB]),
        (["web_name"], [], [WEB]),
        # Through a default into what an included role is given, to what it registers.
        (["web_version"], [], [FETCHED]),
        # An input nothing reads, and a script no task restarts anything for.
        (["web_unused"], [], []),
        ([], ["roles/web/files/nb-tool"], []),
        # A template named through a loop written out.
        ([], ["roles/web/templates/a.conf.j2"], [PROXY]),
        ([], ["roles/web/templates/common.j2"], [WEB]),
        # A file no task places, a handler, and how Ansible gets there: nothing runs differently.
        ([], ["roles/web/files/rules.test.yml"], []),
        ([], ["roles/web/handlers/main.yml"], []),
        ([], ["connection_plugins/nb_pct.py"], []),
        # A role the playbook does not reach.
        ([], ["roles/other/tasks/main.yml"], []),
    ],
)
def test_what_changed_is_followed_to_what_it_restarts(inputs, changed, want):
    assert restarts(inputs, changed) == (want, True)


def test_a_change_that_cannot_be_placed_names_every_restart():
    everything = [FETCHED, WEB, PROXY, TOOK]
    # The playbook itself, or sources that differ with nothing to say where.
    assert restarts(changed=["site.yml"]) == (everything, False)
    assert restarts(changed=None) == (everything, False)
    assert restarts(changed=["roles/web/meta/main.yml"], tree=TREE | {"roles/web/meta/main.yml": "dependencies: []\n"}) == (
        everything,
        False,
    )
    # What the run reads from the machine itself.
    assert restarts(inputs=[applied.FACTS]) == (everything, False)
    # A tasks file with no earlier version to compare: all of its tasks, and what they include.
    assert restarts(changed=["roles/web/tasks/main.yml"]) == (everything, True)
    # Defaults likewise: every one of them.
    assert restarts(changed=["roles/web/defaults/main.yml"]) == ([FETCHED, WEB], True)


def test_with_the_last_runs_version_only_the_changed_task_or_default_counts():
    older = TREE | {
        "roles/web/tasks/main.yml": TASKS.replace("dest: /usr/local/bin/nb-tool", "dest: /usr/local/sbin/nb-tool"),
        "roles/web/defaults/main.yml": TREE["roles/web/defaults/main.yml"].replace("web_port: 80", "web_port: 8080"),
    }
    assert restarts(changed=["roles/web/tasks/main.yml"], was=older) == ([], True)
    assert restarts(changed=["roles/web/defaults/main.yml"], was=older) == ([WEB], True)


def test_a_file_nothing_names_counts_where_a_task_builds_names():
    built_name = TASKS + '- name: Kind\n  ansible.builtin.copy:\n    src: "{{ web_kind }}.conf"\n    dest: /etc/kind.conf\n'
    tree = TREE | {"roles/web/tasks/main.yml": built_name}
    found, followed = restarts(changed=["roles/web/files/rules.test.yml"], tree=tree)
    assert followed and WEB in found and PROXY in found and TOOK in found


def test_a_restart_for_matching_entries_only_is_spared_when_none_of_them_changed():
    tasks = """
- name: Rendered files
  ansible.builtin.copy:
    dest: "{{ item.key }}"
    content: "{{ item.value }}"
  loop: "{{ web_files | dict2items }}"
  register: web_placed
- name: Reboot into the new interfaces
  ansible.builtin.reboot:
  when: >-
    web_placed.results | selectattr('changed') | map(attribute='item.key')
    | select('match', '^/etc/network/') | list | length > 0
- name: Restart after a change
  ansible.builtin.systemd_service:
    name: "{{ item }}"
    state: restarted
  loop: [web, web-helper]
  when: web_placed.changed
"""
    tree = TREE | {"roles/web/tasks/main.yml": tasks}
    services = [("web", "restart"), ("web-helper", "restart")]
    assert restarts(["web_files", "web_files[/etc/web.conf]"], tree=tree) == (services, True)
    assert restarts(["web_files", "web_files[/etc/network/interfaces]"], tree=tree) == ([("the whole machine", "reboot"), *services], True)
    # Without the entries that changed, nothing rules the reboot out.
    assert restarts(["web_files"], tree=tree)[0][0] == ("the whole machine", "reboot")


def test_the_real_roles_restart_what_their_tasks_say():
    checkout = preview.Checkout(applied.ANSIBLE.parent)

    def real(playbook, inputs=(), changed=()):
        return [(r.what, r.how) for r in preview.effects(checkout, playbook, set(inputs), set(changed)).restarts]

    assert real("monitoring.yml", changed=["roles/monitoring/files/nullbill.rules.yml"]) == [("prometheus", "restart")]
    assert real("monitoring.yml", inputs=["monitoring_ntfy_url"]) == [("nb-alert-relay", "restart")]
    assert real("monitoring.yml", inputs=["monitoring_browser_cert"]) == [("nginx", "reload")]
    assert real("game-minecraft.yml", inputs=["game_minecraft_settings"]) == [("minecraft", "restart")]
    assert real("game-minecraft.yml", changed=["roles/game_server/files/nb-game"]) == []
    assert real("egress-proxy.yml", inputs=["egress_proxy_files"]) == [("squid", "restart")]
    assert real("files.yml", inputs=["files_shares"]) == [("smbd", "restart")]
    assert ("the whole machine", "reboot") not in real("router.yml", inputs=["router_files", "router_files[/etc/nftables.conf]"])
    assert ("the whole machine", "reboot") in real("router.yml", inputs=["router_files", "router_files[/etc/network/interfaces]"])
    # No role names a file in a way the preview cannot read; one that starts to is treated with care (Task.built).
    for playbook in sorted(p.name for p in applied.ANSIBLE.glob("*.yml")):
        assert [t.name for t in preview.read_roles(checkout, playbook).tasks if t.built] == [], playbook


def test_restarts_are_said_in_one_sentence():
    note = preview.restart_note
    assert note(preview.Effects([]), False) == ""
    assert note(preview.Effects([]), True).startswith("Nothing in it restarts")
    found = preview.Effects(
        [preview.Restart("the whole machine", "reboot"), preview.Restart("a", "restart"), preview.Restart("nginx", "reload")]
    )
    assert note(found, True) == "The whole machine may restart; a may restart; nginx may reload its settings, which interrupts nothing."
    assert "names every one" in note(preview.Effects([preview.Restart("a", "restart")], followed=False), True)


# --- A waiting update ---------------------------------------------------------


def git(root, *args):
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def repo(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "owner@example.internal")
    git(root, "config", "user.name", "Owner")
    return root, commit(root, files, "first")


def commit(root, files, subject):
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", subject)
    return git(root, "rev-parse", "HEAD")


def test_a_waiting_update_says_which_machines_its_files_set_up_again(tmp_path):
    root, first = repo(tmp_path, {f"ansible/{path}": text for path, text in TREE.items()} | {"src/nullbill/plan.py": "A = 1\n"})
    ran = [("web", "site.yml"), ("mail", "mail.yml"), (None, "site.yml")]
    record = {"web/site.yml": {"took_s": 40.0, "at": 1000, "digest": "d"}}
    port = TREE["roles/web/defaults/main.yml"].replace("web_port: 80", "web_port: 8080")
    second = commit(root, {"ansible/roles/web/defaults/main.yml": port, "src/nullbill/web/static/app.js": "// page\n"}, "second")
    found = preview.update(root, first, second, ran, record)
    by_name = {m["name"]: m for m in found["machines"]}
    web = by_name["web"]
    assert web["what"] == "again" and web["why"] == ["The update sets it up differently: 1 file of web changed."]
    assert web["restarts"] == [{"what": "web", "how": "restart"}] and web["last"]["took_s"] == 40.0
    assert by_name[preview.HOST]["what"] == "again" and by_name[preview.HOST]["last"] is None
    assert by_name["mail"]["what"] == "alone"
    # The machines it sets up again come first; the page's own files change nothing on the server.
    assert [m["what"] for m in found["machines"]] == ["again", "again", "alone"] and found["code"] is False
    assert found["counts"] == {"again": 2, "alone": 1}
    # A change to the code that plans and applies cannot be weighed from files alone: said so.
    third = commit(root, {"src/nullbill/plan.py": "A = 2\n"}, "third")
    after = preview.update(root, second, third, ran, record)
    assert after["code"] is True and {m["what"] for m in after["machines"]} == {"alone"}
    # A commit this clone does not hold.
    assert preview.update(root, first, "0" * 40, ran, record) is None
