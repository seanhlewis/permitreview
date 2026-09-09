#!/usr/bin/env python3
"""Live backend for the static permit bundle.

The permit records remain static JSON. This server stores reviewer state only.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import time
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
DATA_PATH = APP_ROOT / "data" / "permits.json"
MANIFEST_PATH = APP_ROOT / "data" / "manifest.json"
DB_PATH = Path(os.environ.get("STATICVER_DB", str(APP_ROOT / "instance" / "staticver.sqlite3"))).resolve()
HOST = os.environ.get("STATICVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATICVER_PORT", "8873"))
MAX_REVIEWERS = 2
MAX_BODY_BYTES = 2 * 1024 * 1024


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def reviewer_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def reviewer_name(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value or len(value) > 160 or any(ord(char) < 32 for char in value):
        raise ValueError("Enter a valid reviewer name")
    if not reviewer_key(value):
        raise ValueError("Enter a valid reviewer name")
    return value


def reviewer_initial(value: str) -> str:
    return reviewer_name(value)[0].upper()


def task_key(category: str, permit_id: str) -> str:
    return f"{category}::{permit_id}"


with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
    MANIFEST = json.load(handle)
with DATA_PATH.open("r", encoding="utf-8") as handle:
    PERMITS = json.load(handle)["permits"]
TASKS = {(str(row["category"]), str(row["id"])): row for row in PERMITS}
CATEGORIES = {str(row["code"]): row for row in MANIFEST["categories"]}


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS category_assignments (
            category TEXT NOT NULL,
            reviewer_key TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            initial TEXT NOT NULL,
            first_answer_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            answer_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (category, reviewer_key)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            category TEXT NOT NULL,
            permit_id TEXT NOT NULL,
            reviewer_key TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            initial TEXT NOT NULL,
            answer TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            answered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (category, permit_id, reviewer_key)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS drafts (
            category TEXT NOT NULL,
            permit_id TEXT NOT NULL,
            reviewer_key TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (category, permit_id, reviewer_key)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS timing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            permit_id TEXT NOT NULL,
            reviewer_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_reviews_category ON reviews(category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_reviewer ON timing_events(reviewer_key)")
    con.commit()
    return con


def validate_task(category: str, permit_id: str) -> dict:
    permit = TASKS.get((str(category), str(permit_id)))
    if permit is None:
        raise ValueError("That permit is not in the current static bundle")
    return permit


def safe_elapsed(value) -> int:
    try:
        return max(0, min(int(value or 0), 24 * 60 * 60 * 1000))
    except (TypeError, ValueError):
        return 0


def category_reviewer_rows(con: sqlite3.Connection, category: str):
    return con.execute(
        "SELECT reviewer_key, initial FROM category_assignments WHERE category = ? ORDER BY first_answer_at",
        (category,),
    ).fetchall()


def live_state(name: str) -> dict:
    name = reviewer_name(name) if name else ""
    key = reviewer_key(name)
    con = connect_db()
    try:
        assignments = defaultdict(list)
        for row in con.execute("SELECT category, reviewer_key, initial FROM category_assignments"):
            assignments[row["category"]].append(dict(row))
        claims = defaultdict(list)
        for row in con.execute("SELECT category, permit_id, reviewer_key, initial FROM reviews"):
            claims[task_key(row["category"], row["permit_id"])].append(row["initial"])
        mine = {}
        for row in con.execute(
            "SELECT category, permit_id, answer, comment, started_at, answered_at, updated_at, elapsed_ms FROM reviews WHERE reviewer_key = ?",
            (key,),
        ):
            mine[task_key(row["category"], row["permit_id"])] = {
                "answer": row["answer"],
                "comment": row["comment"],
                "startedAt": row["started_at"],
                "answeredAt": row["answered_at"],
                "updatedAt": row["updated_at"],
                "elapsedMs": row["elapsed_ms"],
            }
        drafts = {}
        for row in con.execute(
            "SELECT category, permit_id, comment, started_at, saved_at, elapsed_ms FROM drafts WHERE reviewer_key = ?",
            (key,),
        ):
            drafts[task_key(row["category"], row["permit_id"])] = {
                "comment": row["comment"],
                "startedAt": row["started_at"],
                "savedAt": row["saved_at"],
                "elapsedMs": row["elapsed_ms"],
            }
        categories = []
        for code, meta in CATEGORIES.items():
            rows = assignments[code]
            reviewer_keys = {row["reviewer_key"] for row in rows}
            initials = [row["initial"] for row in rows]
            locked = len(reviewer_keys) >= MAX_REVIEWERS and key not in reviewer_keys
            categories.append(
                {
                    "code": code,
                    "label": meta["label"],
                    "permitCount": meta["permitCount"],
                    "reviewerInitials": initials,
                    "lockedForCurrent": locked,
                    "lockedTo": initials if locked else [],
                    "myAnswered": sum(1 for task in mine if task.startswith(code + "::")),
                }
            )
        return {
            "ok": True,
            "reviewer": name,
            "reviewerInitial": reviewer_initial(name) if name else "",
            "categories": categories,
            "claims": {key_name: sorted(set(values)) for key_name, values in claims.items()},
            "mine": mine,
            "drafts": drafts,
            "serverTime": utc_now(),
        }
    finally:
        con.close()


def save_draft(payload: dict) -> dict:
    name = reviewer_name(payload.get("reviewerName"))
    key = reviewer_key(name)
    category = str(payload.get("category") or "")
    permit_id = str(payload.get("permitId") or "")
    validate_task(category, permit_id)
    now = utc_now()
    elapsed = safe_elapsed(payload.get("elapsedMs"))
    started = str(payload.get("startedAt") or "")[:64] or None
    comment = str(payload.get("comment") or "")[:4000]
    con = connect_db()
    try:
        con.execute(
            """
            INSERT INTO drafts(category, permit_id, reviewer_key, reviewer_name, comment, started_at, elapsed_ms, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, permit_id, reviewer_key) DO UPDATE SET
              reviewer_name=excluded.reviewer_name, comment=excluded.comment,
              started_at=COALESCE(drafts.started_at, excluded.started_at),
              elapsed_ms=excluded.elapsed_ms, saved_at=excluded.saved_at
            """,
            (category, permit_id, key, name, comment, started, elapsed, now),
        )
        con.execute(
            "INSERT INTO timing_events(category, permit_id, reviewer_key, event_type, event_at, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (category, permit_id, key, "draft_saved", now, elapsed),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "savedAt": now, "elapsedMs": elapsed}


def save_review(payload: dict) -> dict:
    name = reviewer_name(payload.get("reviewerName"))
    key = reviewer_key(name)
    category = str(payload.get("category") or "")
    permit_id = str(payload.get("permitId") or "")
    validate_task(category, permit_id)
    answer = str(payload.get("answer") or "").lower()
    if answer not in {"yes", "no", "unclear"}:
        raise ValueError("Choose Yes, No, or Unclear")
    comment = str(payload.get("comment") or "")[:4000]
    elapsed = safe_elapsed(payload.get("elapsedMs"))
    started = str(payload.get("startedAt") or "")[:64] or None
    now = utc_now()
    initial = reviewer_initial(name)
    con = connect_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        category_rows = category_reviewer_rows(con, category)
        category_keys = {row["reviewer_key"] for row in category_rows}
        if key not in category_keys and len(category_keys) >= MAX_REVIEWERS:
            con.rollback()
            return {"ok": False, "status": 409, "error": "category_locked", "lockedTo": [row["initial"] for row in category_rows]}
        permit_rows = con.execute(
            "SELECT reviewer_key, initial FROM reviews WHERE category = ? AND permit_id = ? ORDER BY answered_at",
            (category, permit_id),
        ).fetchall()
        permit_keys = {row["reviewer_key"] for row in permit_rows}
        if key not in permit_keys and len(permit_keys) >= MAX_REVIEWERS:
            con.rollback()
            return {"ok": False, "status": 409, "error": "permit_locked", "lockedTo": [row["initial"] for row in permit_rows]}
        con.execute(
            """
            INSERT INTO category_assignments(category, reviewer_key, reviewer_name, initial, first_answer_at, last_activity_at, answer_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(category, reviewer_key) DO UPDATE SET
              reviewer_name=excluded.reviewer_name, last_activity_at=excluded.last_activity_at,
              answer_count=category_assignments.answer_count + 1
            """,
            (category, key, name, initial, now, now),
        )
        draft = con.execute(
            "SELECT started_at FROM drafts WHERE category = ? AND permit_id = ? AND reviewer_key = ?",
            (category, permit_id, key),
        ).fetchone()
        started = started or (draft["started_at"] if draft else None)
        con.execute(
            """
            INSERT INTO reviews(category, permit_id, reviewer_key, reviewer_name, initial, answer, comment, started_at, answered_at, updated_at, elapsed_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, permit_id, reviewer_key) DO UPDATE SET
              reviewer_name=excluded.reviewer_name, initial=excluded.initial, answer=excluded.answer,
              comment=excluded.comment, started_at=COALESCE(reviews.started_at, excluded.started_at),
              updated_at=excluded.updated_at, elapsed_ms=excluded.elapsed_ms
            """,
            (category, permit_id, key, name, initial, answer, comment, started, now, now, elapsed),
        )
        con.execute(
            "DELETE FROM drafts WHERE category = ? AND permit_id = ? AND reviewer_key = ?",
            (category, permit_id, key),
        )
        con.execute(
            "INSERT INTO timing_events(category, permit_id, reviewer_key, event_type, event_at, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (category, permit_id, key, "answer_saved", now, elapsed),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "savedAt": now, "elapsedMs": elapsed, "initial": initial}


def reset_reviewer(payload: dict) -> dict:
    name = reviewer_name(payload.get("reviewerName"))
    key = reviewer_key(name)
    con = connect_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        reviews = con.execute("SELECT COUNT(*) FROM reviews WHERE reviewer_key = ?", (key,)).fetchone()[0]
        drafts = con.execute("SELECT COUNT(*) FROM drafts WHERE reviewer_key = ?", (key,)).fetchone()[0]
        con.execute("DELETE FROM reviews WHERE reviewer_key = ?", (key,))
        con.execute("DELETE FROM drafts WHERE reviewer_key = ?", (key,))
        con.execute("DELETE FROM category_assignments WHERE reviewer_key = ?", (key,))
        con.execute("DELETE FROM timing_events WHERE reviewer_key = ?", (key,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "deletedReviews": reviews, "deletedDrafts": drafts}


class Handler(BaseHTTPRequestHandler):
    server_version = "StaticVerLive/1.0"

    def log_message(self, format, *args):
        print(f"[{utc_now()}] {self.address_string()} {format % args}", flush=True)

    def send_json(self, payload: dict, status: int = 200):
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path):
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Invalid request body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            con = connect_db()
            try:
                reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
                assignments = con.execute("SELECT COUNT(*) FROM category_assignments").fetchone()[0]
            finally:
                con.close()
            self.send_json({"ok": True, "service": "staticver-live", "port": PORT, "reviews": reviews, "categoryAssignments": assignments, "db": str(DB_PATH)})
            return
        if parsed.path == "/api/state":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                self.send_json(live_state(params.get("reviewer", [""])[0]))
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, 400)
            return
        route = "/index.html" if parsed.path == "/" else "/showcase.html" if parsed.path == "/test" else parsed.path
        relative = route.lstrip("/")
        if ".." in Path(relative).parts or not relative or relative.startswith("instance") or relative == "server.py":
            self.send_error(404)
            return
        path = (APP_ROOT / relative).resolve()
        if APP_ROOT not in path.parents and path != APP_ROOT:
            self.send_error(404)
            return
        if path.exists() and path.is_file():
            self.send_file(path)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/drafts":
                result = save_draft(payload)
                self.send_json(result)
            elif parsed.path == "/api/reviews":
                result = save_review(payload)
                self.send_json(result, int(result.get("status", 200)))
            elif parsed.path == "/api/reset":
                self.send_json(reset_reviewer(payload))
            else:
                self.send_error(404)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"ok": False, "error": str(error)}, 400)
        except sqlite3.Error as error:
            self.send_json({"ok": False, "error": "Database error", "detail": str(error)}, 500)


def main():
    connect_db().close()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"StaticVer live server listening on http://{HOST}:{PORT}", flush=True)
    print(f"Database: {DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping staticver server", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
