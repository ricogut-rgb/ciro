# CIRO

CIRO is the zero point — a substrate for everyday thinking that runs on your own machine.

The name comes from an older personal lineage; it is carried out here as the
public name for one plate, no funnel, nothing centralized.

This repository is one admitted slice of that work: the HTTP service that
renders a single-operator console. Not the whole thing. The deeper mesh — the
build posture, the cross-session memory, the code-review and audit postures —
is held in a private workbench and admitted here one slice at a time, under a
fenced-banyan grammar: interconnection real, fences are value, admission
one-way layered. Opening a gate here does not open any other gate.

---

## What this slice is

A small HTTP server that runs locally and serves a browser console. The
console shows the operator what is on their plate (open items as cards),
takes a reply, routes it through a confirm gate, and folds the result back
into an append-only record.

- **One service.** `python -m agnostic_runtime.trigger.server`
- **Bearer-token auth.** No token configured → 503. Never runs open.
- **Loopback by default.** Binds `127.0.0.1`. Exposing beyond localhost
  (0.0.0.0, a tunnel, a reverse proxy) is the operator's explicit choice.
- **Append-only record.** The plate is a FOLD over a JSONL event log; the
  server never mutates history, only appends.
- **Confirm is the only admit.** Side-effectful actions project cost before
  commitment, admit only on explicit confirm, and re-verify at fire time.

What you will not find here: the model router, the mesh posture, the private
build tools, the operator's own open items. Those stay on the workbench.

---

## Try it

Python 3.11+. No third-party runtime deps (standard library only; a model
backend is optional and not required to start the service).

```
export AGNOSTIC_TRIGGER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 -m agnostic_runtime.trigger.server
```

Then open `http://127.0.0.1:8770/` and paste the token into the console's
token field. (The token prints to the terminal where you set it. Keep it
there; it is the admit key.)

To seed a new plate with one example per card kind so you can see the shape:

```
python3 -m agnostic_runtime.trigger.plate seed ~/.agnostic_runtime/cathedral/plate.jsonl
```

### Configuration

Environment variables the service reads:

| Variable                    | Purpose                                                         |
|-----------------------------|-----------------------------------------------------------------|
| `AGNOSTIC_TRIGGER_TOKEN`    | Required unless `~/.agnostic_runtime/users.json` holds a user record (per-user tokens take over when it does). The Bearer token the console presents. |
| `AGNOSTIC_POSTURE_PATH`     | Optional. Path to a posture text file for the model to wear. A built-in fallback is used when unset. |
| `AGNOSTIC_TRIGGER_PORT`     | Port (default `8770`).                                          |
| `AGNOSTIC_TRIGGER_STATE`    | Path to the thread state JSON (default `~/.agnostic_runtime/threads.json`). |
| `AGNOSTIC_TRIGGER_USERS`    | Path to the users file (default: alongside the state path).     |
| `CATHEDRAL_BRIDGE_DIR`      | Where the append-only record lives (default `~/.agnostic_runtime/cathedral`). |
| `ANTHROPIC_API_KEY`         | Optional. Only read if a Claude model is explicitly routed to.  |

For a sanitized systemd + reverse-proxy sketch, see `DEPLOY.md`.

---

## What's shipped here

```
agnostic_runtime/
  __init__.py              package entry
  canvas.py                the Atom and Creation primitives
  classifier.py            turn classification
  correction.py            correction atoms
  gate.py                  the confirm gate and action registry
  harness.py               posture loading
  m3_classifier.py         model-assisted classification
  router.py                model router (Stub / Ollama / Claude adapters)
  runtime.py               the inner runtime
  distinctions/            distinction checks used by runs
  trigger/
    cathedral.py           the bridge for inter-process notes
    plate.py               plate FOLD over the append-only record
    runs.py                run manager
    server.py              the HTTP service
    users.py               user records
```

This is the minimum set the live service needs to start. Attendants,
daemons, inspection scaffolding, and the private build tools are not in
this repo and are not planned for admission under this slice.

---

## License

AGPL-3.0-or-later. See `LICENSE`.

In plain terms: if you run a modified version as a network service, you
must make the modified source available to the users of that service.
Running an unmodified copy for your own use places no obligation on you
beyond keeping this notice.

---

## Contributing

This is a working codebase, not a product. Maintained by one person as time
permits; no SLA. The admission grammar is one-way layered: a change gets
considered on its own merits, but the mesh behind this repo is not opened
by making a PR.

For security concerns, see `SECURITY.md` — do not use public issues for
vulnerability reports. If you've built something interesting on top, a
short note (issue or discussion) is welcome.

For straightforward bugs or clarifications: an issue first. For a code
change: an issue first, then a PR against `main` once the shape is agreed.
Keep PRs small. The admitted slice stays minimum-ship; expansions are
their own admission decision.
