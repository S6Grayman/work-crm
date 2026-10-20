# WorkCRM

A **local** project notebook: one clear status, plus the log that proves it.

Not a sales CRM. You keep clients, projects, and a dated record of calls, emails, decisions, steps, and blockers. The **status report** is those two things together — ready to print.

No cloud. No account. Work stays on **your** computer.

## Try it

This is not a website. You run it on your PC, then open http://127.0.0.1:8765

**Windows (easiest):** download **WorkCRM.exe** from [Releases](https://github.com/S6Grayman/work-crm/releases). Double-click it. Leave the window open while you work; close it to quit. Windows may warn the first time (the file is new and unsigned) — **More info → Run anyway**.

**Python 3.11+** (Windows or Linux), from this folder:

```bash
python3 crm.py
```

Windows: `py -3 crm.py`

Or double-click `Start-WorkCRM.bat` (Windows) / `start-workcrm.sh` (Linux).

**Docker:**

```text
docker run -d --name workcrm -p 8765:8765 s6grayman/work-crm:latest
```

Then open http://127.0.0.1:8765 — not `0.0.0.0`. Stop/Start the `workcrm` container in Docker Desktop whenever you need it.

## Where your work is saved

| OS | Database |
|---|---|
| Windows | `%APPDATA%\WorkCRM\work.db` |
| Linux | `~/.work-crm/work.db` |

Attachments live in a `files/` folder next to that database. They are not uploaded anywhere.

**Export** (sidebar) saves a `WorkCRM-update-….workcrm` backup you can keep or send. **Import** adds what’s new without wiping your local projects.

The app listens on **localhost only**. It is not a public server.

## How to use it

1. **Profile** — your name and email (used on exports and the audit log).
2. **Clients** — company profiles.
3. **New project** — pick a client, then keep **Current status** as one paragraph you’d send today.
4. After every call, email, decision, or step — **log it**.
5. Attach PDFs and pictures on the project (12 MB each).
6. **Status report** → Print / PDF.
7. Move work with **Group** (Planning → Execution → Launch). **Archive** takes a project off the board but keeps it.

The board is a grouped table. Use ▾ on a column to rename it, add a column, or filter like a spreadsheet.

Log types: Call, Email, Meeting, Decision, Process step, Blocker, Note.

## For developers

```bash
python3 -m unittest discover -s tests -v
```

Do not commit `*.db` or a `files/` folder with real client data.

## License

MIT
