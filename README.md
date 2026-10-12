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

1. Create a **client profile** (Clients in the sidebar).
2. Create a **project** and pick that client.
3. Keep **Current status** as one paragraph you'd send today.
4. After every call, email, decision, or step, **log it**.
5. Drop **PDFs and pictures** on the project (Attachments). They stay next to the database on this computer.
6. Open **Status report** → Print / PDF.

The home screen is a grouped table. Click ▾ on a column to rename it, add a column left or right, or filter like a spreadsheet. Each section has its own filter bar.

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

Working. Resume: grouped board, column menus, per-section filters, client profiles (sidebar → Clients) linked on new projects. Private GitHub first snapshot only — do not push unless Earl asks. Guest `~/labs/linux-growth/work-crm`, host `J:\hermes-exfil\linux-growth\work-crm`. GitHub replay PDF: `J:\hermes-exfil\GitHub-repo-setup-guide.pdf`.
