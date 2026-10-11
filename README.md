# WorkCRM

A tiny **local** project notebook: current status + the log that proves it.

Not a sales CRM. You write a status someone else can read, then keep a dated record of calls, emails, decisions, process steps, and blockers. The **status report** is those two things together.

No cloud. No account. One SQLite file. Runs on **Windows and Linux** with Python 3.11+ — no pip, no Docker. Hand someone this folder; they run it.

## Run

```bash
python3 crm.py
```

Windows:

```text
py -3 crm.py
```

Then open http://127.0.0.1:8765

| OS | Default database |
|---|---|
| Linux | `~/.work-crm/work.db` |
| Windows | `%APPDATA%\WorkCRM\work.db` |

```bash
python3 crm.py --db /path/to/file.db
python3 crm.py --port 9000
```

Binds to **localhost only**. It is not a public website.

## How to use it

1. Create a **project** (and who it's for). Put it in Planning, Execution, or Launch.
2. Keep **Current status** as one paragraph you'd send today.
3. After every call, email, decision, or step, **log it**.
4. Open **Status report** → Print / PDF. That's the pack you share.

The home screen is a grouped table (status/priority chips). The written status and the log still live on the project page.

**Developer mode** (sidebar → Developer): turn it on to add board columns (e.g. Assigned between Client and Status), reorder, or remove custom columns. Built-in columns stay. This is local-only; there is no cloud admin login.

Log types: Call, Email, Meeting, Decision, Process step, Blocker, Note.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Privacy

Do not commit `*.db`. This repo is the app, not your book of work. If you share screenshots, strip names you don't want public.

Old WorkCRM databases (customers/jobs pipeline) are a different schema. Use a new `--db` file or a fresh `~/.work-crm/work.db`.

## License

MIT

## Parked

Working, tests green. Resume here: grouped board + status report + developer-mode custom columns. Not on GitHub yet. Next if we pick it up: more column types, or publish the repo.
