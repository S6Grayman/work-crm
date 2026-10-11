#!/usr/bin/env python3
"""WorkCRM — local project status + process log. Python 3.11+ stdlib only."""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

KINDS = ("call", "email", "meeting", "decision", "step", "blocker", "note")
KIND_LABEL = {
    "call": "Call",
    "email": "Email",
    "meeting": "Meeting",
    "decision": "Decision",
    "step": "Process step",
    "blocker": "Blocker",
    "note": "Note",
}
LANES = ("Planning", "Execution", "Launch")
HEALTH = {
    "not_started": "Not started",
    "working": "Working on it",
    "stuck": "Stuck",
    "done": "Done",
}
PRIORITY = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "critical": "Critical",
}
BUILTIN_COLUMNS = (
    ("name", "Project", "name"),
    ("client", "Client", "client"),
    ("health", "Status", "health"),
    ("priority", "Priority", "priority"),
    ("blockers", "Blockers", "blockers"),
    ("last_log", "Last log", "last_log"),
    ("log_count", "Log", "log_count"),
)


def slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return (s[:40] or "column")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home())) / "WorkCRM"
    else:
        root = Path.home() / ".work-crm"
    root.mkdir(parents=True, exist_ok=True)
    return root / "work.db"


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                client TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                status_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                who TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                happened_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(projects)")}
        extras = {
            "lane": "TEXT NOT NULL DEFAULT 'Planning'",
            "health": "TEXT NOT NULL DEFAULT 'not_started'",
            "priority": "TEXT NOT NULL DEFAULT 'medium'",
        }
        for name, decl in extras.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {decl}")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS board_columns (
                id INTEGER PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                kind TEXT NOT NULL,
                position INTEGER NOT NULL,
                builtin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS column_values (
                column_id INTEGER NOT NULL REFERENCES board_columns(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (column_id, project_id)
            );
            """
        )
        existing = self.conn.execute("SELECT COUNT(*) FROM board_columns").fetchone()[0]
        if existing == 0:
            for pos, (key, label, kind) in enumerate(BUILTIN_COLUMNS):
                self.conn.execute(
                    """INSERT INTO board_columns (key, label, kind, position, builtin)
                       VALUES (?,?,?,?,1)""",
                    (key, label, kind, pos),
                )
        self.conn.commit()

    def add_project(
        self,
        name: str,
        client: str = "",
        lane: str = "Planning",
        health: str = "not_started",
        priority: str = "medium",
    ) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required")
        lane = (lane or "Planning").strip() or "Planning"
        if health not in HEALTH:
            health = "not_started"
        if priority not in PRIORITY:
            priority = "medium"
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO projects (name, client, status, status_at, lane, health, priority, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, client.strip(), "", "", lane, health, priority, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def list_projects(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT projects.*,
                          (SELECT COUNT(*) FROM entries WHERE project_id = projects.id) AS entry_count,
                          (SELECT COUNT(*) FROM entries WHERE project_id = projects.id AND kind='blocker') AS blocker_count,
                          (SELECT MAX(happened_at) FROM entries WHERE project_id = projects.id) AS last_entry_at
                   FROM projects
                   ORDER BY id ASC"""
            )
        )

    def update_project(
        self,
        project_id: int,
        name: str,
        client: str,
        lane: str | None = None,
        health: str | None = None,
        priority: str | None = None,
    ) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required")
        row = self.get_project(project_id)
        if row is None:
            raise ValueError("Project not found")
        lane = (lane if lane is not None else row["lane"]).strip() or "Planning"
        health = health if health in HEALTH else row["health"]
        priority = priority if priority in PRIORITY else row["priority"]
        self.conn.execute(
            """UPDATE projects SET name=?, client=?, lane=?, health=?, priority=?, updated_at=?
               WHERE id=?""",
            (name, client.strip(), lane, health, priority, utcnow(), project_id),
        )
        self.conn.commit()

    def set_health(self, project_id: int, health: str) -> None:
        if health not in HEALTH:
            raise ValueError("Unknown status")
        self.conn.execute(
            "UPDATE projects SET health=?, updated_at=? WHERE id=?",
            (health, utcnow(), project_id),
        )
        self.conn.commit()

    def set_priority(self, project_id: int, priority: str) -> None:
        if priority not in PRIORITY:
            raise ValueError("Unknown priority")
        self.conn.execute(
            "UPDATE projects SET priority=?, updated_at=? WHERE id=?",
            (priority, utcnow(), project_id),
        )
        self.conn.commit()

    def set_lane(self, project_id: int, lane: str) -> None:
        lane = lane.strip() or "Planning"
        self.conn.execute(
            "UPDATE projects SET lane=?, updated_at=? WHERE id=?",
            (lane, utcnow(), project_id),
        )
        self.conn.commit()

    def board(self) -> dict[str, list[sqlite3.Row]]:
        groups: dict[str, list[sqlite3.Row]] = {lane: [] for lane in LANES}
        for project in self.list_projects():
            lane = project["lane"] or "Planning"
            groups.setdefault(lane, [])
            groups[lane].append(project)
        return groups

    def dev_mode(self) -> bool:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='dev_mode'"
        ).fetchone()
        return bool(row and row["value"] == "1")

    def set_dev_mode(self, on: bool) -> None:
        self.conn.execute(
            """INSERT INTO settings (key, value) VALUES ('dev_mode', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            ("1" if on else "0",),
        )
        self.conn.commit()

    def list_columns(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM board_columns ORDER BY position, id")
        )

    def add_column(self, label: str, after: str = "") -> int:
        label = label.strip()
        if not label:
            raise ValueError("Column name is required")
        key = slugify(label)
        taken = {
            row["key"] for row in self.conn.execute("SELECT key FROM board_columns")
        }
        if key in taken:
            raise ValueError("A column with that name already exists")
        cols = self.list_columns()
        insert_at = len(cols)
        if after:
            for col in cols:
                if col["key"] == after:
                    insert_at = int(col["position"]) + 1
                    break
        self.conn.execute(
            "UPDATE board_columns SET position = position + 1 WHERE position >= ?",
            (insert_at,),
        )
        cur = self.conn.execute(
            """INSERT INTO board_columns (key, label, kind, position, builtin)
               VALUES (?,?,?,?,0)""",
            (key, label, "custom", insert_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def delete_column(self, column_id: int) -> None:
        row = self.conn.execute(
            "SELECT builtin FROM board_columns WHERE id=?", (column_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Column not found")
        if row["builtin"]:
            raise ValueError("Built-in columns cannot be deleted")
        self.conn.execute("DELETE FROM board_columns WHERE id=?", (column_id,))
        self.conn.commit()

    def move_column(self, column_id: int, direction: str) -> None:
        cols = self.list_columns()
        idx = next((i for i, c in enumerate(cols) if c["id"] == column_id), None)
        if idx is None:
            raise ValueError("Column not found")
        swap = idx - 1 if direction == "up" else idx + 1
        if swap < 0 or swap >= len(cols):
            return
        a, b = cols[idx], cols[swap]
        self.conn.execute(
            "UPDATE board_columns SET position=? WHERE id=?", (b["position"], a["id"])
        )
        self.conn.execute(
            "UPDATE board_columns SET position=? WHERE id=?", (a["position"], b["id"])
        )
        self.conn.commit()

    def set_cell(self, project_id: int, column_id: int, value: str) -> None:
        if self.get_project(project_id) is None:
            raise ValueError("Project not found")
        col = self.conn.execute(
            "SELECT id FROM board_columns WHERE id=? AND builtin=0", (column_id,)
        ).fetchone()
        if col is None:
            raise ValueError("Column not found")
        self.conn.execute(
            """INSERT INTO column_values (column_id, project_id, value)
               VALUES (?,?,?)
               ON CONFLICT(column_id, project_id) DO UPDATE SET value=excluded.value""",
            (column_id, project_id, value.strip()),
        )
        self.conn.commit()

    def cell_value(self, project_id: int, column_id: int) -> str:
        row = self.conn.execute(
            "SELECT value FROM column_values WHERE column_id=? AND project_id=?",
            (column_id, project_id),
        ).fetchone()
        return row["value"] if row else ""

    def cells_for(self, project_id: int) -> dict[int, str]:
        rows = self.conn.execute(
            "SELECT column_id, value FROM column_values WHERE project_id=?",
            (project_id,),
        )
        return {int(r["column_id"]): r["value"] for r in rows}

    def set_status(self, project_id: int, status: str) -> None:
        if self.get_project(project_id) is None:
            raise ValueError("Project not found")
        now = utcnow()
        self.conn.execute(
            "UPDATE projects SET status=?, status_at=?, updated_at=? WHERE id=?",
            (status.strip(), now, now, project_id),
        )
        self.conn.commit()

    def add_entry(
        self,
        project_id: int,
        kind: str,
        body: str,
        who: str = "",
        happened_at: str | None = None,
    ) -> int:
        if self.get_project(project_id) is None:
            raise ValueError("Project not found")
        if kind not in KINDS:
            raise ValueError("Unknown entry type")
        body = body.strip()
        if not body:
            raise ValueError("Entry text is required")
        when = (happened_at or utcnow()).strip()
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO entries (project_id, kind, who, body, happened_at, created_at)
               VALUES (?,?,?,?,?,?)""",
            (project_id, kind, who.strip(), body, when, now),
        )
        self.conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_entries(self, project_id: int, since: str | None = None) -> list[sqlite3.Row]:
        if since:
            return list(
                self.conn.execute(
                    """SELECT * FROM entries
                       WHERE project_id=? AND happened_at >= ?
                       ORDER BY happened_at DESC, id DESC""",
                    (project_id, since),
                )
            )
        return list(
            self.conn.execute(
                """SELECT * FROM entries
                   WHERE project_id=?
                   ORDER BY happened_at DESC, id DESC""",
                (project_id,),
            )
        )

    def open_blockers(self, project_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM entries
                   WHERE project_id=? AND kind='blocker'
                   ORDER BY happened_at DESC, id DESC""",
                (project_id,),
            )
        )

    def make_report(self, project_id: int, since: str | None = None) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError("Project not found")
        entries = [dict(row) for row in self.list_entries(project_id, since=since)]
        blockers = [dict(row) for row in self.open_blockers(project_id)]
        return {
            "project_id": project["id"],
            "project_name": project["name"],
            "client": project["client"],
            "status": project["status"],
            "status_at": project["status_at"],
            "generated_at": utcnow(),
            "since": since or "",
            "entries": entries,
            "blockers": blockers,
        }

    def search(self, q: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        q = q.strip()
        if not q:
            return [], []
        like = f"%{q}%"
        projects = list(
            self.conn.execute(
                """SELECT * FROM projects
                   WHERE name LIKE ? OR client LIKE ? OR status LIKE ?
                   ORDER BY updated_at DESC""",
                (like, like, like),
            )
        )
        entries = list(
            self.conn.execute(
                """SELECT entries.*, projects.name AS project_name
                   FROM entries JOIN projects ON projects.id = entries.project_id
                   WHERE entries.body LIKE ? OR entries.who LIKE ?
                   ORDER BY entries.happened_at DESC""",
                (like, like),
            )
        )
        return projects, entries


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def short_when(iso: str) -> str:
    if not iso:
        return ""
    return iso[:16].replace("T", " ")


CSS = """
:root {
  --bg:#f6f7fb; --panel:#fff; --ink:#323338; --sec:#676879; --muted:#8a8b96;
  --line:#d0d4e4; --hover:#f0f1f5; --acc:#0073ea; --side:#f7f8fa;
  --plan:#579bfc; --exec:#a25ddc; --launch:#00c875;
}
* { box-sizing:border-box; }
html,body { margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.45 ui-sans-serif,system-ui,sans-serif; }
a { color:var(--acc); text-decoration:none; }
a:hover { text-decoration:underline; }
.app { display:grid; grid-template-columns:232px 1fr; min-height:100vh; }
aside { background:var(--side); border-right:1px solid var(--line);
  padding:18px 14px; display:flex; flex-direction:column; gap:16px; }
.brand { font-weight:700; letter-spacing:.01em; font-size:15px; }
.brand span { color:var(--acc); }
nav { display:flex; flex-direction:column; gap:2px; }
nav a { color:var(--ink); padding:8px 10px; border-radius:8px; }
nav a:hover { background:#e6f2ff; text-decoration:none; }
nav a.on { background:#cce5ff; }
.grow { flex:1; }
aside form input { width:100%; background:#fff; }
.top { display:flex; align-items:center; gap:12px; padding:18px 28px 8px; }
h1 { margin:0; font-size:22px; font-weight:600; }
.sp { flex:1; }
.content { padding:8px 28px 48px; }
.toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap;
  padding:4px 28px 16px; color:var(--sec); font-size:13px; }
.btn, button {
  display:inline-flex; align-items:center; gap:6px;
  background:var(--acc); color:#fff; border:0; border-radius:6px;
  padding:8px 12px; font-weight:600; cursor:pointer; text-decoration:none;
}
.btn:hover, button:hover { filter:brightness(1.05); text-decoration:none; }
.btn.ghost { background:transparent; color:var(--sec); border:1px solid var(--line); }
input, textarea, select {
  width:100%; background:#fff; color:var(--ink);
  border:1px solid var(--line); border-radius:6px; padding:8px 10px; font:inherit;
}
textarea { min-height:90px; resize:vertical; }
label { display:block; font-size:12px; color:var(--muted); margin-top:10px; }
.stack { max-width:560px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.muted { color:var(--muted); }
.faint { color:var(--muted); }
.box { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 18px; }
.record { display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-size:12px; font-weight:500; }
.pill { display:inline-block; font-size:11px; letter-spacing:.03em;
  padding:2px 8px; border-radius:999px; background:#f0f1f5; color:var(--sec); }
.pill.blocker { background:#f4c3cb; color:#5c1219; }
.pill.decision { background:#cce5ff; color:#1a4b8c; }
.pill.step { background:#c3f0d9; color:#0b5c38; }
.pill.call, .pill.email, .pill.meeting { background:#ffe8c0; color:#6b4a00; }
.timeline { margin-top:14px; }
.ev { padding:10px 0; border-bottom:1px solid var(--line); }
.ev .k { font-size:11px; color:var(--muted); }
.status { white-space:pre-wrap; font-size:16px; line-height:1.55; margin:8px 0 0; }
.group { margin:0 28px 28px; }
.group h2 { margin:0 0 8px; font-size:16px; font-weight:600; display:flex; align-items:center; gap:8px; }
.group.Planning h2 { color:var(--plan); }
.group.Execution h2 { color:var(--exec); }
.group.Launch h2 { color:#00c875; }
.board { background:#fff; border:1px solid var(--line); border-radius:8px; overflow:auto; }
.board table { min-width:860px; }
.board th, .board td { border-bottom:1px solid var(--line); padding:0; vertical-align:middle; }
.board th { font-size:12px; color:var(--muted); font-weight:500; padding:8px 10px; background:#fff; }
.board td { height:40px; }
.board .name { padding:0 12px; min-width:240px; }
.board .name a { color:var(--ink); font-weight:500; }
.board .name a:hover { color:var(--acc); }
.board .client, .board .updated, .board .logn { padding:0 10px; color:var(--sec); font-size:13px; }
.chip-cell { padding:0; }
.chip-cell form { margin:0; height:40px; }
.chip-cell select {
  height:40px; border:0; border-radius:0; width:100%; text-align:center;
  font-weight:600; font-size:13px; color:#fff; cursor:pointer; appearance:none;
}
.chip-cell select.not_started { background:#c4c4c4; color:#323338; }
.chip-cell select.working { background:#fdab3d; }
.chip-cell select.stuck { background:#e2445c; }
.chip-cell select.done { background:#00c875; }
.chip-cell select.low { background:#9d99b9; }
.chip-cell select.medium { background:#fdab3d; }
.chip-cell select.high { background:#ff642e; }
.chip-cell select.critical { background:#bb3354; }
.add-row { display:flex; align-items:center; gap:8px; padding:6px 10px; border-top:1px solid var(--line); }
.add-row input { border:0; background:transparent; padding:6px 4px; }
.add-row button { background:transparent; color:var(--muted); font-weight:500; padding:4px 8px; }
.add-row button:hover { color:var(--acc); }
.cell-in { border:0; background:transparent; height:38px; padding:0 10px; border-radius:0; }
.cell-in:focus { outline:2px solid #cce5ff; background:#fff; }
.group.Planning .board { border-left:4px solid var(--plan); }
.group.Execution .board { border-left:4px solid var(--exec); }
.group.Launch .board { border-left:4px solid var(--launch); }
@media (max-width:900px) {
  .app { grid-template-columns:1fr; }
  aside { flex-direction:row; flex-wrap:wrap; align-items:center; }
  .record, .grid2 { grid-template-columns:1fr; }
  .group, .content, .top, .toolbar { margin-left:12px; margin-right:12px; padding-left:8px; padding-right:8px; }
}
@media print {
  aside, .top .btn, .no-print, .toolbar, .add-row { display:none !important; }
  .app { display:block; }
  body { background:#fff; color:#111; }
  .box { border:1px solid #ccc; }
  a { color:#111; }
}
"""


def page(title: str, body: str, nav: str = "projects") -> bytes:
    def item(href: str, key: str, label: str) -> str:
        cls = " on" if nav == key else ""
        return f'<a class="{cls.strip()}" href="{href}">{label}</a>'

    doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · WorkCRM</title>
<style>{CSS}</style>
</head><body>
<div class="app">
  <aside>
    <div class="brand">Work<span>CRM</span></div>
    <nav>
      {item("/", "projects", "Projects")}
      {item("/projects/new", "new", "New project")}
      {item("/dev", "dev", "Developer")}
    </nav>
    <div class="grow"></div>
    <form method="get" action="/search">
      <input name="q" placeholder="Search" aria-label="Search">
    </form>
  </aside>
  <main>{body}</main>
</div>
</body></html>"""
    return doc.encode("utf-8")


def kind_options(selected: str = "step") -> str:
    bits = []
    for k in KINDS:
        sel = " selected" if k == selected else ""
        bits.append(f'<option value="{k}"{sel}>{KIND_LABEL[k]}</option>')
    return "".join(bits)


def map_options(mapping: dict[str, str], selected: str) -> str:
    bits = []
    for key, label in mapping.items():
        sel = " selected" if key == selected else ""
        bits.append(f'<option value="{key}"{sel}>{label}</option>')
    return "".join(bits)


def lane_options(selected: str) -> str:
    names = list(LANES)
    if selected and selected not in names:
        names.append(selected)
    bits = []
    for name in names:
        sel = " selected" if name == selected else ""
        bits.append(f'<option value="{e(name)}"{sel}>{e(name)}</option>')
    return "".join(bits)


def pill(kind: str) -> str:
    return f'<span class="pill {e(kind)}">{e(KIND_LABEL.get(kind, kind))}</span>'


class Handler(BaseHTTPRequestHandler):
    store: Store

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: (v[-1] if v else "") for k, v in parsed.items()}

    def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _bar(self, heading: str, extra: str = "") -> str:
        return f'<div class="top"><h1>{e(heading)}</h1><div class="sp"></div>{extra}</div>'

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path == "/":
                self._send(200, self._home())
            elif path == "/projects/new":
                self._send(200, self._project_form())
            elif path.startswith("/projects/") and path.endswith("/edit"):
                self._send(200, self._project_form(int(path.split("/")[2])))
            elif path.startswith("/projects/") and path.endswith("/report"):
                since = (qs.get("since") or [""])[0]
                self._send(200, self._report(int(path.split("/")[2]), since or None))
            elif path.startswith("/projects/"):
                self._send(200, self._project(int(path.split("/")[2])))
            elif path == "/search":
                self._send(200, self._search((qs.get("q") or [""])[0]))
            elif path == "/dev":
                self._send(200, self._dev())
            else:
                self._send(404, page("Not found", self._bar("Not found") + '<div class="content"><p>Not found.</p></div>'))
        except (ValueError, IndexError):
            self._send(400, page("Bad request", self._bar("Bad request") + '<div class="content"><p>Bad request.</p></div>'))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        form = self._read_form()
        try:
            if path == "/projects/new":
                pid = self.store.add_project(
                    form.get("name", ""),
                    form.get("client", ""),
                    lane=form.get("lane", "Planning") or "Planning",
                    health=form.get("health", "not_started") or "not_started",
                    priority=form.get("priority", "medium") or "medium",
                )
                status = form.get("status", "").strip()
                if status:
                    self.store.set_status(pid, status)
                if form.get("stay") == "board":
                    self._redirect("/")
                else:
                    self._redirect(f"/projects/{pid}")
            elif path.startswith("/projects/") and path.endswith("/edit"):
                pid = int(path.split("/")[2])
                self.store.update_project(
                    pid,
                    form.get("name", ""),
                    form.get("client", ""),
                    lane=form.get("lane"),
                    health=form.get("health"),
                    priority=form.get("priority"),
                )
                self._redirect(f"/projects/{pid}")
            elif path.startswith("/projects/") and path.endswith("/health"):
                pid = int(path.split("/")[2])
                self.store.set_health(pid, form.get("health", ""))
                self._redirect("/")
            elif path.startswith("/projects/") and path.endswith("/priority"):
                pid = int(path.split("/")[2])
                self.store.set_priority(pid, form.get("priority", ""))
                self._redirect("/")
            elif path.startswith("/projects/") and path.endswith("/status"):
                pid = int(path.split("/")[2])
                self.store.set_status(pid, form.get("status", ""))
                self._redirect(f"/projects/{pid}")
            elif path.startswith("/projects/") and path.endswith("/entry"):
                pid = int(path.split("/")[2])
                when = form.get("happened_at", "").strip()
                if when and "T" not in when:
                    when = when + "T12:00:00+00:00"
                self.store.add_entry(
                    pid,
                    form.get("kind", "note"),
                    form.get("body", ""),
                    who=form.get("who", ""),
                    happened_at=when or None,
                )
                self._redirect(f"/projects/{pid}")
            elif path.startswith("/projects/") and path.endswith("/cell"):
                pid = int(path.split("/")[2])
                self.store.set_cell(pid, int(form.get("column_id", "0") or "0"), form.get("value", ""))
                self._redirect("/")
            elif path == "/dev/toggle":
                self.store.set_dev_mode(not self.store.dev_mode())
                self._redirect("/dev")
            elif path == "/dev/columns":
                if not self.store.dev_mode():
                    raise ValueError("Turn on developer mode first")
                self.store.add_column(form.get("label", ""), after=form.get("after", ""))
                self._redirect("/dev")
            elif path.startswith("/dev/columns/") and path.endswith("/delete"):
                if not self.store.dev_mode():
                    raise ValueError("Turn on developer mode first")
                self.store.delete_column(int(path.split("/")[3]))
                self._redirect("/dev")
            elif path.startswith("/dev/columns/") and path.endswith("/move"):
                if not self.store.dev_mode():
                    raise ValueError("Turn on developer mode first")
                self.store.move_column(int(path.split("/")[3]), form.get("direction", "up"))
                self._redirect("/dev")
            else:
                self._send(404, page("Not found", self._bar("Not found") + '<div class="content"><p>Not found.</p></div>'))
        except ValueError as err:
            self._send(
                400,
                page("Error", self._bar("Error") + f'<div class="content"><p>{e(err)}</p></div>'),
            )

    def _home(self) -> bytes:
        def chip(action: str, field: str, value: str, mapping: dict[str, str], pid: int) -> str:
            return (
                f'<td class="chip-cell"><form method="post" action="/projects/{pid}/{action}">'
                f'<select class="{e(value)}" name="{field}" onchange="this.form.submit()">'
                f"{map_options(mapping, value)}</select></form></td>"
            )

        def cell(col: Any, p: Any, values: dict[int, str]) -> str:
            kind = col["kind"]
            if kind == "name":
                return f'<td class="name"><a href="/projects/{p["id"]}">{e(p["name"])}</a></td>'
            if kind == "client":
                return f'<td class="client">{e(p["client"]) or "—"}</td>'
            if kind == "health":
                return chip("health", "health", p["health"], HEALTH, p["id"])
            if kind == "priority":
                return chip("priority", "priority", p["priority"], PRIORITY, p["id"])
            if kind == "blockers":
                n = p["blocker_count"]
                text = f'{n} blocker{"s" if n != 1 else ""}' if n else "—"
                return f'<td class="logn">{text}</td>'
            if kind == "last_log":
                return f'<td class="updated">{e(short_when(p["last_entry_at"] or p["updated_at"]))}</td>'
            if kind == "log_count":
                return f'<td class="logn">{p["entry_count"]}</td>'
            val = values.get(int(col["id"]), "")
            return (
                f'<td class="custom"><form method="post" action="/projects/{p["id"]}/cell">'
                f'<input type="hidden" name="column_id" value="{col["id"]}">'
                f'<input class="cell-in" name="value" value="{e(val)}" '
                f'placeholder="—" onchange="this.form.submit()"></form></td>'
            )

        columns = self.store.list_columns()
        headers = "".join(f"<th>{e(c['label'])}</th>" for c in columns)
        groups = []
        for lane, projects in self.store.board().items():
            rows = []
            for p in projects:
                values = self.store.cells_for(p["id"])
                rows.append("<tr>" + "".join(cell(c, p, values) for c in columns) + "</tr>")
            cls = lane if lane in LANES else "other"
            add = f"""
            <form class="add-row" method="post" action="/projects/new">
              <input type="hidden" name="lane" value="{e(lane)}">
              <input type="hidden" name="stay" value="board">
              <input name="name" placeholder="+ Add project" required>
              <button type="submit">Add</button>
            </form>"""
            groups.append(
                f'<section class="group {e(cls)}"><h2>▾ {e(lane)}</h2>'
                f'<div class="board"><table><tr>{headers}</tr>'
                f"{''.join(rows)}</table>{add}</div></section>"
            )
        extra = '<a class="btn" href="/projects/new">New project</a>'
        hint = (
            "Developer mode on — add or move columns under Developer."
            if self.store.dev_mode()
            else "Status and priority change in the grid. Open a row for the written status and log."
        )
        toolbar = f'<div class="toolbar"><span>Main table</span><span class="muted">{hint}</span></div>'
        body = self._bar("Projects", extra) + toolbar + "".join(groups)
        return page("Projects", body, "projects")

    def _dev(self) -> bytes:
        on = self.store.dev_mode()
        toggle = "Turn developer mode off" if on else "Turn developer mode on"
        extra = (
            f'<form method="post" action="/dev/toggle">'
            f'<button type="submit">{toggle}</button></form>'
        )
        if not on:
            body = self._bar("Developer", extra) + """
            <div class="content">
              <div class="box">
                <p>Developer mode lets you customize the board: add columns (Assigned, Due date, Shop…),
                drop them between Client and Status, and reorder them.</p>
                <p class="muted">This is local only. There is no cloud admin account.</p>
              </div>
            </div>"""
            return page("Developer", body, "dev")
        rows = []
        for col in self.store.list_columns():
            lock = "built-in" if col["builtin"] else "custom"
            delete = ""
            if not col["builtin"]:
                delete = (
                    f"<form method='post' action='/dev/columns/{col['id']}/delete'>"
                    f"<button class='btn ghost' type='submit'>Remove</button></form>"
                )
            rows.append(
                f"<tr><td>{e(col['label'])}</td><td class='muted'>{lock}</td><td>"
                f"<form method='post' action='/dev/columns/{col['id']}/move' style='display:inline'>"
                f"<input type='hidden' name='direction' value='up'>"
                f"<button class='btn ghost' type='submit'>Up</button></form> "
                f"<form method='post' action='/dev/columns/{col['id']}/move' style='display:inline'>"
                f"<input type='hidden' name='direction' value='down'>"
                f"<button class='btn ghost' type='submit'>Down</button></form>"
                f"</td><td>{delete}</td></tr>"
            )
        after_opts = "".join(
            f'<option value="{e(c["key"])}">{e(c["label"])}</option>'
            for c in self.store.list_columns()
        )
        form = f"""
        <form class="stack" method="post" action="/dev/columns">
          <label>New column name <input name="label" required placeholder="Assigned"></label>
          <label>Insert after <select name="after">{after_opts}</select></label>
          <button type="submit">Add column</button>
        </form>"""
        body = self._bar("Developer", extra) + f"""
        <div class="content record">
          <div class="box">
            <h2>Add a column</h2>
            <p class="muted">Example: name it Assigned, insert after Client. Type a name in the new cell on the board.</p>
            {form}
          </div>
          <div class="box">
            <h2>Board columns</h2>
            <table><tr><th>Column</th><th></th><th>Order</th><th></th></tr>
            {''.join(rows)}</table>
          </div>
        </div>"""
        return page("Developer", body, "dev")

    def _project_form(self, project_id: int | None = None) -> bytes:
        p = self.store.get_project(project_id) if project_id else None
        if project_id and p is None:
            return page("Not found", self._bar("Not found") + '<div class="content"><p>Project not found.</p></div>')
        action = f"/projects/{project_id}/edit" if p else "/projects/new"
        heading = "Edit project" if p else "New project"
        lane = p["lane"] if p else "Planning"
        health = p["health"] if p else "not_started"
        priority = p["priority"] if p else "medium"
        status_field = (
            ""
            if p
            else '<label>Opening status <textarea name="status" placeholder="Where things stand today."></textarea></label>'
        )
        form = f"""
        <form class="stack" method="post" action="{action}">
          <label>Project name <input name="name" required value="{e(p['name'] if p else '')}"></label>
          <label>Client / who it's for <input name="client" value="{e(p['client'] if p else '')}"></label>
          <label>Group <select name="lane">{lane_options(lane)}</select></label>
          <div class="grid2">
            <label>Status <select name="health">{map_options(HEALTH, health)}</select></label>
            <label>Priority <select name="priority">{map_options(PRIORITY, priority)}</select></label>
          </div>
          {status_field}
          <button type="submit">Save</button>
        </form>"""
        body = self._bar(heading) + f'<div class="content">{form}</div>'
        return page(heading, body, "new" if not p else "projects")

    def _project(self, project_id: int) -> bytes:
        p = self.store.get_project(project_id)
        if p is None:
            return page("Not found", self._bar("Not found") + '<div class="content"><p>Project not found.</p></div>')
        entries = self.store.list_entries(project_id)
        blockers = self.store.open_blockers(project_id)
        extra = (
            f'<a class="btn ghost" href="/projects/{project_id}/edit">Edit</a>'
            f'<a class="btn" href="/projects/{project_id}/report">Status report</a>'
        )
        block_html = ""
        if blockers:
            items = "".join(f"<li>{e(b['body'])}</li>" for b in blockers)
            block_html = f'<div class="box" style="margin-bottom:16px"><h2>Open blockers</h2><ul>{items}</ul></div>'
        timeline = self._timeline(entries)
        status_when = short_when(p["status_at"]) or "not set"
        body = self._bar(p["name"], extra) + f"""
        <div class="content">
          {f'<p class="muted">For {e(p["client"])}</p>' if p["client"] else ''}
          {block_html}
          <div class="record">
            <div class="box">
              <h2>Current status</h2>
              <p class="faint">Updated {e(status_when)}</p>
              <p class="status">{e(p["status"]) or "Write the status someone else could read tomorrow."}</p>
              <form class="stack" method="post" action="/projects/{project_id}/status">
                <label>Replace status
                  <textarea name="status" required>{e(p["status"])}</textarea>
                </label>
                <button type="submit">Save status</button>
              </form>
            </div>
            <div class="box">
              <h2>Log a step</h2>
              <p class="muted">Calls, emails, decisions, process steps. This is the evidence behind the status.</p>
              <form class="stack" method="post" action="/projects/{project_id}/entry">
                <div class="grid2">
                  <label>Type <select name="kind">{kind_options()}</select></label>
                  <label>Who <input name="who" placeholder="Ada, vendor, me"></label>
                </div>
                <label>When <input name="happened_at" type="date"></label>
                <label>What happened <textarea name="body" required></textarea></label>
                <button type="submit">Add to log</button>
              </form>
              {timeline}
            </div>
          </div>
        </div>"""
        return page(p["name"], body, "projects")

    def _report(self, project_id: int, since: str | None) -> bytes:
        try:
            report = self.store.make_report(project_id, since=since)
        except ValueError:
            return page("Not found", self._bar("Not found") + '<div class="content"><p>Project not found.</p></div>')
        rows = []
        for entry in report["entries"]:
            who = f" · {e(entry['who'])}" if entry["who"] else ""
            rows.append(
                f"<tr><td>{e(short_when(entry['happened_at']))}</td>"
                f"<td>{pill(entry['kind'])}{who}</td>"
                f"<td>{e(entry['body'])}</td></tr>"
            )
        table = (
            "<table><tr><th>When</th><th>Type</th><th>What happened</th></tr>"
            + ("".join(rows) or "<tr><td colspan='3' class='muted'>No log items in this window.</td></tr>")
            + "</table>"
        )
        blockers = report["blockers"]
        block = ""
        if blockers:
            items = "".join(f"<li>{e(b['body'])}</li>" for b in blockers)
            block = f"<h2>Blockers</h2><ul>{items}</ul>"
        client = f'<p class="muted">For {e(report["client"])}</p>' if report["client"] else ""
        extra = '<button class="btn ghost no-print" type="button" onclick="window.print()">Print / PDF</button>'
        body = self._bar("Status report", extra) + f"""
        <div class="content">
          <div class="box">
            <p class="faint">Generated {e(short_when(report["generated_at"]))}</p>
            <h2>{e(report["project_name"])}</h2>
            {client}
            <h2>Status</h2>
            <p class="status">{e(report["status"]) or "No status written yet."}</p>
            <p class="faint">Status set {e(short_when(report["status_at"]) or "—")}</p>
            {block}
            <h2>Supporting log</h2>
            <p class="muted">Process and communication that back the status above.</p>
            {table}
          </div>
        </div>"""
        return page(f"Report · {report['project_name']}", body, "projects")

    def _search(self, q: str) -> bytes:
        projects, entries = self.store.search(q)
        prows = "".join(
            f"<tr><td><a href='/projects/{p['id']}'>{e(p['name'])}</a></td><td>{e(p['client'])}</td></tr>"
            for p in projects
        ) or "<tr><td colspan='2' class='muted'>None</td></tr>"
        erows = "".join(
            f"<tr><td><a href='/projects/{en['project_id']}'>{e(en['project_name'])}</a></td>"
            f"<td>{pill(en['kind'])}</td><td>{e(en['body'])}</td></tr>"
            for en in entries
        ) or "<tr><td colspan='3' class='muted'>None</td></tr>"
        body = self._bar("Search") + f"""
        <div class="content">
          <p class="muted">Results for “{e(q)}”</p>
          <div class="box"><h2>Projects</h2><table><tr><th>Name</th><th>Client</th></tr>{prows}</table></div>
          <div class="box" style="margin-top:16px"><h2>Log</h2>
            <table><tr><th>Project</th><th>Type</th><th>What</th></tr>{erows}</table></div>
        </div>"""
        return page("Search", body, "projects")

    def _timeline(self, rows: list[Any]) -> str:
        if not rows:
            return '<p class="faint">No log yet. First call or first step goes here.</p>'
        bits = []
        for r in rows:
            who = f" · {e(r['who'])}" if r["who"] else ""
            bits.append(
                f'<div class="ev"><div class="k">{pill(r["kind"])}{who} · {e(short_when(r["happened_at"]))}</div>'
                f'<div>{e(r["body"])}</div></div>'
            )
        return '<div class="timeline">' + "".join(bits) + "</div>"


def make_handler(store: Store) -> type[Handler]:
    class Bound(Handler):
        pass

    Bound.store = store
    Bound.protocol_version = "HTTP/1.1"
    return Bound


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WorkCRM local server")
    parser.add_argument("--db", default=str(default_db_path()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    store = Store(args.db)
    handler = make_handler(store)
    httpd = HTTPServer((args.host, args.port), handler)
    print(f"WorkCRM  http://{args.host}:{args.port}  db={args.db}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        httpd.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
