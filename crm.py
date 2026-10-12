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
from email import message_from_bytes
from email.policy import HTTP as EMAIL_HTTP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

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
    "not_started": "Queued",
    "working": "In motion",
    "stuck": "Blocked",
    "done": "Complete",
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


CLIENT_FIELDS = (
    ("name", "Company name"),
    ("short_name", "Short name"),
    ("industry", "Industry"),
    ("website", "Website"),
    ("street", "Street"),
    ("city", "City"),
    ("region", "State / region"),
    ("postal", "Postal code"),
    ("country", "Country"),
    ("phone", "Main phone"),
    ("email", "Main email"),
    ("billing_email", "Billing email"),
    ("contact_name", "Contact person"),
    ("contact_title", "Contact title"),
    ("contact_phone", "Contact phone"),
    ("contact_email", "Contact email"),
    ("preferred_contact", "Preferred contact"),
    ("timezone", "Timezone"),
    ("account_ref", "Account / PO number"),
    ("notes", "Notes"),
)

ATTACH_MAX = 12 * 1024 * 1024
ATTACH_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


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
            "client_id": "INTEGER",
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
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                street TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                postal TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                billing_email TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',
                contact_title TEXT NOT NULL DEFAULT '',
                contact_phone TEXT NOT NULL DEFAULT '',
                contact_email TEXT NOT NULL DEFAULT '',
                preferred_contact TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT '',
                account_ref TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                stored TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
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
        client_id: int | None = None,
    ) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required")
        lane = (lane or "Planning").strip() or "Planning"
        if health not in HEALTH:
            health = "not_started"
        if priority not in PRIORITY:
            priority = "medium"
        label = client.strip()
        cid: int | None = None
        if client_id:
            profile = self.get_client(int(client_id))
            if profile is None:
                raise ValueError("Client not found")
            cid = int(profile["id"])
            label = profile["name"]
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO projects (name, client, client_id, status, status_at, lane, health, priority, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, label, cid, "", "", lane, health, priority, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def files_root(self) -> Path:
        return Path(self.db_path).resolve().parent / "files"

    def add_attachment(
        self, project_id: int, filename: str, data: bytes, mime: str = ""
    ) -> int:
        if self.get_project(project_id) is None:
            raise ValueError("Project not found")
        raw_name = Path(filename.replace("\\", "/")).name.strip()
        if not raw_name or raw_name in (".", ".."):
            raise ValueError("File name is required")
        ext = Path(raw_name).suffix.lower()
        if ext not in ATTACH_TYPES:
            raise ValueError("Allowed: PDF, PNG, JPG, GIF, WebP, TXT, CSV")
        if not data:
            raise ValueError("Empty file")
        if len(data) > ATTACH_MAX:
            raise ValueError("File is too large (12 MB max)")
        mime = (mime or ATTACH_TYPES[ext]).split(";")[0].strip() or ATTACH_TYPES[ext]
        now = utcnow()
        cur = self.conn.execute(
            """INSERT INTO attachments (project_id, name, stored, mime, size, created_at)
               VALUES (?,?,?,?,?,?)""",
            (project_id, raw_name, "", mime, len(data), now),
        )
        aid = int(cur.lastrowid)
        stored = f"{project_id}/{aid}{ext}"
        dest = self.files_root() / stored
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.conn.execute("UPDATE attachments SET stored=? WHERE id=?", (stored, aid))
        self.conn.commit()
        return aid

    def list_attachments(self, project_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM attachments WHERE project_id=? ORDER BY id ASC",
                (project_id,),
            )
        )

    def read_attachment(self, project_id: int, attachment_id: int) -> tuple[bytes, str, str]:
        row = self.conn.execute(
            "SELECT * FROM attachments WHERE id=? AND project_id=?",
            (attachment_id, project_id),
        ).fetchone()
        if row is None:
            raise ValueError("File not found")
        path = self.files_root() / row["stored"]
        if not path.is_file():
            raise ValueError("File missing on disk")
        return path.read_bytes(), row["mime"], row["name"]

    def delete_attachment(self, project_id: int, attachment_id: int) -> None:
        row = self.conn.execute(
            "SELECT * FROM attachments WHERE id=? AND project_id=?",
            (attachment_id, project_id),
        ).fetchone()
        if row is None:
            raise ValueError("File not found")
        path = self.files_root() / row["stored"]
        self.conn.execute(
            "DELETE FROM attachments WHERE id=? AND project_id=?",
            (attachment_id, project_id),
        )
        self.conn.commit()
        if path.is_file():
            path.unlink()

    def add_client(self, name: str, **fields: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Company name is required")
        now = utcnow()
        keys = ["name", "created_at", "updated_at"]
        vals: list[Any] = [name, now, now]
        allowed = {k for k, _ in CLIENT_FIELDS if k != "name"}
        for key, value in fields.items():
            if key not in allowed:
                continue
            keys.append(key)
            vals.append((value or "").strip())
        placeholders = ",".join("?" * len(keys))
        cur = self.conn.execute(
            f"INSERT INTO clients ({', '.join(keys)}) VALUES ({placeholders})",
            vals,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_client(self, client_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()

    def list_clients(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT clients.*,
                          (SELECT COUNT(*) FROM projects WHERE client_id = clients.id) AS project_count
                   FROM clients ORDER BY name COLLATE NOCASE"""
            )
        )

    def update_client(self, client_id: int, name: str, **fields: str) -> None:
        if self.get_client(client_id) is None:
            raise ValueError("Client not found")
        name = name.strip()
        if not name:
            raise ValueError("Company name is required")
        allowed = {k for k, _ in CLIENT_FIELDS if k != "name"}
        sets = ["name=?", "updated_at=?"]
        vals: list[Any] = [name, utcnow()]
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            vals.append((value or "").strip())
        vals.append(client_id)
        self.conn.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id=?", vals)
        self.conn.execute(
            "UPDATE projects SET client=? WHERE client_id=?", (name, client_id)
        )
        self.conn.commit()

    def projects_for_client(self, client_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM projects WHERE client_id=? ORDER BY updated_at DESC""",
                (client_id,),
            )
        )

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
        client_id: int | None = None,
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
        label = client.strip()
        cid = row["client_id"]
        if client_id is not None:
            if int(client_id) == 0:
                cid = None
            else:
                profile = self.get_client(int(client_id))
                if profile is None:
                    raise ValueError("Client not found")
                cid = int(profile["id"])
                label = profile["name"]
        self.conn.execute(
            """UPDATE projects SET name=?, client=?, client_id=?, lane=?, health=?, priority=?, updated_at=?
               WHERE id=?""",
            (name, label, cid, lane, health, priority, utcnow(), project_id),
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

    def insert_column(self, label: str, beside: str, side: str = "right") -> int:
        if side not in ("left", "right"):
            raise ValueError("Insert left or right")
        if side == "right":
            return self.add_column(label, after=beside)
        cols = self.list_columns()
        prev = ""
        for col in cols:
            if col["key"] == beside:
                return self.add_column(label, after=prev)
            prev = col["key"]
        return self.add_column(label)

    def rename_column(self, column_id: int, label: str) -> None:
        label = label.strip()
        if not label:
            raise ValueError("Column name is required")
        row = self.conn.execute(
            "SELECT id FROM board_columns WHERE id=?", (column_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Column not found")
        self.conn.execute(
            "UPDATE board_columns SET label=? WHERE id=?", (label, column_id)
        )
        self.conn.commit()

    def column_text(self, project: sqlite3.Row, col: sqlite3.Row) -> str:
        kind = col["kind"]
        if kind == "name":
            return project["name"] or ""
        if kind == "client":
            return project["client"] or ""
        if kind == "health":
            return HEALTH.get(project["health"], project["health"] or "")
        if kind == "priority":
            return PRIORITY.get(project["priority"], project["priority"] or "")
        if kind == "blockers":
            keys = project.keys()
            n = project["blocker_count"] if "blocker_count" in keys else 0
            return str(n) if n else ""
        if kind == "last_log":
            when = project["last_entry_at"] if "last_entry_at" in project.keys() else ""
            return short_when(when or project["updated_at"] or "")
        if kind == "log_count":
            n = project["entry_count"] if "entry_count" in project.keys() else 0
            return str(n)
        return self.cell_value(int(project["id"]), int(col["id"]))

    def matches(self, project: sqlite3.Row, filters: dict[str, str]) -> bool:
        if not filters:
            return True
        cols = {c["key"]: c for c in self.list_columns()}
        for key, want in filters.items():
            if not want:
                continue
            col = cols.get(key)
            if col is None:
                continue
            got = self.column_text(project, col)
            raw = ""
            if col["kind"] in project.keys():
                raw = str(project[col["kind"]] or "")
            if want == "(blank)":
                if got.strip():
                    return False
                continue
            if got != want and raw != want:
                return False
        return True

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
  --bg:#f4f0f7; --panel:#fff; --ink:#1d1524; --sec:#5a5163; --muted:#847a8e;
  --line:rgba(61,24,80,.12); --hover:#f3eaf8; --acc:#6b2d8b; --acc2:#2f9e7a; --side:#35143f;
  --side-ink:#f7eef8; --side-dim:#c9b3d4; --ring:rgba(107,45,139,.18);
  --ok:#1f9d62; --warn:#e0892c; --bad:#d4535a; --info:#4f7dd9;
  --plan:#5b8def; --exec:#9b5de5; --lau:#e8a317;
}
* { box-sizing:border-box; }
html,body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 ui-sans-serif,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
h1, .brand, .group h2 { font-weight:650; letter-spacing:-.03em; }
a { color:var(--info); text-decoration:none; }
a:hover { text-decoration:underline; }
.app { display:grid; grid-template-columns:232px 1fr; min-height:100vh; }
aside { background:var(--side); color:var(--side-ink); border-right:0;
  padding:20px 14px; display:flex; flex-direction:column; gap:16px; }
.brand { font-weight:700; font-size:17px; letter-spacing:-.04em; color:#fff; }
.brand span { color:#f0c36d; font-weight:600; letter-spacing:0; font-size:17px; }
nav { display:flex; flex-direction:column; gap:2px; }
nav a { color:var(--side-dim); padding:8px 10px; border-radius:8px; font-weight:500; }
nav a:hover { background:rgba(255,255,255,.08); color:#fff; text-decoration:none; }
nav a.on { background:rgba(255,255,255,.14); color:#fff; }
.grow { flex:1; }
aside form input { width:100%; background:rgba(0,0,0,.25); color:#fff; border:1px solid rgba(255,255,255,.16); border-radius:8px; }
aside form input::placeholder { color:#c9b3d4; }
.top { display:flex; align-items:center; gap:12px; padding:22px 28px 8px; }
h1 { margin:0; font-size:22px; }
.sp { flex:1; }
.content { padding:8px 28px 48px; }
.toolbar { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap;
  padding:0 28px 16px; color:var(--sec); font-size:13px; }
.toolbar span:first-child { font-size:13px; font-weight:600; color:var(--ink); letter-spacing:0; }
.btn, button {
  display:inline-flex; align-items:center; gap:6px;
  background:var(--acc); color:#fff; border:0; border-radius:8px;
  padding:8px 14px; font-weight:600; font-size:14px; cursor:pointer; text-decoration:none;
}
.btn:hover, button:hover { filter:brightness(1.12); text-decoration:none; }
.btn.ghost { background:#fff; color:var(--ink); border:1px solid var(--line); }
input, textarea, select {
  width:100%; background:#fff; color:var(--ink);
  border:1px solid var(--line); border-radius:8px; padding:8px 10px; font:inherit;
}
input:focus, textarea:focus, select:focus { outline:2px solid var(--ring); border-color:var(--acc); }
textarea { min-height:90px; resize:vertical; }
label { display:block; font-size:12px; color:var(--muted); margin-top:10px; font-weight:600; letter-spacing:.02em; }
.stack { max-width:560px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.muted, .faint { color:var(--muted); }
.box { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px;
  box-shadow:0 1px 2px rgba(0,0,0,.04); }
.record { display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }
.files { display:flex; flex-wrap:wrap; gap:12px; margin:8px 0; }
.files .item { border:1px solid var(--line); background:#fff; padding:8px; min-width:140px; max-width:220px; border-radius:10px; }
.files img { display:block; max-width:200px; max-height:140px; border-radius:6px; }
.files .item form { margin-top:6px; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; }
.pill { display:inline-block; font-size:11px; font-weight:600; letter-spacing:.02em;
  padding:2px 8px; border-radius:999px; background:#f0eeeb; color:var(--sec); }
.pill.blocker { background:#fef3f2; color:var(--bad); }
.pill.decision { background:#ecfdf3; color:var(--ok); }
.pill.step { background:#f0fdfa; color:var(--acc2); }
.pill.call, .pill.email, .pill.meeting { background:#eff8ff; color:var(--info); }
.timeline { margin-top:14px; }
.ev { padding:10px 0; border-bottom:1px solid var(--line); }
.ev .k { font-size:11px; color:var(--muted); }
.status { white-space:pre-wrap; font-size:16px; line-height:1.55; margin:8px 0 0; }
.group { margin:0 28px 28px; }
.group h2 { margin:0 0 12px; font-size:15px; font-weight:650; color:var(--ink); border:0; padding:0 0 0 14px; display:block; position:relative; }
.group h2::before { content:""; position:absolute; left:0; top:4px; width:8px; height:8px; border-radius:99px; background:var(--muted); }
.group.Planning h2::before { background:var(--plan); }
.group.Execution h2::before { background:var(--exec); }
.group.Launch h2::before { background:var(--lau); }
.board { background:var(--panel); border:1px solid var(--line); overflow:auto; border-radius:12px;
  box-shadow:0 1px 2px rgba(0,0,0,.04); }
.board table { min-width:860px; }
.board th, .board td { border-bottom:1px solid var(--line); vertical-align:middle; }
.board th { padding:10px 12px; background:#faf9f7; }
.th-wrap { display:flex; align-items:center; justify-content:space-between; gap:6px; }
.colmenu { position:relative; }
.colmenu summary { cursor:pointer; list-style:none; color:var(--muted); font-size:12px; padding:4px 6px; border-radius:6px; }
.colmenu summary:hover { background:var(--hover); }
.colmenu summary::-webkit-details-marker { display:none; }
.colmenu .menu { position:absolute; right:0; top:120%; z-index:20; background:#fff;
  border:1px solid var(--line); padding:12px; min-width:220px; border-radius:12px;
  box-shadow:0 8px 24px rgba(0,0,0,.10); }
.colmenu .menu form { margin:8px 0 0; }
.colmenu .menu button, .colmenu .menu .btn { width:100%; justify-content:center; margin-top:4px; }
.filters { display:flex; flex-wrap:wrap; gap:10px; align-items:end; margin:0 0 10px; }
.filters label { margin:0; }
.filters select { width:auto; min-width:8rem; }
.board td { height:44px; padding:4px 10px; }
.board .name { min-width:240px; padding-left:14px; }
.board .name a { color:var(--ink); font-weight:600; }
.board .name a:hover { color:var(--info); }
.board .client, .board .updated, .board .logn { color:var(--sec); font-size:13px; }
.chip-cell { padding:4px 8px; }
.chip-cell form { margin:0; }
.chip-cell select {
  width:auto; min-width:7.5rem; height:28px; border:0;
  border-radius:999px; padding:0 10px; text-align:left; font-weight:600; font-size:12px;
  cursor:pointer; appearance:auto; color:#fff; background:#7b8fa1;
}
.chip-cell select.not_started { background:#7b8fa1; color:#fff; }
.chip-cell select.working { background:#2f9e7a; color:#fff; }
.chip-cell select.stuck { background:#d4535a; color:#fff; }
.chip-cell select.done { background:#1f9d62; color:#fff; }
.chip-cell select.low { background:#7b8fa1; color:#fff; }
.chip-cell select.medium { background:#4f7dd9; color:#fff; }
.chip-cell select.high { background:#e0892c; color:#fff; }
.chip-cell select.critical { background:#c2410c; color:#fff; font-weight:700; }
.add-row { display:flex; align-items:center; gap:8px; padding:8px 12px; border-top:1px dashed var(--line); }
.add-row input { border:0; background:transparent; padding:6px 4px; }
.add-row button { background:transparent; color:var(--muted); font-weight:500; padding:4px 8px; }
.add-row button:hover { color:var(--ink); }
.cell-in { border:0; background:transparent; height:36px; padding:0 4px; border-radius:0; }
.cell-in:focus { outline:2px solid rgba(23,23,23,.16); background:#fff; border-radius:6px; }
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
  .box { border:1px solid #ccc; box-shadow:none; }
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
      {item("/clients", "clients", "Clients")}
      {item("/projects/new", "new", "New project")}
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
                self._send(200, self._home(qs))
            elif path == "/projects/new":
                self._send(200, self._project_form())
            elif path.startswith("/projects/") and path.endswith("/edit"):
                self._send(200, self._project_form(int(path.split("/")[2])))
            elif path.startswith("/projects/") and path.endswith("/report"):
                since = (qs.get("since") or [""])[0]
                self._send(200, self._report(int(path.split("/")[2]), since or None))
            elif "/files/" in path and path.startswith("/projects/"):
                bits = path.split("/")
                pid, fid = int(bits[2]), int(bits[4])
                data, mime, name = self.store.read_attachment(pid, fid)
                safe = quote(name)
                inline = mime.startswith("image/") or mime == "application/pdf"
                disp = "inline" if inline else "attachment"
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'{disp}; filename="{safe}"')
                self.send_header("Cache-Control", "private, max-age=120")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/projects/"):
                self._send(200, self._project(int(path.split("/")[2])))
            elif path == "/search":
                self._send(200, self._search((qs.get("q") or [""])[0]))
            elif path == "/clients":
                self._send(200, self._clients())
            elif path == "/clients/new":
                self._send(200, self._client_form())
            elif path.startswith("/clients/") and path.endswith("/edit"):
                self._send(200, self._client_form(int(path.split("/")[2])))
            elif path.startswith("/clients/"):
                self._send(200, self._client_record(int(path.split("/")[2])))
            else:
                self._send(404, page("Not found", self._bar("Not found") + '<div class="content"><p>Not found.</p></div>'))
        except (ValueError, IndexError):
            self._send(400, page("Bad request", self._bar("Bad request") + '<div class="content"><p>Bad request.</p></div>'))

    def _read_multipart(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > ATTACH_MAX + 65536:
            raise ValueError("File is too large (12 MB max)")
        raw = self.rfile.read(length)
        blob = (
            b"Content-Type: "
            + self.headers.get("Content-Type", "").encode("utf-8", "replace")
            + b"\r\n\r\n"
            + raw
        )
        msg = message_from_bytes(blob, policy=EMAIL_HTTP)
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes, str]] = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename() or ""
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[str(name)] = (
                    filename,
                    payload if isinstance(payload, bytes) else bytes(payload),
                    part.get_content_type() or "",
                )
            else:
                if isinstance(payload, bytes):
                    fields[str(name)] = payload.decode("utf-8", "replace")
                else:
                    fields[str(name)] = str(payload)
        return fields, files

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            try:
                _fields, files = self._read_multipart()
                if path.startswith("/projects/") and path.endswith("/files"):
                    pid = int(path.split("/")[2])
                    if "file" not in files:
                        raise ValueError("Choose a file")
                    name, data, mime = files["file"]
                    self.store.add_attachment(pid, name, data, mime)
                    self._redirect(f"/projects/{pid}")
                    return
                raise ValueError("Unexpected upload")
            except ValueError as err:
                self._send(
                    400,
                    page("Error", self._bar("Error") + f'<div class="content"><p>{e(err)}</p></div>'),
                )
                return
        form = self._read_form()
        try:
            if path == "/projects/new":
                raw_cid = form.get("client_id", "").strip()
                cid = int(raw_cid) if raw_cid.isdigit() and int(raw_cid) else None
                pid = self.store.add_project(
                    form.get("name", ""),
                    form.get("client", ""),
                    lane=form.get("lane", "Planning") or "Planning",
                    health=form.get("health", "not_started") or "not_started",
                    priority=form.get("priority", "medium") or "medium",
                    client_id=cid,
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
                raw_cid = form.get("client_id", "").strip()
                cid = int(raw_cid) if raw_cid.isdigit() else 0
                self.store.update_project(
                    pid,
                    form.get("name", ""),
                    form.get("client", ""),
                    lane=form.get("lane"),
                    health=form.get("health"),
                    priority=form.get("priority"),
                    client_id=cid,
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
            elif path == "/columns":
                self.store.insert_column(
                    form.get("label", ""),
                    beside=form.get("beside", ""),
                    side=form.get("side", "right") or "right",
                )
                self._redirect("/")
            elif path.startswith("/columns/") and path.endswith("/rename"):
                self.store.rename_column(int(path.split("/")[2]), form.get("label", ""))
                self._redirect("/")
            elif path.startswith("/columns/") and path.endswith("/delete"):
                self.store.delete_column(int(path.split("/")[2]))
                self._redirect("/")
            elif path.startswith("/projects/") and "/files/" in path and path.endswith("/delete"):
                bits = path.split("/")
                self.store.delete_attachment(int(bits[2]), int(bits[4]))
                self._redirect(f"/projects/{bits[2]}")
            elif path == "/clients/new":
                cid = self.store.add_client(
                    form.get("name", ""),
                    **{k: form.get(k, "") for k, _ in CLIENT_FIELDS if k != "name"},
                )
                self._redirect(f"/clients/{cid}")
            elif path.startswith("/clients/") and path.endswith("/edit"):
                cid = int(path.split("/")[2])
                self.store.update_client(
                    cid,
                    form.get("name", ""),
                    **{k: form.get(k, "") for k, _ in CLIENT_FIELDS if k != "name"},
                )
                self._redirect(f"/clients/{cid}")
            else:
                self._send(404, page("Not found", self._bar("Not found") + '<div class="content"><p>Not found.</p></div>'))
        except ValueError as err:
            self._send(
                400,
                page("Error", self._bar("Error") + f'<div class="content"><p>{e(err)}</p></div>'),
            )

    def _home(self, qs: dict[str, list[str]] | None = None) -> bytes:
        qs = qs or {}
        grouped_filters: dict[str, dict[str, str]] = {}
        for key, vals in qs.items():
            if "." not in key:
                continue
            lane, colkey = key.split(".", 1)
            grouped_filters.setdefault(lane, {})[colkey] = vals[-1]

        def hidden_filters(except_lane: str = "", except_col: str = "") -> str:
            bits = []
            for lane, fmap in grouped_filters.items():
                for colkey, val in fmap.items():
                    if lane == except_lane and colkey == except_col:
                        continue
                    if not val:
                        continue
                    bits.append(
                        f'<input type="hidden" name="{e(lane)}.{e(colkey)}" value="{e(val)}">'
                    )
            return "".join(bits)

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

        def uniques(col: Any, projects: list[Any]) -> list[str]:
            seen: list[str] = []
            blank = False
            for p in projects:
                text = self.store.column_text(p, col)
                if not text.strip():
                    blank = True
                    continue
                if text not in seen:
                    seen.append(text)
            seen.sort(key=str.lower)
            if blank:
                seen.append("(blank)")
            return seen

        def header_cell(col: Any, lane: str, projects: list[Any], current: dict[str, str]) -> str:
            opts = ['<option value="">All</option>']
            chosen = current.get(col["key"], "")
            for val in uniques(col, projects):
                sel = " selected" if chosen == val else ""
                label = val if val != "(blank)" else "(blank)"
                opts.append(f'<option value="{e(val)}"{sel}>{e(label)}</option>')
            remove = ""
            if not col["builtin"]:
                remove = (
                    f'<form method="post" action="/columns/{col["id"]}/delete">'
                    f'<button class="btn ghost" type="submit">Remove column</button></form>'
                )
            return f"""<th><div class="th-wrap"><span>{e(col["label"])}</span>
              <details class="colmenu"><summary>▾</summary>
              <div class="menu">
                <form method="post" action="/columns/{col["id"]}/rename">
                  <label>Rename <input name="label" value="{e(col["label"])}" required></label>
                  <button type="submit">Save name</button>
                </form>
                <form method="post" action="/columns">
                  <input type="hidden" name="beside" value="{e(col["key"])}">
                  <input type="hidden" name="side" value="left">
                  <label>Add column left <input name="label" placeholder="Name" required></label>
                  <button type="submit">Add left</button>
                </form>
                <form method="post" action="/columns">
                  <input type="hidden" name="beside" value="{e(col["key"])}">
                  <input type="hidden" name="side" value="right">
                  <label>Add column right <input name="label" placeholder="Name" required></label>
                  <button type="submit">Add right</button>
                </form>
                <form method="get" action="/">
                  {hidden_filters(lane, col["key"])}
                  <label>Filter
                    <select name="{e(lane)}.{e(col["key"])}" onchange="this.form.submit()">
                    {"".join(opts)}</select>
                  </label>
                </form>
                {remove}
              </div></details></div></th>"""

        columns = self.store.list_columns()
        groups = []
        for lane, projects in self.store.board().items():
            current = grouped_filters.get(lane, {})
            visible = [p for p in projects if self.store.matches(p, current)]
            rows = []
            for p in visible:
                values = self.store.cells_for(p["id"])
                rows.append("<tr>" + "".join(cell(c, p, values) for c in columns) + "</tr>")
            headers = "".join(header_cell(c, lane, projects, current) for c in columns)
            filter_bits = [hidden_filters(lane)]
            for col in columns:
                opts = ['<option value="">All</option>']
                chosen = current.get(col["key"], "")
                for val in uniques(col, projects):
                    sel = " selected" if chosen == val else ""
                    opts.append(f'<option value="{e(val)}"{sel}>{e(val)}</option>')
                filter_bits.append(
                    f'<label>{e(col["label"])} '
                    f'<select name="{e(lane)}.{e(col["key"])}" onchange="this.form.submit()">'
                    f'{"".join(opts)}</select></label>'
                )
            clear = f'<a class="btn ghost" href="/">Clear filters</a>' if current else ""
            bar = (
                f'<form class="filters" method="get" action="/">{"".join(filter_bits)}'
                f"{clear}</form>"
            )
            cls = lane if lane in LANES else "other"
            add = f"""
            <form class="add-row" method="post" action="/projects/new">
              <input type="hidden" name="lane" value="{e(lane)}">
              <input type="hidden" name="stay" value="board">
              <input name="name" placeholder="+ Add project" required>
              <button type="submit">Add</button>
            </form>"""
            shown = f"{len(visible)}/{len(projects)}" if current else str(len(projects))
            groups.append(
                f'<section class="group {e(cls)}"><h2>{e(lane)}</h2>{bar}'
                f'<div class="board"><table><tr>{headers}</tr>'
                f"{''.join(rows)}</table>{add}</div>"
                f'<p class="muted">{shown} rows</p></section>'
            )
        extra = '<a class="btn" href="/projects/new">New project</a>'
        toolbar = (
            '<div class="toolbar"><span>Board</span>'
            '<span class="muted">▾ on a column: rename, add left/right, or filter. '
            "Each section has its own filters.</span></div>"
        )
        body = self._bar("Projects", extra) + toolbar + "".join(groups)
        return page("Projects", body, "projects")

    def _clients(self) -> bytes:
        rows = []
        for c in self.store.list_clients():
            who = c["contact_name"] or "—"
            rows.append(
                f"<tr><td><a href='/clients/{c['id']}'>{e(c['name'])}</a></td>"
                f"<td>{e(c['city'])}</td><td>{e(who)}</td>"
                f"<td>{e(c['phone'])}</td><td>{e(c['email'])}</td>"
                f"<td>{c['project_count']}</td></tr>"
            )
        table = (
            "<table><tr><th>Company</th><th>City</th><th>Contact</th><th>Phone</th><th>Email</th><th>Projects</th></tr>"
            + ("".join(rows) or "<tr><td colspan='6' class='muted'>No clients yet.</td></tr>")
            + "</table>"
        )
        extra = '<a class="btn" href="/clients/new">New client</a>'
        body = self._bar("Clients", extra) + f'<div class="content">{table}</div>'
        return page("Clients", body, "clients")

    def _client_form(self, client_id: int | None = None) -> bytes:
        c = self.store.get_client(client_id) if client_id else None
        if client_id and c is None:
            return page("Not found", self._bar("Not found") + '<div class="content"><p>Client not found.</p></div>')
        action = f"/clients/{client_id}/edit" if c else "/clients/new"
        heading = "Edit client" if c else "New client"
        bits = []
        for key, label in CLIENT_FIELDS:
            val = c[key] if c else ""
            if key == "notes":
                bits.append(f'<label>{e(label)} <textarea name="{key}">{e(val)}</textarea></label>')
            elif key == "email" or key == "billing_email" or key == "contact_email":
                bits.append(
                    f'<label>{e(label)} <input name="{key}" type="email" value="{e(val)}"></label>'
                )
            elif key == "website":
                bits.append(f'<label>{e(label)} <input name="{key}" value="{e(val)}" placeholder="https://"></label>')
            elif key == "name":
                bits.append(f'<label>{e(label)} <input name="{key}" required value="{e(val)}"></label>')
            else:
                bits.append(f'<label>{e(label)} <input name="{key}" value="{e(val)}"></label>')
        form = f'<form class="stack" method="post" action="{action}">{"".join(bits)}<button type="submit">Save profile</button></form>'
        body = self._bar(heading) + f'<div class="content">{form}</div>'
        return page(heading, body, "clients")

    def _client_record(self, client_id: int) -> bytes:
        c = self.store.get_client(client_id)
        if c is None:
            return page("Not found", self._bar("Not found") + '<div class="content"><p>Client not found.</p></div>')
        extra = (
            f'<a class="btn ghost" href="/clients/{client_id}/edit">Edit</a>'
            f'<a class="btn" href="/projects/new">New project</a>'
        )
        skip = {"id", "created_at", "updated_at", "name"}
        props = []
        for key, label in CLIENT_FIELDS:
            if key in skip:
                continue
            val = c[key]
            if not val:
                continue
            props.append(f'<p class="muted">{e(label)}</p><p>{e(val)}</p>')
        jobs = "".join(
            f"<tr><td><a href='/projects/{j['id']}'>{e(j['title'] if 'title' in j.keys() else j['name'])}</a></td>"
            f"<td>{e(j['lane'])}</td><td>{e(HEALTH.get(j['health'], j['health']))}</td></tr>"
            for j in self.store.projects_for_client(client_id)
        ) or "<tr><td colspan='3' class='muted'>No projects yet.</td></tr>"
        body = self._bar(c["name"], extra) + f"""
        <div class="content record">
          <div class="box">
            <h2>Profile</h2>
            {"".join(props) or '<p class="muted">No extra details yet.</p>'}
          </div>
          <div class="box">
            <h2>Projects</h2>
            <table><tr><th>Project</th><th>Group</th><th>Status</th></tr>{jobs}</table>
          </div>
        </div>"""
        return page(c["name"], body, "clients")

    def _project_form(self, project_id: int | None = None) -> bytes:
        p = self.store.get_project(project_id) if project_id else None
        if project_id and p is None:
            return page("Not found", self._bar("Not found") + '<div class="content"><p>Project not found.</p></div>')
        action = f"/projects/{project_id}/edit" if p else "/projects/new"
        heading = "Edit project" if p else "New project"
        lane = p["lane"] if p else "Planning"
        health = p["health"] if p else "not_started"
        priority = p["priority"] if p else "medium"
        selected_cid = p["client_id"] if p else None
        client_opts = ['<option value="0">No client profile</option>']
        for c in self.store.list_clients():
            sel = " selected" if selected_cid == c["id"] else ""
            client_opts.append(f'<option value="{c["id"]}"{sel}>{e(c["name"])}</option>')
        status_field = (
            ""
            if p
            else '<label>Opening status <textarea name="status" placeholder="Where things stand today."></textarea></label>'
        )
        form = f"""
        <form class="stack" method="post" action="{action}">
          <label>Project name <input name="name" required value="{e(p['name'] if p else '')}"></label>
          <label>Client profile <select name="client_id">{''.join(client_opts)}</select></label>
          <p class="muted"><a href="/clients/new">New client profile</a></p>
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
        items = []
        for a in self.store.list_attachments(project_id):
            href = f"/projects/{project_id}/files/{a['id']}"
            if str(a["mime"]).startswith("image/"):
                preview = f'<a href="{href}"><img src="{href}" alt="{e(a["name"])}"></a>'
            else:
                preview = f'<a href="{href}">{e(a["name"])}</a>'
            items.append(
                f'<div class="item">{preview}'
                f'<p class="muted">{e(a["name"])} · {int(a["size"])} bytes</p>'
                f'<form method="post" action="/projects/{project_id}/files/{a["id"]}/delete">'
                f'<button class="btn ghost" type="submit">Remove</button></form></div>'
            )
        files_html = f"""
            <div class="box" style="margin-bottom:16px">
              <h2>Attachments</h2>
              <p class="muted">PDF, pictures, TXT, CSV. Stays on this computer (12 MB each).</p>
              <form class="stack" method="post" action="/projects/{project_id}/files" enctype="multipart/form-data">
                <label>Add file <input type="file" name="file" required accept=".pdf,.png,.jpg,.jpeg,.gif,.webp,.txt,.csv"></label>
                <button type="submit">Upload</button>
              </form>
              <div class="files">{"".join(items) or '<p class="faint">No files yet.</p>'}</div>
            </div>"""
        body = self._bar(p["name"], extra) + f"""
        <div class="content">
          {f'<p class="muted">For <a href="/clients/{p["client_id"]}">{e(p["client"])}</a></p>' if p["client_id"] else (f'<p class="muted">For {e(p["client"])}</p>' if p["client"] else '')}
          {block_html}
          {files_html}
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
