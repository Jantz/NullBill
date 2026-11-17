# The page against stand-ins

`tools/check` does not open the page in a browser. This folder shows it in
one, in a named state, without a server:

    python3 tools/page-demo/serve.py healthy --port 8443

It prints the address and the session cookie (`nb_session`) a browser
needs; `first` prints the setup code in its place. Needs the packages in
`requirements-dev.txt` and the repository installed (`pip install -e .`).

| State | What the page shows |
| --- | --- |
| `first` | No password yet: the sign-in with the setup code. |
| `fresh` | Signed in, nothing read. |
| `planned` | Hardware read and choices saved; nothing built. |
| `running` | The first setup run, six seconds a stage. |
| `failed` | The first setup run stopped in the forgejo stage (apt refused by the proxy). |
| `healthy` | Built, checks passed, nothing waits. |
| `degraded` | Built; alerts fire, a guest is down, a backup is late, checks failed. |
| `changed` | Built, then a choice saved: a setup run waits. |
| `unreachable` | Built; monitoring and the host do not answer. |

What stands in for what:

- `nullbill` is `fake_nullbill.py`. A setup run is the real `run_apply`
  against the stand-in host of `tests/test_apply.py` (`FakeReach`), so the
  log and every record under `$NULLBILL_HOME` are what a real run leaves.
  `demo.json` in the home sets its pace, the stage it fails in (with the
  output in `failures/`) and what the checks find.
- Prometheus and SSH to the host answer from `serve.py` (`Server`).
- The agent is `fake_agent.py`; a message line `CALL <tool> <json>` calls
  one of its tools through the page's gate.
- Secrets are plain JSON in the temporary home, which goes when the
  process ends.

`shoot.js` takes a picture of every page (needs Playwright and Chromium):

    node tools/page-demo/shoot.js <folder> <name> <cookie> [port]

`SIZES="desktop phone"`, `PAGES="overview setup/build"` and `DARK=1` narrow
or change what it takes. Pressing buttons changes a stand-in's state: start
it again before taking pictures to keep.
