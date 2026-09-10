#!/usr/bin/env python3
"""Dependency-free 177M CANDY human evaluation webapp.

Runs on cluster Python 3.6+ using only the standard library.
"""

import csv
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn


APP_DIR = Path(__file__).resolve().parent
STATIC_PERMITS_PATH = Path(os.environ.get("STATICVER_PERMITS", str(APP_DIR / "data" / "permits.json"))).resolve()
STATIC_MANIFEST_PATH = Path(os.environ.get("STATICVER_MANIFEST", str(APP_DIR / "data" / "manifest.json"))).resolve()
VALIDATION_ROOT = Path(os.environ.get("STATICVER_ARTIFACT_ROOT", str(APP_DIR / "instance" / "artifacts"))).resolve()
HOST = os.environ.get("STATICVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATICVER_PORT", "8873"))
DB_PATH = Path(os.environ.get("STATICVER_DB", str(APP_DIR / "instance" / "human_eval.sqlite3"))).resolve()
TAXONOMY_PATH = Path(os.environ.get("STATICVER_TAXONOMY", str(APP_DIR / "taxonomy_catalog.json"))).resolve()
BLIND_SAMPLES = ()
# Historical V3 cards used reviewer slots. Those cards are now locked.
# Active V4 targeted queues are class-organized and visible in full to every reviewer.
# The model's guess remains hidden during active review.
FAMILIES = {
    "Electrical": ["SOLAR_PV", "EV_CHARGER", "BATTERY_STORAGE", "ELECTRICAL_PANEL_UPGRADE", "ELECTRICAL"],
    "Mechanical and HVAC": ["HEAT_PUMP", "MECHANICAL_HVAC"],
    "Plumbing": ["WATER_HEATER", "GAS", "PLUMBING"],
    "Exterior": ["ROOFING", "WINDOW_DOOR", "SIGN", "DECK_PATIO_PORCH"],
    "Building": ["ALTERATION_REMODEL", "NEW_CONSTRUCTION", "DEMOLITION", "GARAGE_ACCESSORY", "STRUCTURAL"],
    "Sitework": ["POOL_SPA", "FENCE_WALL", "DRIVEWAY_PAVING", "GRADING_SITEWORK", "SEWER_UTILITY"],
    "Life safety": ["FIRE_PROTECTION"],
}
FAM_TASK = {
    "taxval_v3_electrical": ("Electrical", "Electrical & electrification"),
    "taxval_v3_mechanical": ("Mechanical and HVAC", "Mechanical & HVAC"),
    "taxval_v3_plumbing": ("Plumbing", "Plumbing & gas"),
    "taxval_v3_exterior": ("Exterior", "Exterior"),
    "taxval_v3_building": ("Building", "Building & structural"),
    "taxval_v3_sitework": ("Sitework", "Sitework & pools"),
    "taxval_v3_lifesafety": ("Life safety", "Life safety"),
}
ANCHOR_TASK = "taxval_v3_anchor"
HISTORICAL_TASK_SAMPLES = ()

# Targeted class-support queues built from the balanced challenge candidate file.
# Each queue is a separate review card. A permit may appear in more than one queue
# when it supports more than one class-specific estimate. New queues have no
# reviewer-slot assignment.
TARGET_CLASS_FAMILIES = {
    "ALTERATION_REMODEL": "Building",
    "BATTERY_STORAGE": "Electrical",
    "DECK_PATIO_PORCH": "Exterior",
    "DEMOLITION": "Building",
    "DRIVEWAY_PAVING": "Sitework",
    "ELECTRICAL_PANEL_UPGRADE": "Electrical",
    "EV_CHARGER": "Electrical",
    "FENCE_WALL": "Sitework",
    "FIRE_PROTECTION": "Life safety",
    "GARAGE_ACCESSORY": "Building",
    "GAS": "Plumbing",
    "GRADING_SITEWORK": "Sitework",
    "MECHANICAL_HVAC": "Mechanical and HVAC",
    "NEW_CONSTRUCTION": "Building",
    "PLUMBING": "Plumbing",
    "ROOFING": "Exterior",
    "SEWER_UTILITY": "Sitework",
    "SIGN": "Exterior",
    "STRUCTURAL": "Building",
    "WATER_HEATER": "Plumbing",
    "WINDOW_DOOR": "Exterior",
}
TARGET_CLASS_TITLES = {
    "ALTERATION_REMODEL": "Alteration / remodel",
    "BATTERY_STORAGE": "Battery storage",
    "DECK_PATIO_PORCH": "Deck / patio / porch",
    "DEMOLITION": "Demolition",
    "DRIVEWAY_PAVING": "Driveway / paving",
    "ELECTRICAL_PANEL_UPGRADE": "Electrical panel upgrade",
    "EV_CHARGER": "EV charger",
    "FENCE_WALL": "Fence / wall",
    "FIRE_PROTECTION": "Fire protection",
    "GARAGE_ACCESSORY": "Garage / accessory",
    "GAS": "Gas",
    "GRADING_SITEWORK": "Grading / sitework",
    "MECHANICAL_HVAC": "Mechanical / HVAC",
    "NEW_CONSTRUCTION": "New construction",
    "PLUMBING": "Plumbing",
    "ROOFING": "Roofing",
    "SEWER_UTILITY": "Sewer / utility",
    "SIGN": "Sign",
    "STRUCTURAL": "Structural",
    "WATER_HEATER": "Water heater",
    "WINDOW_DOOR": "Window / door",
}
TARGET_TASKS = tuple("taxval_v4_targeted_" + label.lower() for label in TARGET_CLASS_FAMILIES)
TARGET_LABEL_BY_TASK = dict(zip(TARGET_TASKS, TARGET_CLASS_FAMILIES.keys()))
ACTIVE_REVIEW_WORKLOAD = 50 * len(TARGET_CLASS_FAMILIES)
MIN_REVIEW_PER_CLASS = 50
MAX_REVIEWERS_PER_TASK = 2
TASK_SAMPLES = TARGET_TASKS
TASK_TITLES = {k: v[1] for k, v in FAM_TASK.items()}
TASK_TITLES[ANCHOR_TASK] = "Random mix (all categories)"
TASK_TITLES.update({sample: "Targeted review · " + TARGET_CLASS_TITLES[label]
                    for sample, label in TARGET_LABEL_BY_TASK.items()})
TASK_FAMILY = {k: v[0] for k, v in FAM_TASK.items()}
TASK_FAMILY.update({sample: TARGET_CLASS_FAMILIES[label]
                    for sample, label in TARGET_LABEL_BY_TASK.items()})
TASK_MODE = "narrow"
SAMPLES = TASK_SAMPLES
MAX_UPLOAD_BYTES = 80 * 1024 * 1024


def load_static_target_rows():
    """Adapt the exported static permit bundle to the original queue schema."""
    try:
        payload = json.loads(STATIC_PERMITS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    grouped = {}
    for permit in payload.get("permits", []):
        category = str(permit.get("category") or "").strip()
        permit_id = str(permit.get("id") or "").strip()
        if not category or not permit_id or category not in TARGET_CLASS_FAMILIES:
            continue
        sample = "taxval_v4_targeted_" + category.lower()
        grouped.setdefault(sample, []).append({
            "review_id": permit_id,
            "permit_number": permit.get("permitNumber", ""),
            "description": permit.get("description", ""),
            "source_city": permit.get("city", ""),
            "source_state": permit.get("state", ""),
            "county": permit.get("county", ""),
            "jurisdiction": permit.get("jurisdiction", ""),
            "permit_type": permit.get("permitType", ""),
            "work_type": permit.get("workType", ""),
            "candidate_rank": permit.get("selectionScore", 0),
        })
    return grouped


STATIC_TARGET_ROWS = load_static_target_rows()


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dirs():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    (VALIDATION_ROOT / "human_review_exports").mkdir(parents=True, exist_ok=True)
    (VALIDATION_ROOT / "reports").mkdir(parents=True, exist_ok=True)


def connect_db():
    ensure_dirs()
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            sample_name TEXT NOT NULL,
            review_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            chosen_labels_json TEXT NOT NULL,
            chosen_source TEXT NOT NULL,
            reason_tag TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            review_time_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (sample_name, review_id, reviewer, mode)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reviewer_slots (
            reviewer TEXT PRIMARY KEY,
            slot INTEGER NOT NULL,
            assigned_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_name TEXT NOT NULL,
            review_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            chosen_labels_json TEXT NOT NULL,
            chosen_source TEXT NOT NULL,
            reason_tag TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            review_time_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS active_assignments (
            sample_name TEXT NOT NULL,
            review_id TEXT NOT NULL,
            reviewer_key TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            assignment_kind TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (sample_name, review_id, reviewer_key)
        )
        """
    )
    assignment_columns = {
        row["name"] for row in con.execute("PRAGMA table_info(active_assignments)").fetchall()
    }
    if "completed_at" not in assignment_columns:
        con.execute("ALTER TABLE active_assignments ADD COLUMN completed_at TEXT")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_assignments_reviewer ON active_assignments (reviewer_key)"
    )
    con.commit()
    return con


def read_csv_rows(path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def split_flags(value):
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    seen = []
    for part in re.split(r"[;,|]", text):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def json_loads(value, fallback):
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return fallback


def sample_paths(sample):
    return {
        "blinded": VALIDATION_ROOT / "human_review_exports" / (sample + "_for_review_blinded.csv"),
        "joined": VALIDATION_ROOT / "human_review_exports" / (sample + "_source_joined.csv"),
        "answers_csv": VALIDATION_ROOT / "validation_samples" / (sample + "_model_answers.csv"),
        "answers_jsonl": VALIDATION_ROOT / "validation_samples" / (sample + "_model_answers.jsonl"),
        "export": VALIDATION_ROOT / "human_review_exports" / (sample + "_webapp_reviews.csv"),
        "report": VALIDATION_ROOT / "reports" / (sample + "_webapp_scored_report.md"),
        "summary": VALIDATION_ROOT / "reports" / (sample + "_webapp_scored_summary.json"),
        "verdicts": VALIDATION_ROOT / "reports" / (sample + "_webapp_scored_row_verdicts.csv"),
    }


def timing_paths():
    return {
        "events_csv": VALIDATION_ROOT / "reports" / "webapp_review_timing_events.csv",
        "summary_csv": VALIDATION_ROOT / "reports" / "webapp_reviewer_time_summary.csv",
        "summary_json": VALIDATION_ROOT / "reports" / "webapp_reviewer_time_summary.json",
    }


def infer_sample_size(sample):
    match = re.search(r"blind_random_(\d+)_", sample)
    return int(match.group(1)) if match else 0


def load_answers(sample):
    path = sample_paths(sample)["answers_csv"]
    rows = {}
    if not path.exists():
        return rows
    for row in read_csv_rows(path):
        rows[row.get("review_id", "")] = row
    return rows


def load_joined_items(sample):
    if sample in TASK_SAMPLES:
        rows = STATIC_TARGET_ROWS.get(sample, [])
        return rows, str(STATIC_PERMITS_PATH), bool(rows and any(description_for_row(row) for row in rows))
    paths = sample_paths(sample)
    source_path = paths["joined"] if paths["joined"].exists() else paths["blinded"]
    if not source_path.exists():
        return [], str(source_path), False
    rows = read_csv_rows(source_path)
    has_reviewable_descriptions = any(description_for_row(row) for row in rows)
    return rows, str(source_path), has_reviewable_descriptions


def reviewable_items(sample):
    rows, source_path, has_reviewable_descriptions = load_joined_items(sample)
    if sample in TASK_SAMPLES:
        # Keep the exported 50-row target queue intact. Reviewers can mark a
        # blank description as skipped through the same prior-app workflow.
        return rows, source_path, bool(rows)
    return [row for row in rows if description_for_row(row)], source_path, has_reviewable_descriptions


def reviewer_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def canonical_reviewer_name(value):
    return str(value or "").strip()


def row_value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def review_is_finished(row):
    """Return whether a saved row represents a completed categorization."""
    status = str(row_value(row, "status", "") or "").strip().lower()
    chosen_source = str(row_value(row, "chosen_source", "") or "").strip().lower()
    if status == "denied":
        return True
    if status != "reviewed" or chosen_source == "autosave":
        return False
    try:
        labels = json.loads(row_value(row, "chosen_labels_json", "[]") or "[]")
    except (TypeError, ValueError):
        labels = []
    return bool(labels) or chosen_source == "none"


def review_occupies_slot(row):
    """Return whether a saved active review occupies one reviewer slot.

    Completed categorizations and genuinely started autosaves reserve a slot.
    Never-opened dashboard assignments do not reserve one.
    """
    if review_is_finished(row):
        return True
    status = str(row_value(row, "status", "") or "").strip().lower()
    chosen_source = str(row_value(row, "chosen_source", "") or "").strip().lower()
    return status == "in_progress" or chosen_source == "autosave"


def normalize_assignment_completion(con):
    """Clear stale completion markers for pending or uncategorized work."""
    rows = con.execute(
        """
        SELECT a.sample_name, a.review_id, a.reviewer_key, a.completed_at,
               r.status, r.chosen_labels_json, r.chosen_source, r.updated_at
        FROM active_assignments AS a
        LEFT JOIN reviews AS r
          ON r.sample_name = a.sample_name
         AND r.review_id = a.review_id
         AND r.reviewer = a.reviewer_name
        WHERE a.sample_name IN (%s)
        """ % ",".join("?" for _ in TASK_SAMPLES),
        TASK_SAMPLES,
    ).fetchall()
    for row in rows:
        completed_at = row["updated_at"] if review_is_finished(row) else None
        if row["completed_at"] != completed_at:
            con.execute(
                """
                UPDATE active_assignments
                SET completed_at = ?
                WHERE sample_name = ? AND review_id = ? AND reviewer_key = ?
                """,
                (completed_at, row["sample_name"], row["review_id"], row["reviewer_key"]),
            )


def release_unstarted_assignments(con, reviewer_key_value=None):
    """Release provisional rows that have no saved review yet."""
    rows = con.execute(
        """
        SELECT sample_name, review_id, reviewer_key, reviewer_name
        FROM active_assignments
        WHERE sample_name IN (%s)
        """ % ",".join("?" for _ in TASK_SAMPLES),
        TASK_SAMPLES,
    ).fetchall()
    for row in rows:
        saved = con.execute(
            """
            SELECT status, chosen_labels_json, chosen_source
            FROM reviews
            WHERE sample_name = ? AND review_id = ? AND reviewer = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (row["sample_name"], row["review_id"], row["reviewer_name"]),
        ).fetchone()
        if saved is None:
            con.execute(
                """
                DELETE FROM active_assignments
                WHERE sample_name = ? AND review_id = ? AND reviewer_key = ?
                """,
                (row["sample_name"], row["review_id"], row["reviewer_key"]),
            )


def active_queue_rows():
    """Return active rows in a class-interleaved order for fair allocation."""
    by_label = {}
    for sample in TASK_SAMPLES:
        items, _, _ = reviewable_items(sample)
        label = TARGET_LABEL_BY_TASK.get(sample, sample)
        by_label.setdefault(label, []).extend((sample, row) for row in items)
    for label in by_label:
        by_label[label].sort(key=lambda pair: (int(pair[1].get("candidate_rank") or 0), pair[1].get("review_id", "")))
    ordered = []
    labels = sorted(by_label)
    index = 0
    while True:
        added = False
        for label in labels:
            if index < len(by_label[label]):
                ordered.append(by_label[label][index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def backfill_active_assignments(con):
    """Record any active reviews saved before the assignment table existed."""
    placeholders = ",".join("?" for _ in TASK_SAMPLES)
    rows = con.execute(
        "SELECT sample_name, review_id, reviewer, status, chosen_labels_json, chosen_source, created_at, updated_at FROM reviews "
        "WHERE sample_name IN (%s)" % placeholders,
        TASK_SAMPLES,
    ).fetchall()
    for row in rows:
        con.execute(
            """
            INSERT OR IGNORE INTO active_assignments
            (sample_name, review_id, reviewer_key, reviewer_name, assignment_kind, assigned_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["sample_name"], row["review_id"], reviewer_key(row["reviewer"]),
             row["reviewer"], "completed_review", row["created_at"],
             row["updated_at"] if review_is_finished(row) else None),
        )


def ensure_reviewer_assignments(reviewer):
    """Allocate up to 300 rows to any reviewer name using live coverage counts."""
    reviewer = canonical_reviewer_name(reviewer)
    key = reviewer_key(reviewer)
    if not key:
        return 0
    con = connect_db()
    try:
        backfill_active_assignments(con)
        normalize_assignment_completion(con)
        release_unstarted_assignments(con, key)
        assignment_rows = con.execute(
            "SELECT sample_name, review_id FROM active_assignments WHERE reviewer_key = ?",
            (key,),
        ).fetchall()
        existing = {(row["sample_name"], row["review_id"]) for row in assignment_rows}
        existing_class_counts = {}
        for sample, _ in existing:
            label = TARGET_LABEL_BY_TASK.get(sample, sample)
            existing_class_counts[label] = existing_class_counts.get(label, 0) + 1
        remaining = max(0, ACTIVE_REVIEW_WORKLOAD - len(existing))
        if not remaining:
            con.commit()
            return len(existing)
        coverage = {}
        saved_rows = con.execute(
            """
            SELECT a.sample_name, a.review_id, a.reviewer_key,
                   r.status, r.chosen_labels_json, r.chosen_source
            FROM active_assignments AS a
            JOIN reviews AS r
              ON r.sample_name = a.sample_name
             AND r.review_id = a.review_id
             AND r.reviewer = a.reviewer_name
            WHERE a.sample_name IN (%s)
            """ % ",".join("?" for _ in TASK_SAMPLES),
            TASK_SAMPLES,
        ).fetchall()
        slot_holders = {}
        for row in saved_rows:
            if review_occupies_slot(row):
                item_key = (row["sample_name"], row["review_id"])
                slot_holders.setdefault(item_key, set()).add(row["reviewer_key"])
        coverage = {item_key: len(reviewers) for item_key, reviewers in slot_holders.items()}
        queue = active_queue_rows()
        priority = {(sample, row.get("review_id", "")): index for index, (sample, row) in enumerate(queue)}
        candidates = [
            (sample, row) for sample, row in queue
            if (sample, row.get("review_id", "")) not in existing
            and coverage.get((sample, row.get("review_id", "")), 0) < MAX_REVIEWERS_PER_TASK
        ]
        candidates.sort(key=lambda pair: (
            coverage.get((pair[0], pair[1].get("review_id", "")), 0),
            priority[(pair[0], pair[1].get("review_id", ""))],
        ))
        by_class = {}
        for pair in candidates:
            label = TARGET_LABEL_BY_TASK.get(pair[0], pair[0])
            by_class.setdefault(label, []).append(pair)
        selected = []
        selected_keys = set()
        for label in sorted(by_class):
            already_have = existing_class_counts.get(label, 0)
            quota = max(0, min(MIN_REVIEW_PER_CLASS, already_have + len(by_class[label])) - already_have)
            for pair in by_class[label][:quota]:
                selected.append(pair)
                selected_keys.add((pair[0], pair[1].get("review_id", "")))
        class_counts = {label: 0 for label in by_class}
        for sample, row in selected:
            label = TARGET_LABEL_BY_TASK.get(sample, sample)
            class_counts[label] = class_counts.get(label, 0) + 1
        remainder = [pair for pair in candidates
                     if (pair[0], pair[1].get("review_id", "")) not in selected_keys]
        while remainder and len(selected) < remaining:
            best_index = min(
                range(len(remainder)),
                key=lambda index: (
                    class_counts.get(TARGET_LABEL_BY_TASK.get(remainder[index][0], remainder[index][0]), 0),
                    coverage.get((remainder[index][0], remainder[index][1].get("review_id", "")), 0),
                    priority[(remainder[index][0], remainder[index][1].get("review_id", ""))],
                ),
            )
            pair = remainder.pop(best_index)
            selected.append(pair)
            label = TARGET_LABEL_BY_TASK.get(pair[0], pair[0])
            class_counts[label] = class_counts.get(label, 0) + 1
        now = utc_now()
        for sample, row in selected[:remaining]:
            review_id = row.get("review_id", "")
            item_key = (sample, review_id)
            kind = "cross_ref" if coverage.get(item_key, 0) >= 1 else "primary"
            con.execute(
                """
                INSERT OR IGNORE INTO active_assignments
                (sample_name, review_id, reviewer_key, reviewer_name, assignment_kind, assigned_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sample, review_id, key, reviewer, kind, now, None),
            )
            existing.add(item_key)
            coverage[item_key] = coverage.get(item_key, 0) + 1
        con.commit()
        return len(existing)
    finally:
        con.close()


def assigned_items(sample, reviewer):
    items, source_path, has_reviewable_descriptions = reviewable_items(sample)
    if sample not in TASK_SAMPLES:
        return items, source_path, has_reviewable_descriptions
    key = reviewer_key(reviewer)
    con = connect_db()
    try:
        assigned_ids = {
            row["review_id"]
            for row in con.execute(
                "SELECT review_id FROM active_assignments WHERE sample_name = ? AND reviewer_key = ?",
                (sample, key),
            ).fetchall()
        }
    finally:
        con.close()
    filtered = []
    for row in items:
        if row.get("review_id", "") in assigned_ids:
            filtered.append(row)
    return filtered, source_path, has_reviewable_descriptions


def description_for_row(row):
    for key in ("description", "source_description", "permit_description", "work_description", "job_description"):
        value = str(row.get(key, "") or "").strip()
        if value:
            return value
    return ""


def review_text_for_row(row):
    return description_for_row(row)


def location_for_row(row):
    parts = []
    for key in ("source_address", "address", "street_address", "source_city", "city", "source_state", "state", "zip_code"):
        value = str(row.get(key, "") or "").strip()
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def source_join_coverage(rows):
    description_count = 0
    join_status_counts = {}
    for row in rows:
        if description_for_row(row):
            description_count += 1
        status = str(row.get("source_join_status", "") or "").strip() or "unknown"
        join_status_counts[status] = join_status_counts.get(status, 0) + 1
    return description_count, join_status_counts


def load_taxonomy():
    if not TAXONOMY_PATH.exists():
        return [], {}
    rows = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    by_flag = {}
    for row in rows:
        flag = str(row.get("flag", ""))
        if flag:
            by_flag[flag] = row
    return rows, by_flag


def label_display(flag, by_flag):
    row = by_flag.get(flag)
    if not row:
        return flag
    return row.get("display_label") or "%s -> %s" % (row.get("category", ""), row.get("subcategory", ""))


def labels_display(flags, by_flag):
    return " + ".join(label_display(flag, by_flag) for flag in flags) if flags else "No taxonomy label"


def review_map(sample, reviewer, mode):
    con = connect_db()
    try:
        rows = con.execute(
            """
            SELECT * FROM reviews
            WHERE sample_name = ? AND reviewer = ? AND mode = ?
            """,
            (sample, reviewer, mode),
        ).fetchall()
    finally:
        con.close()
    return {row["review_id"]: dict(row) for row in rows}


def finished_review_map(sample, reviewer, mode):
    return {
        review_id: row
        for review_id, row in review_map(sample, reviewer, mode).items()
        if review_is_finished(row)
    }


def review_map_any_mode(sample, reviewer):
    con = connect_db()
    try:
        rows = con.execute(
            """
            SELECT * FROM reviews
            WHERE sample_name = ? AND reviewer = ?
            ORDER BY updated_at DESC
            """,
            (sample, reviewer),
        ).fetchall()
    finally:
        con.close()
    out = {}
    for row in rows:
        out.setdefault(row["review_id"], dict(row))
    return out


def historical_progress(sample, reviewer):
    """Summarize this reviewer's saved historical work for one locked queue."""
    rows = review_map_any_mode(sample, reviewer)
    counts = {
        "reviewed": 0,
        "skipped": 0,
        "unclear": 0,
        "denied": 0,
        "in_progress": 0,
    }
    for row in rows.values():
        status = str(row.get("status") or "").strip().lower()
        counts[status] = counts.get(status, 0) + 1
    # Historical rows are immutable records. Older V3 exports can carry the
    # autosave source marker even when their saved status is already reviewed.
    completed = sum(
        1
        for row in rows.values()
        if str(row.get("status") or "").strip().lower() in {"reviewed", "denied"}
    )
    source_total = sample_status(sample)["reviewable_count"]
    reviewer_total = len(rows)
    saved = len(rows)
    last_updated = max((str(row.get("updated_at") or "") for row in rows.values()), default="")
    if reviewer_total and completed >= reviewer_total:
        state = "done"
    elif saved:
        state = "in_progress"
    else:
        state = "not_started"
    return {
        "total": reviewer_total,
        "source_total": source_total,
        "reviewer_total": reviewer_total,
        "saved_count": saved,
        "completed_count": completed,
        "reviewed_count": counts.get("reviewed", 0),
        "skipped_count": counts.get("skipped", 0),
        "unclear_count": counts.get("unclear", 0),
        "denied_count": counts.get("denied", 0),
        "in_progress_count": counts.get("in_progress", 0),
        "progress_pct": (100.0 * completed / reviewer_total) if reviewer_total else 0.0,
        "last_updated": last_updated,
        "state": state,
    }


def historical_items(sample, reviewer):
    """Return only the historical permits represented in this reviewer's records."""
    items, source_path, has_reviewable_descriptions = reviewable_items(sample)
    saved_ids = set(review_map_any_mode(sample, reviewer))
    return [row for row in items if (row.get("review_id") or "") in saved_ids], source_path, has_reviewable_descriptions


def reviewer_tasks(reviewer):
    """Allocate live work for any reviewer name and return active plus historical queues."""
    reviewer = canonical_reviewer_name(reviewer)
    ensure_reviewer_assignments(reviewer)
    out = []
    for sample in TASK_SAMPLES:
        mine, _, _ = assigned_items(sample, reviewer)
        assigned = len(mine)
        done_map = finished_review_map(sample, reviewer, TASK_MODE)
        done = sum(1 for r in mine if (r.get("review_id") or "") in done_map)
        if assigned and done >= assigned:
            state = "done"
        elif done > 0:
            state = "in_progress"
        else:
            state = "not_started"
        out.append({
            "sample": sample,
            "title": TASK_TITLES.get(sample, sample),
            "mode": TASK_MODE,
            "assigned": assigned,
            "done": done,
            "remaining": max(0, assigned - done),
            "state": state,
            "ready": assigned > 0,
        })
    historical = []
    for sample in HISTORICAL_TASK_SAMPLES:
        progress = historical_progress(sample, reviewer)
        historical.append({
            "sample": sample,
            "title": TASK_TITLES.get(sample, sample),
            "assigned": progress["reviewer_total"],
            "mode": "readonly",
            "ready": progress["reviewer_total"] > 0,
            "locked": True,
            **progress,
        })
    return {
        "tasks": out,
        "historical": historical,
        "reviewer": reviewer,
        "reviewer_known": bool(reviewer),
        "assigned_total": sum(item["assigned"] for item in out),
    }


def count_reviews(sample, mode=None):
    con = connect_db()
    try:
        if mode:
            rows = con.execute(
                "SELECT mode, COUNT(*) AS n FROM reviews WHERE sample_name = ? AND mode = ? GROUP BY mode",
                (sample, mode),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT mode, COUNT(*) AS n FROM reviews WHERE sample_name = ? GROUP BY mode",
                (sample,),
            ).fetchall()
    finally:
        con.close()
    return {row["mode"]: int(row["n"]) for row in rows}


def sample_status(sample):
    paths = sample_paths(sample)
    items, source_path, has_reviewable_descriptions = load_joined_items(sample)
    answers = load_answers(sample)
    counts = count_reviews(sample)
    description_count, join_status_counts = source_join_coverage(items)
    item_count = len(items)
    has_all_descriptions = bool(items) and description_count == item_count
    return {
        "sample": sample,
        "expected_n": infer_sample_size(sample) or item_count,
        "item_count": item_count,
        "answer_count": len(answers),
        "has_blinded_csv": paths["blinded"].exists(),
        "has_joined_csv": paths["joined"].exists(),
        "has_description": has_all_descriptions,
        "has_review_context": has_reviewable_descriptions,
        "has_reviewable_descriptions": has_reviewable_descriptions,
        "reviewable_count": item_count if sample in TASK_SAMPLES else description_count,
        "description_count": description_count,
        "missing_description_count": max(0, item_count - description_count),
        "description_coverage_pct": (100.0 * description_count / item_count) if item_count else 0.0,
        "join_status_counts": join_status_counts,
        "source_path": source_path,
        "fast_reviews": counts.get("fast", 0),
        "blind_reviews": counts.get("blind", 0),
    }


def json_response(handler, payload, status=200):
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def text_response(handler, text, status=200, content_type="text/plain; charset=utf-8"):
    raw = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def file_response(handler, path):
    if not path.exists() or not path.is_file():
        text_response(handler, "Not found", 404)
        return
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    raw = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


HELP_MODAL_HTML_V1 = """
  <div id="helpOverlay" class="help-overlay" hidden>
    <div class="help-modal" role="dialog" aria-modal="true" aria-labelledby="helpTitle">
      <div class="help-head">
        <div>
          <div class="help-eyebrow">Reviewer guide</div>
          <h2 id="helpTitle">How to label a permit</h2>
        </div>
        <button id="helpClose" class="help-close" aria-label="Close">&times;</button>
      </div>
      <div class="help-body">

        <p class="help-lead">You read a building-permit description and choose the single category that
        best describes the <b>actual work</b>. This is a <b>blind</b> review &mdash; you will not see the
        model's guess. Say what the permit really is, from the text alone.</p>

        <section class="help-sec">
          <h3>The flow</h3>
          <ol class="help-ol">
            <li><b>Click the category</b> that matches the main work, or press its number key
              <span class="kbd">1</span>&ndash;<span class="kbd">5</span>. The button flashes
              <span class="pill-good">green</span> to confirm.</li>
            <li><b>None of these apply</b> (<span class="kbd">8</span>) &mdash; the work is real but fits no
              category shown. Saves <i>none</i> and moves on. If you <i>know</i> it belongs to another
              family, use the full-tree escape link to record it.</li>
            <li><b>Skip for now</b> (<span class="kbd">9</span>) &mdash; the text is too vague or cut off to
              judge. Skipping is fine &mdash; don't guess.</li>
            <li>You can <b>go back</b>: your previous choice is highlighted and can be changed.</li>
          </ol>
          <p class="help-note"><b>One permit &rarr; one category.</b> If it covers several trades, pick the
          <b>headline scope</b> &mdash; e.g. &ldquo;New residence with attached garage&rdquo; is
          <b>New construction</b>, not Garage.</p>
        </section>

        <section class="help-sec">
          <h3>Four rules that settle most cases</h3>
          <ul class="help-ul">
            <li><b>Label the work, not the paperwork.</b> Ignore the permit type name, department, or fee
              code. A permit filed &ldquo;Electrical&nbsp;&ndash;&nbsp;Misc.&rdquo; describing a solar array
              is <b>Solar&nbsp;PV</b>.</li>
            <li><b>Most specific wins.</b> Use the catch-alls (Electrical, Plumbing, Mechanical/HVAC,
              Alteration/remodel) only when no narrower category fits.</li>
            <li><b>When the text won't tell you, Skip</b> &mdash; don't invent detail.</li>
            <li><b>The electrification categories matter most</b> (Solar, EV charger, Battery, Panel
              upgrade, Heat pump). Slow down and apply the boundary rules.</li>
          </ul>
        </section>

        <section class="help-sec">
          <h3>Boundary rules that actually trip people up</h3>
          <table class="help-table">
            <thead><tr><th>Situation</th><th>Correct label</th></tr></thead>
            <tbody>
              <tr><td>Heat-pump <b>water heater</b> (domestic hot water)</td><td><b>Water heater</b> &mdash; not Heat pump</td></tr>
              <tr><td><b>Solar thermal</b> / solar hot water (no electricity)</td><td><i>not</i> Solar PV (Solar PV = electric only)</td></tr>
              <tr><td><b>Roof-mounted solar</b>, no re-roofing scope</td><td><b>Solar PV</b> &mdash; not Roofing</td></tr>
              <tr><td>Service <b>capacity change</b> (100A&rarr;200A, meter-main)</td><td><b>Panel upgrade</b> &mdash; not Electrical</td></tr>
              <tr><td>Any <b>EV charging equipment</b> present</td><td><b>EV charger</b> &mdash; not Electrical</td></tr>
              <tr><td><b>Retaining / freestanding wall</b></td><td><b>Fence/wall</b> &mdash; not Structural</td></tr>
              <tr><td><b>Sewer lateral / water service main</b> to street</td><td><b>Sewer/utility</b> &mdash; not Plumbing</td></tr>
              <tr><td>Interior <b>fixtures &amp; supply lines</b> only</td><td><b>Plumbing</b> &mdash; not Sewer/utility</td></tr>
              <tr><td><b>ADU, shed, detached garage, carport</b></td><td><b>Garage/accessory</b> &mdash; not New construction</td></tr>
              <tr><td><b>Mini-split / air-source heat pump</b></td><td><b>Heat pump</b> &mdash; not Mechanical/HVAC</td></tr>
            </tbody>
          </table>
        </section>

        <section class="help-sec">
          <h3>The 25 categories &mdash; with a gold-standard example each</h3>
          <p class="help-note">Each shows what it means, a <b>gold example</b> (unambiguously this
          category), and the most common thing it is <i>not</i>.</p>

          <div class="help-fam"><span class="fam-tag">&#9889; Electrical</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>1 &middot; Solar PV</h4><p>Photovoltaic solar <i>electric</i> generation equipment.</p><p class="gold">&ldquo;Install roof-mounted 7.2&nbsp;kW photovoltaic system, 18 modules with string inverter, residential.&rdquo;</p><p class="nope">Not: solar thermal; a generic electrical permit.</p></div>
            <div class="help-card"><h4>2 &middot; EV charger</h4><p>Electric-vehicle charging equipment (EVSE).</p><p class="gold">&ldquo;Install 240V/40A dedicated circuit and Level 2 EVSE in attached garage.&rdquo;</p><p class="nope">Not: parking electrical with no charger.</p></div>
            <div class="help-card"><h4>3 &middot; Battery storage</h4><p>Stationary electrical energy storage.</p><p class="gold">&ldquo;Install Powerwall (13.5&nbsp;kWh) battery energy storage system with backup gateway, garage.&rdquo;</p><p class="nope">Not: batteries in alarms or vehicles.</p></div>
            <div class="help-card"><h4>4 &middot; Panel upgrade</h4><p>Service panel / meter-main replacement or capacity change.</p><p class="gold">&ldquo;Upgrade main service from 100A to 200A, replace meter-main and load center.&rdquo;</p><p class="nope">Not: reconnect/inspection with no change.</p></div>
            <div class="help-card"><h4>5 &middot; Electrical <span class="catch">catch-all</span></h4><p>General wiring, circuits, service, fixtures, equipment.</p><p class="gold">&ldquo;Rewire kitchen, add six 20A circuits, replace outlets, switches and lighting.&rdquo;</p><p class="nope">Not: solar/EV/battery/panel when one applies.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">&#128293; Mechanical &amp; HVAC</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>6 &middot; Heat pump</h4><p>Heat-pump equipment (air-source, mini-split, ground-source).</p><p class="gold">&ldquo;Install 3-ton ducted air-source heat pump to replace gas furnace and AC.&rdquo;</p><p class="nope">Not: generic HVAC; a heat-pump <i>water heater</i>.</p></div>
            <div class="help-card"><h4>7 &middot; Mechanical / HVAC <span class="catch">catch-all</span></h4><p>General heating, ventilation, cooling, refrigeration, ductwork.</p><p class="gold">&ldquo;Replace 4-ton gas furnace and condenser, new ductwork and thermostat; AC changeout.&rdquo;</p><p class="nope">Not: a heat pump when that fits.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">&#128703; Plumbing</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>8 &middot; Water heater</h4><p>Water-heating equipment.</p><p class="gold">&ldquo;Replace 50-gallon gas water heater with tankless unit; like-for-like.&rdquo;</p><p class="nope">Not: a space-heating boiler, no domestic hot water.</p></div>
            <div class="help-card"><h4>9 &middot; Gas</h4><p>Fuel-gas piping, meters, or gas-fired equipment connections.</p><p class="gold">&ldquo;Install new fuel-gas piping and meter for range and dryer; gas line extension.&rdquo;</p><p class="nope">Not: water piping; gasoline storage.</p></div>
            <div class="help-card"><h4>10 &middot; Plumbing <span class="catch">catch-all</span></h4><p>General fixtures, water supply, drainage, piping.</p><p class="gold">&ldquo;Repipe two bathrooms, replace fixtures, water supply and drain lines.&rdquo;</p><p class="nope">Not: fuel gas only; a sewer main only.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">&#127968; Exterior</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>11 &middot; Roofing</h4><p>Roof covering replacement, repair, or new roof.</p><p class="gold">&ldquo;Tear off and reroof 24 squares of asphalt shingles over existing residence.&rdquo;</p><p class="nope">Not: roof-mounted equipment, no roofing scope.</p></div>
            <div class="help-card"><h4>12 &middot; Window/door</h4><p>Windows, doors, or glazing.</p><p class="gold">&ldquo;Replace 12 windows and 2 exterior doors like-for-like; retrofit vinyl.&rdquo;</p><p class="nope">Not: framing with no window/door work.</p></div>
            <div class="help-card"><h4>13 &middot; Sign</h4><p>Signs and sign structures.</p><p class="gold">&ldquo;Install illuminated wall sign and monument sign for retail tenant.&rdquo;</p><p class="nope">Not: traffic signs; contract signatures.</p></div>
            <div class="help-card"><h4>14 &middot; Deck/patio/porch</h4><p>Decks, patios, or porches.</p><p class="gold">&ldquo;Construct 200 sq ft attached wood deck with stairs at rear of residence.&rdquo;</p><p class="nope">Not: an interior floor platform.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">&#127959;&#65039; Building</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>15 &middot; Alteration/remodel <span class="catch">catch-all</span></h4><p>Alteration, renovation, or remodel of an existing building.</p><p class="gold">&ldquo;Interior remodel of kitchen and two baths, non-structural; tenant improvement.&rdquo;</p><p class="nope">Not: a new standalone building.</p></div>
            <div class="help-card"><h4>16 &middot; New construction</h4><p>A new principal building or structure.</p><p class="gold">&ldquo;Construct new two-story single-family residence, 2,400 sq ft.&rdquo;</p><p class="nope">Not: an addition or accessory structure only.</p></div>
            <div class="help-card"><h4>17 &middot; Demolition</h4><p>Full or partial demolition.</p><p class="gold">&ldquo;Full demolition of existing single-family residence and foundation.&rdquo;</p><p class="nope">Not: removing one fixture.</p></div>
            <div class="help-card"><h4>18 &middot; Garage/accessory</h4><p>Garage, carport, shed, ADU, or other accessory structure.</p><p class="gold">&ldquo;Construct new detached 400 sq ft garage&rdquo; / &ldquo;Build 600 sq ft ADU.&rdquo;</p><p class="nope">Not: a principal new building.</p></div>
            <div class="help-card"><h4>19 &middot; Structural</h4><p>Framing, foundation, load-bearing or reinforcement work.</p><p class="gold">&ldquo;Foundation repair: install 8 steel piers and reinforce framing; seismic retrofit.&rdquo;</p><p class="nope">Not: non-structural finish work.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">&#127795; Sitework</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>20 &middot; Pool/spa</h4><p>Swimming pool, spa, hot tub, or pool equipment.</p><p class="gold">&ldquo;Construct in-ground gunite swimming pool and spa with equipment pad.&rdquo;</p><p class="nope">Not: a fountain or a beauty spa.</p></div>
            <div class="help-card"><h4>21 &middot; Fence/wall</h4><p>Fences or freestanding/retaining walls.</p><p class="gold">&ldquo;Install 120 ft of 6 ft cedar fence and a 4 ft masonry retaining wall.&rdquo;</p><p class="nope">Not: an interior partition wall.</p></div>
            <div class="help-card"><h4>22 &middot; Driveway/paving</h4><p>Driveway, sidewalk, paving, or hardscape.</p><p class="gold">&ldquo;Replace concrete driveway approach and sidewalk; new asphalt paving.&rdquo;</p><p class="nope">Not: roof paving products.</p></div>
            <div class="help-card"><h4>23 &middot; Grading/sitework</h4><p>Earthwork, excavation, grading, erosion control, site prep.</p><p class="gold">&ldquo;Rough grading and excavation with erosion control for site preparation.&rdquo;</p><p class="nope">Not: an interior renovation.</p></div>
            <div class="help-card"><h4>24 &middot; Sewer/utility</h4><p>Sewer/water service main, lateral, or ROW utility connection.</p><p class="gold">&ldquo;Replace sewer lateral from house to city main&rdquo; / &ldquo;New 1-inch water service line.&rdquo;</p><p class="nope">Not: interior plumbing fixtures.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">&#129519; Life safety</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>25 &middot; Fire protection</h4><p>Fire alarm, sprinkler, suppression, or detection systems.</p><p class="gold">&ldquo;Install NFPA 13D fire sprinkler system throughout new residence; monitored fire alarm.&rdquo;</p><p class="nope">Not: a lawn irrigation sprinkler.</p></div>
          </div>
        </section>

        <section class="help-sec help-tldr">
          <h3>The whole decision in one line</h3>
          <p>Read the description &rarr; pick the <b>most specific</b> category for the <b>main work</b>
          &rarr; if nothing in the shown family fits, <b>None of these apply</b> &rarr; if you can't tell,
          <b>Skip</b>. Watch the electrification boundaries. Label the <b>work</b>, never the paperwork.</p>
        </section>

      </div>
    </div>
  </div>
"""


HELP_MODAL_HTML_V2 = """
  <div id="helpOverlay" class="help-overlay" hidden>
    <div class="help-modal" role="dialog" aria-modal="true" aria-labelledby="helpTitle">
      <div class="help-head">
        <div>
          <div class="help-eyebrow">Reviewer guide · v2</div>
          <h2 id="helpTitle">How to review this round</h2>
        </div>
        <button id="helpClose" class="help-close" aria-label="Close">&times;</button>
      </div>
      <div class="help-body">
        <p class="help-lead">Read the permit description and choose the most specific active category supported by the text. The model prediction is hidden during this review.</p>

        <section class="help-sec">
          <h3>Before you start</h3>
          <ol class="help-ol">
            <li>Enter your reviewer name in the top bar.</li>
            <li>Use the active targeted queues. Each card shows your assigned count and remaining count.</li>
            <li>Historical cards are locked. Their old records remain preserved.</li>
          </ol>
          <p class="help-note"><b>Assignment rule:</b> the system targets at least 10 permits per available active class, caps each reviewer at 300 class-target reviews, and allows no more than two substantive reviewers per class-target permit. Demolition, Sign, and Window / door have fewer than 10 permits in this round. Solar PV is not an active queue. Heat Pump appears as a choice inside the Mechanical / HVAC queue when the description supports it.</p>
        </section>

        <section class="help-sec">
          <h3>For each permit</h3>
          <ol class="help-ol">
            <li>Read the full description.</li>
            <li>Choose the narrow category that matches the main work. One permit can appear in multiple class queues because one permit can support multiple class-specific estimates.</li>
            <li>Use <b>None of these apply</b> when the work is real and no shown category fits.</li>
            <li>Use <b>Skip for now</b> when the description is too vague, incomplete, or contradictory.</li>
            <li>Use the note field for a short explanation. Notes and unfinished selections autosave after a short pause. Autosave does not complete the permit.</li>
          </ol>
          <p class="help-note"><b>Category selection completes the permit.</b> You can use Back to revisit a saved choice. Forward saves any unfinished note before moving.</p>
        </section>

        <section class="help-sec">
          <h3>Boundary rules</h3>
          <table class="help-table">
            <thead><tr><th>Permit text</th><th>Use</th></tr></thead>
            <tbody>
              <tr><td>Interior remodel with no clear narrow scope</td><td><b>Alteration / remodel</b></td></tr>
              <tr><td>New principal residence or principal building</td><td><b>New construction</b></td></tr>
              <tr><td>ADU, shed, detached garage, or carport</td><td><b>Garage / accessory</b></td></tr>
              <tr><td>Service panel or meter-main capacity change</td><td><b>Electrical panel upgrade</b></td></tr>
              <tr><td>EV charging equipment</td><td><b>EV charger</b></td></tr>
              <tr><td>Stationary battery energy storage</td><td><b>Battery storage</b></td></tr>
              <tr><td>Air-source, mini-split, or ground-source heat-pump equipment</td><td><b>Heat Pump</b></td></tr>
              <tr><td>Generic furnace, air conditioner, ventilation, or ductwork</td><td><b>Mechanical / HVAC</b></td></tr>
              <tr><td>Domestic hot-water equipment, including heat-pump water heaters</td><td><b>Water heater</b></td></tr>
              <tr><td>Roof work with no re-roofing scope</td><td>Use the equipment category when supported; do not use <b>Roofing</b></td></tr>
              <tr><td>Sewer lateral or water service to the street</td><td><b>Sewer / utility</b></td></tr>
              <tr><td>Interior fixtures, supply, or drain lines</td><td><b>Plumbing</b></td></tr>
              <tr><td>Permit text lists multiple trades</td><td>Choose the headline scope</td></tr>
            </tbody>
          </table>
        </section>

        <section class="help-sec">
          <h3>Active categories with example permits</h3>
          <p class="help-note">These examples show clear cases. Use the permit description when the wording is less direct.</p>

          <div class="help-fam"><span class="fam-tag">Electrical</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Battery storage</h4><p>Stationary electrical energy storage.</p><p class="gold">&ldquo;Install a 13.5 kWh battery energy storage system with backup gateway.&rdquo;</p><p class="nope">Not: batteries in vehicles or alarms.</p></div>
            <div class="help-card"><h4>Electrical panel upgrade</h4><p>Service panel, meter-main, or capacity change.</p><p class="gold">&ldquo;Upgrade the main service from 100A to 200A and replace the meter-main.&rdquo;</p><p class="nope">Not: ordinary wiring with no capacity change.</p></div>
            <div class="help-card"><h4>EV charger</h4><p>Electric-vehicle charging equipment.</p><p class="gold">&ldquo;Install a 240V dedicated circuit and Level 2 EV charger in the garage.&rdquo;</p><p class="nope">Not: parking electrical work with no charger.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">Mechanical and HVAC</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Mechanical / HVAC</h4><p>General heating, cooling, ventilation, refrigeration, or ductwork.</p><p class="gold">&ldquo;Replace the gas furnace and air conditioner, including new ductwork and thermostat.&rdquo;</p><p class="nope">Heat-pump equipment uses the Heat Pump choice.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">Plumbing</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Water heater</h4><p>Domestic water-heating equipment.</p><p class="gold">&ldquo;Replace the 50-gallon gas water heater with a tankless unit.&rdquo;</p><p class="nope">Includes heat-pump water heaters.</p></div>
            <div class="help-card"><h4>Gas</h4><p>Fuel-gas piping, meters, or gas connections.</p><p class="gold">&ldquo;Install new fuel-gas piping and extend the gas line to the range and dryer.&rdquo;</p><p class="nope">Not: water piping or sewer work.</p></div>
            <div class="help-card"><h4>Plumbing</h4><p>Fixtures, water supply, drainage, or interior piping.</p><p class="gold">&ldquo;Repipe two bathrooms and replace the fixtures, water supply, and drain lines.&rdquo;</p><p class="nope">Not: a sewer main or fuel-gas-only permit.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">Exterior</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Roofing</h4><p>Roof covering replacement or repair.</p><p class="gold">&ldquo;Tear off and reroof 24 squares of asphalt shingles over the existing residence.&rdquo;</p><p class="nope">Not: roof-mounted equipment with no re-roofing scope.</p></div>
            <div class="help-card"><h4>Window / door</h4><p>Windows, exterior doors, or glazing.</p><p class="gold">&ldquo;Replace 12 windows and 2 exterior doors like-for-like.&rdquo;</p><p class="nope">Not: framing with no window or door work.</p></div>
            <div class="help-card"><h4>Sign</h4><p>Signs and sign structures.</p><p class="gold">&ldquo;Install an illuminated wall sign and a monument sign for the retail tenant.&rdquo;</p><p class="nope">Not: traffic signs or contract signatures.</p></div>
            <div class="help-card"><h4>Deck / patio / porch</h4><p>Decks, patios, porches, or related stairs.</p><p class="gold">&ldquo;Construct a 200 sq ft attached wood deck with stairs at the rear of the residence.&rdquo;</p><p class="nope">Not: an interior floor platform.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">Building</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Alteration / remodel</h4><p>Renovation of an existing building with no narrower active scope.</p><p class="gold">&ldquo;Remodel the kitchen and two bathrooms with non-structural tenant improvements.&rdquo;</p><p class="nope">Not: a new principal building.</p></div>
            <div class="help-card"><h4>New construction</h4><p>A new principal building or structure.</p><p class="gold">&ldquo;Construct a new two-story single-family residence, 2,400 sq ft.&rdquo;</p><p class="nope">Not: an accessory structure only.</p></div>
            <div class="help-card"><h4>Demolition</h4><p>Full or partial demolition.</p><p class="gold">&ldquo;Demolish the existing single-family residence and foundation.&rdquo;</p><p class="nope">Not: removing one fixture during a remodel.</p></div>
            <div class="help-card"><h4>Garage / accessory</h4><p>Garage, carport, shed, ADU, or other accessory structure.</p><p class="gold">&ldquo;Construct a new detached 400 sq ft garage.&rdquo;</p><p class="nope">Not: the principal new building.</p></div>
            <div class="help-card"><h4>Structural</h4><p>Foundation, framing, load-bearing, or reinforcement work.</p><p class="gold">&ldquo;Repair the foundation with steel piers and reinforce the load-bearing framing.&rdquo;</p><p class="nope">Not: non-structural finish work.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">Sitework</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Fence / wall</h4><p>Fences or freestanding and retaining walls.</p><p class="gold">&ldquo;Install 120 ft of cedar fence and a masonry retaining wall.&rdquo;</p><p class="nope">Not: an interior partition wall.</p></div>
            <div class="help-card"><h4>Driveway / paving</h4><p>Driveways, sidewalks, paving, or hardscape.</p><p class="gold">&ldquo;Replace the concrete driveway approach and sidewalk with new asphalt paving.&rdquo;</p><p class="nope">Not: roof paving products.</p></div>
            <div class="help-card"><h4>Grading / sitework</h4><p>Earthwork, excavation, grading, erosion control, or site preparation.</p><p class="gold">&ldquo;Perform rough grading and excavation with erosion control for site preparation.&rdquo;</p><p class="nope">Not: an interior renovation.</p></div>
            <div class="help-card"><h4>Sewer / utility</h4><p>Sewer or water service mains, laterals, or right-of-way connections.</p><p class="gold">&ldquo;Replace the sewer lateral from the house to the city main.&rdquo;</p><p class="nope">Not: interior plumbing fixtures.</p></div>
          </div>

          <div class="help-fam"><span class="fam-tag">Life safety</span></div>
          <div class="help-cards">
            <div class="help-card"><h4>Fire protection</h4><p>Fire alarms, sprinklers, suppression, or detection systems.</p><p class="gold">&ldquo;Install an NFPA 13D fire sprinkler system throughout the new residence.&rdquo;</p><p class="nope">Not: a lawn irrigation sprinkler.</p></div>
          </div>
        </section>

        <section class="help-sec help-tldr">
          <h3>Quick decision</h3>
          <p>Read the work description &rarr; select the most specific shown category &rarr; use <b>None of these apply</b> for a real but unmatched scope &rarr; use <b>Skip for now</b> when the text cannot support a decision.</p>
          <p class="help-note">Need the previous guide? <button id="legacyHelpBtn" class="help-inline" type="button">Open reviewer guide v1</button>.</p>
        </section>
      </div>
    </div>
  </div>
"""

HELP_MODAL_HTML = HELP_MODAL_HTML_V2
HELP_MODAL_HTML_V1_EMBEDDED = (HELP_MODAL_HTML_V1
    .replace('id="helpOverlay"', 'id="helpOverlayV1"')
    .replace('aria-labelledby="helpTitle"', 'aria-labelledby="helpTitleV1"')
    .replace('id="helpTitle"', 'id="helpTitleV1"')
    .replace('id="helpClose"', 'id="helpCloseV1"'))


def app_shell():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Permit Validation</title>
  <link rel="stylesheet" href="static/styles.css">
</head>
<body>
  <header class="topbar">
    <div>
      <div class="eyebrow">Permit validation</div>
      <h1>Permit categorization review</h1>
    </div>
    <div class="top-actions">
      <label class="reviewer-label" for="reviewerName">Reviewer</label>
      <input id="reviewerName" class="reviewer-input" type="text" placeholder="Type your name" autocomplete="off">
      <button id="themeToggle" class="icon-text" title="Toggle dark / light">Dark</button>
      <button id="refreshBtn" class="icon-text">Refresh</button>
      <button id="helpBtn" class="help-icon" title="How to label — reviewer guide" aria-label="Reviewer guide">?</button>
    </div>
  </header>
  <main id="app"></main>
""" + HELP_MODAL_HTML + HELP_MODAL_HTML_V1_EMBEDDED + """
  <script src="static/api-config.js"></script>
  <script src="static/app.js"></script>
</body>
</html>
"""


def save_review(payload):
    sample = str(payload.get("sample") or "")
    review_id = str(payload.get("review_id") or "")
    reviewer = str(payload.get("reviewer") or "").strip()
    mode = str(payload.get("mode") or "fast")
    status = str(payload.get("status") or "reviewed")
    labels = payload.get("labels") or []
    if not isinstance(labels, list):
        labels = []
    labels = sorted(dict.fromkeys(str(flag).strip() for flag in labels if str(flag).strip()))
    chosen_source = str(payload.get("chosen_source") or "manual")
    reason_tag = str(payload.get("reason_tag") or "")
    note = str(payload.get("note") or "")
    review_time_ms = int(payload.get("review_time_ms") or 0)
    if chosen_source == "autosave":
        status = "in_progress"
    now = utc_now()
    if sample not in SAMPLES or not review_id:
        return {"ok": False, "error": "bad_sample_or_review_id"}, 400
    if sample in HISTORICAL_TASK_SAMPLES:
        return {"ok": False, "error": "historical_task_locked", "message": "This historical task is locked for new review."}, 410
    if sample in TASK_SAMPLES:
        reviewer = canonical_reviewer_name(reviewer)
        ensure_reviewer_assignments(reviewer)
    if not reviewer:
        return {"ok": False, "error": "reviewer_required", "message": "Type your reviewer name before saving."}, 400
    con = connect_db()
    try:
        if sample in TASK_SAMPLES:
            # Serialize slot claims so concurrent saves cannot create a third
            # substantive reviewer for the same class-target permit.
            con.execute("BEGIN IMMEDIATE")
            incoming_row = {
                "status": status,
                "chosen_labels_json": json.dumps(labels),
                "chosen_source": chosen_source,
            }
            if review_occupies_slot(incoming_row):
                rows = con.execute(
                    """
                    SELECT reviewer, status, chosen_labels_json, chosen_source
                    FROM reviews
                    WHERE sample_name = ? AND review_id = ?
                    """,
                    (sample, review_id),
                ).fetchall()
                occupied_by = set()
                current_key = reviewer_key(reviewer)
                for row in rows:
                    row_key = reviewer_key(row["reviewer"])
                    if row_key != current_key and review_occupies_slot(row):
                        occupied_by.add(row_key)
                if len(occupied_by) >= MAX_REVIEWERS_PER_TASK:
                    con.rollback()
                    return {
                        "ok": False,
                        "error": "reviewer_limit_reached",
                        "message": "This permit already has two substantive reviewers. Refresh to receive another permit.",
                    }, 409
        if sample in TASK_SAMPLES:
            con.execute(
                """
                INSERT OR IGNORE INTO active_assignments
                (sample_name, review_id, reviewer_key, reviewer_name, assignment_kind, assigned_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sample, review_id, reviewer_key(reviewer), reviewer, "direct_review", now, None),
            )
        con.execute(
            """
            INSERT OR REPLACE INTO reviews (
                sample_name, review_id, reviewer, mode, status,
                chosen_labels_json, chosen_source, reason_tag, note,
                review_time_ms, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sample, review_id, reviewer, mode, status, json.dumps(labels), chosen_source, reason_tag, note, review_time_ms, now, now),
        )
        con.execute(
            """
            INSERT INTO events (
                sample_name, review_id, reviewer, mode, status,
                chosen_labels_json, chosen_source, reason_tag, note,
                review_time_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sample, review_id, reviewer, mode, status, json.dumps(labels), chosen_source, reason_tag, note, review_time_ms, now),
        )
        if sample in TASK_SAMPLES:
            saved_row = {
                "status": status,
                "chosen_labels_json": json.dumps(labels),
                "chosen_source": chosen_source,
            }
            con.execute(
                """
                UPDATE active_assignments
                SET completed_at = ?
                WHERE sample_name = ? AND review_id = ? AND reviewer_key = ?
                """,
                (now if review_is_finished(saved_row) else None, sample, review_id, reviewer_key(reviewer)),
            )
        con.commit()
    finally:
        con.close()
    export_path, export_rows = export_reviews(sample)
    write_timing_artifacts()
    return {"ok": True, "export_csv": str(export_path), "export_rows": len(export_rows)}, 200


def build_item(sample, row, answer, by_flag, mode, index=None, total=None, existing_review=None):
    review_id = row.get("review_id") or answer.get("review_id", "")
    hybrid = split_flags(answer.get("hybrid_labels"))
    deterministic = split_flags(answer.get("deterministic_labels"))
    options = []
    if mode == "fast":
        options.append({
            "id": "hybrid",
            "title": "Hybrid",
            "flags": hybrid,
            "display": labels_display(hybrid, by_flag),
            "confidence": answer.get("hybrid_confidence", ""),
        })
        if deterministic != hybrid:
            options.append({
                "id": "deterministic",
                "title": "Deterministic",
                "flags": deterministic,
                "display": labels_display(deterministic, by_flag),
                "confidence": answer.get("deterministic_confidence", ""),
            })
    fam = TASK_FAMILY.get(sample, "")
    leaf_options = [{"flag": f, "display": label_display(f, by_flag)} for f in FAMILIES.get(fam, [])]
    return {
        "review_id": review_id,
        "permit_number": answer.get("permit_number") or row.get("permit_number", ""),
        "row_index": row.get("population_row_index_0based") or answer.get("population_row_index_0based", ""),
        "batch_id": row.get("batch_id") or answer.get("batch_id", ""),
        "location": location_for_row(row),
        "description": review_text_for_row(row),
        "mode": mode,
        "family": fam,
        "leaf_options": leaf_options,
        "options": options,
        "index": index,
        "position": None if index is None else index + 1,
        "total": total,
        "existing_review": existing_review or {},
    }


def next_item(sample, reviewer, mode):
    readonly = mode == "readonly"
    if sample in HISTORICAL_TASK_SAMPLES and not readonly:
        return {"ok": False, "error": "historical_task_locked", "message": "This historical task is locked for new review."}, 410
    reviewer = canonical_reviewer_name(reviewer)
    if not readonly:
        ensure_reviewer_assignments(reviewer)
    if readonly and sample in HISTORICAL_TASK_SAMPLES:
        items, source_path, has_reviewable_descriptions = historical_items(sample, reviewer)
    else:
        items, source_path, has_reviewable_descriptions = reviewable_items(sample)
    if not readonly:
        items, source_path, has_reviewable_descriptions = assigned_items(sample, reviewer)
    if not has_reviewable_descriptions:
        return {
            "ok": False,
            "error": "source_description_missing",
            "message": "No source descriptions are available yet for this sample.",
            "source_path": source_path,
        }, 409
    answers = load_answers(sample)
    done = {} if readonly else finished_review_map(sample, reviewer, mode)
    saved_reviews = review_map_any_mode(sample, reviewer) if readonly else done
    _, by_flag = load_taxonomy()
    total = len(items)
    for index, row in enumerate(items):
        review_id = row.get("review_id", "")
        if not review_id or review_id in done:
            continue
        answer = answers.get(review_id, {})
        return {
            "ok": True,
            "item": build_item(
                sample,
                row,
                answer,
                by_flag,
                mode,
                index,
                total,
                saved_reviews.get(review_id, {}),
            ),
        }, 200
    return {"ok": True, "complete": True}, 200


def item_at_index(sample, reviewer, mode, index):
    readonly = mode == "readonly"
    if sample in HISTORICAL_TASK_SAMPLES and not readonly:
        return {"ok": False, "error": "historical_task_locked", "message": "This historical task is locked for new review."}, 410
    reviewer = canonical_reviewer_name(reviewer)
    if not readonly:
        ensure_reviewer_assignments(reviewer)
    if readonly and sample in HISTORICAL_TASK_SAMPLES:
        items, source_path, has_reviewable_descriptions = historical_items(sample, reviewer)
    else:
        items, source_path, has_reviewable_descriptions = reviewable_items(sample)
    if not readonly:
        items, source_path, has_reviewable_descriptions = assigned_items(sample, reviewer)
    if not has_reviewable_descriptions:
        return {
            "ok": False,
            "error": "source_description_missing",
            "message": "No source descriptions are available yet for this sample.",
            "source_path": source_path,
        }, 409
    total = len(items)
    if total <= 0:
        return {"ok": True, "complete": True}, 200
    index = max(0, min(int(index or 0), total - 1))
    answers = load_answers(sample)
    done = review_map_any_mode(sample, reviewer) if readonly else review_map(sample, reviewer, mode)
    _, by_flag = load_taxonomy()
    row = items[index]
    review_id = row.get("review_id", "")
    answer = answers.get(review_id, {})
    existing_review = done.get(review_id, {})
    return {
        "ok": True,
        "item": build_item(sample, row, answer, by_flag, mode, index, total, existing_review),
    }, 200


def export_reviews(sample):
    con = connect_db()
    try:
        rows = con.execute(
            """
            SELECT * FROM reviews
            WHERE sample_name = ?
            ORDER BY mode, reviewer, review_id
            """,
            (sample,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        labels = json_loads(row["chosen_labels_json"], [])
        out.append({
            "review_id": row["review_id"],
            "item_key": row["review_id"],
            "sample_name": row["sample_name"],
            "reviewer_id": row["reviewer"],
            "mode": row["mode"],
            "review_status": row["status"],
            "chosen_labels": " | ".join(labels),
            "human_primary_flags": ";".join(labels),
            "chosen_source": row["chosen_source"],
            "reason_tag": row["reason_tag"],
            "note": row["note"],
            "review_time_ms": row["review_time_ms"],
            "review_time_seconds": "%.3f" % (int(row["review_time_ms"]) / 1000.0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    path = sample_paths(sample)["export"]
    fields = [
        "review_id", "item_key", "sample_name", "reviewer_id", "mode",
        "review_status", "chosen_labels", "human_primary_flags",
        "chosen_source", "reason_tag", "note", "review_time_ms",
        "review_time_seconds", "created_at", "updated_at",
    ]
    write_csv_rows(path, out, fields)
    return path, out


def write_timing_artifacts():
    paths = timing_paths()
    con = connect_db()
    try:
        event_rows = con.execute(
            """
            SELECT id, sample_name, review_id, reviewer, mode, status,
                   chosen_source, reason_tag, note, review_time_ms, created_at
            FROM events
            ORDER BY id
            """
        ).fetchall()
    finally:
        con.close()

    events = []
    summary = {}
    for row in event_rows:
        ms = int(row["review_time_ms"] or 0)
        seconds = ms / 1000.0
        event = {
            "event_id": row["id"],
            "sample_name": row["sample_name"],
            "review_id": row["review_id"],
            "reviewer_id": row["reviewer"],
            "mode": row["mode"],
            "review_status": row["status"],
            "chosen_source": row["chosen_source"],
            "reason_tag": row["reason_tag"],
            "note": row["note"],
            "review_time_ms": ms,
            "review_time_seconds": "%.3f" % seconds,
            "created_at": row["created_at"],
        }
        events.append(event)
        reviewer = row["reviewer"]
        item = summary.setdefault(
            reviewer,
            {
                "reviewer_id": reviewer,
                "event_count": 0,
                "reviewed_count": 0,
                "skipped_count": 0,
                "unclear_count": 0,
                "denied_count": 0,
                "total_review_time_ms": 0,
                "total_review_time_seconds": 0.0,
                "avg_review_time_seconds": 0.0,
                "first_event_at": row["created_at"],
                "last_event_at": row["created_at"],
            },
        )
        item["event_count"] += 1
        if row["status"] == "reviewed":
            item["reviewed_count"] += 1
        elif row["status"] == "skipped":
            item["skipped_count"] += 1
        elif row["status"] == "unclear":
            item["unclear_count"] += 1
        elif row["status"] == "denied":
            item["denied_count"] += 1
        item["total_review_time_ms"] += ms
        item["total_review_time_seconds"] += seconds
        if row["created_at"] < item["first_event_at"]:
            item["first_event_at"] = row["created_at"]
        if row["created_at"] > item["last_event_at"]:
            item["last_event_at"] = row["created_at"]

    for item in summary.values():
        item["total_review_time_seconds"] = round(item["total_review_time_seconds"], 3)
        item["avg_review_time_seconds"] = round(
            item["total_review_time_seconds"] / item["event_count"], 3
        ) if item["event_count"] else 0.0

    event_fields = [
        "event_id", "sample_name", "review_id", "reviewer_id", "mode",
        "review_status", "chosen_source", "reason_tag", "note",
        "review_time_ms", "review_time_seconds", "created_at",
    ]
    summary_fields = [
        "reviewer_id", "event_count", "reviewed_count", "skipped_count",
        "unclear_count", "denied_count", "total_review_time_ms", "total_review_time_seconds",
        "avg_review_time_seconds", "first_event_at", "last_event_at",
    ]
    write_csv_rows(paths["events_csv"], events, event_fields)
    summary_rows = sorted(summary.values(), key=lambda item: item["reviewer_id"].lower())
    write_csv_rows(paths["summary_csv"], summary_rows, summary_fields)
    paths["summary_json"].write_text(json.dumps(summary_rows, indent=2, sort_keys=True), encoding="utf-8")
    return paths, events, summary_rows


def binom_cdf(k, n, p):
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0 if k < n else 1.0
    q = 1.0 - p
    prob = q ** n
    total = prob
    for i in range(1, k + 1):
        prob *= (n - i + 1) / float(i) * p / q
        total += prob
    return min(max(total, 0.0), 1.0)


def cp_upper(k, n, alpha=0.05):
    if n <= 0:
        return None
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if binom_cdf(k, n, mid) >= alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def score_sample(sample, mode=None, reviewer=None):
    export_path, rows = export_reviews(sample)
    answers = load_answers(sample)
    verdicts = []
    methods = ("hybrid", "deterministic")
    for row in rows:
        if mode and row["mode"] != mode:
            continue
        if reviewer and row["reviewer_id"] != reviewer:
            continue
        answer = answers.get(row["review_id"], {})
        human = set(split_flags(row.get("human_primary_flags")))
        if row["review_status"] != "reviewed" or not human:
            continue
        verdict = dict(row)
        for method in methods:
            model = set(split_flags(answer.get(method + "_labels")))
            verdict[method + "_exact"] = str(human == model).lower()
            verdict[method + "_model_missing_human_flags"] = ";".join(sorted(human - model))
            verdict[method + "_model_extra_flags"] = ";".join(sorted(model - human))
        verdicts.append(verdict)
    fields = []
    for row in verdicts:
        for key in row:
            if key not in fields:
                fields.append(key)
    paths = sample_paths(sample)
    write_csv_rows(paths["verdicts"], verdicts, fields)
    summary_methods = []
    for method in methods:
        reviewed = [r for r in verdicts if r.get(method + "_exact")]
        n = len(reviewed)
        errors = sum(1 for r in reviewed if r[method + "_exact"] == "false")
        upper = cp_upper(errors, n)
        summary_methods.append({
            "method": method,
            "mode_filter": mode or "all",
            "reviewed_n": n,
            "strict_exact_errors": errors,
            "strict_exact_accuracy": None if n == 0 else (n - errors) / float(n),
            "one_sided_95_upper_error": upper,
            "supports_error_lt_1pct": bool(upper is not None and upper < 0.01),
        })
    summary = {
        "created_utc": utc_now(),
        "sample": sample,
        "mode_filter": mode or "all",
        "reviewer_filter": reviewer or "all",
        "export_csv": str(export_path),
        "verdicts_csv": str(paths["verdicts"]),
        "methods": summary_methods,
    }
    paths["summary"].write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# 177M Human Eval Score",
        "",
        "- Sample: `%s`" % sample,
        "- Mode filter: `%s`" % (mode or "all"),
        "- Reviewer filter: `%s`" % (reviewer or "all"),
        "- Export CSV: `%s`" % export_path,
        "",
        "| method | reviewed n | errors | accuracy | one-sided 95% upper error | <1% supported |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in summary_methods:
        acc = "" if item["strict_exact_accuracy"] is None else "%.4f" % item["strict_exact_accuracy"]
        upper = "" if item["one_sided_95_upper_error"] is None else "%.4f%%" % (100 * item["one_sided_95_upper_error"])
        lines.append(
            "| %(method)s | %(reviewed_n)s | %(strict_exact_errors)s | " % item
            + "%s | %s | %s |" % (acc, upper, item["supports_error_lt_1pct"])
        )
    paths["report"].write_text("\n".join(lines), encoding="utf-8")
    return summary


def mode_job(mode):
    if mode == "blind":
        return "Blind review"
    if mode == "fast":
        return "Guided review"
    return mode or "Review"


def mode_description(mode):
    if mode == "blind":
        return "Assign categories without seeing model suggestions."
    if mode == "fast":
        return "Check the model suggestion, accept it when correct, or correct it."
    return "Review permits and save human labels."


def sample_label(sample):
    if sample in TASK_TITLES:
        return TASK_TITLES[sample]
    size = infer_sample_size(sample)
    return "Random sample %s" % f"{size:,}" if size else sample


def review_history():
    totals = {sample: sample_status(sample)["reviewable_count"] for sample in SAMPLES}
    grouped = {}
    con = connect_db()
    try:
        rows = con.execute(
            """
            SELECT sample_name, reviewer, mode, status, COUNT(*) AS n, MAX(updated_at) AS last_updated
            FROM reviews
            GROUP BY sample_name, reviewer, mode, status
            """
        ).fetchall()
    finally:
        con.close()

    for row in rows:
        sample = row["sample_name"]
        reviewer = row["reviewer"]
        mode = row["mode"]
        assigned_total = totals.get(sample, 0)
        if sample in TASK_SAMPLES:
            assigned_total = len(assigned_items(sample, reviewer)[0])
        key = (sample, reviewer, mode)
        item = grouped.setdefault(
            key,
            {
                "sample": sample,
                "sample_label": sample_label(sample),
                "reviewer": reviewer,
                "mode": mode,
                "job": mode_job(mode),
                "job_description": mode_description(mode),
                "total": assigned_total,
                "saved_count": 0,
                "reviewed_count": 0,
                "skipped_count": 0,
                "unclear_count": 0,
                "denied_count": 0,
                "last_updated": "",
            },
        )
        n = int(row["n"])
        status = row["status"]
        item["saved_count"] += n
        if status == "reviewed":
            item["reviewed_count"] += n
        elif status == "skipped":
            item["skipped_count"] += n
        elif status == "unclear":
            item["unclear_count"] += n
        elif status == "denied":
            item["denied_count"] += n
        if row["last_updated"] and row["last_updated"] > item["last_updated"]:
            item["last_updated"] = row["last_updated"]

    items = list(grouped.values())
    for item in items:
        total = item["total"]
        item["progress_pct"] = (100.0 * item["saved_count"] / total) if total else 0.0
    items.sort(key=lambda row: row.get("last_updated") or "", reverse=True)
    return items


def handle_upload(handler, sample):
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        json_response(handler, {"ok": False, "error": "expected_multipart"}, 400)
        return
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length > MAX_UPLOAD_BYTES:
        json_response(handler, {"ok": False, "error": "upload_too_large"}, 413)
        return
    body = handler.rfile.read(length)
    mime = (
        "Content-Type: " + content_type + "\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=email_policy).parsebytes(mime)
    raw = None
    if message.is_multipart():
        for part in message.iter_parts():
            disposition = part.get("Content-Disposition", "")
            if part.get_param("name", header="Content-Disposition") == "file" or 'name="file"' in disposition:
                raw = part.get_payload(decode=True)
                break
    if raw is None:
        json_response(handler, {"ok": False, "error": "missing_file"}, 400)
        return
    # Basic validation: must have review_id and some description field.
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if "review_id" not in headers:
        json_response(handler, {"ok": False, "error": "missing_review_id"}, 400)
        return
    if not any(h in headers for h in ("description", "source_description", "permit_description", "work_description", "job_description")):
        json_response(handler, {"ok": False, "error": "missing_description_column"}, 400)
        return
    path = sample_paths(sample)["joined"]
    path.write_bytes(raw)
    json_response(handler, {"ok": True, "path": str(path)})


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "CANDYEval177M/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def parsed(self):
        return urllib.parse.urlparse(self.path)

    def query(self):
        return urllib.parse.parse_qs(self.parsed().query)

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        try:
            path = self.parsed().path
            if path == "/":
                text_response(self, app_shell(), 200, "text/html; charset=utf-8")
            elif path == "/test":
                file_response(self, APP_DIR / "showcase.html")
            elif path == "/legacy-help":
                legacy = HELP_MODAL_HTML_V1.replace('class="help-overlay" hidden', 'class="help-overlay"')
                text_response(self, "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Reviewer guide v1</title><link rel='stylesheet' href='/static/styles.css'></head><body>" + legacy + "</body></html>", 200, "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                file_response(self, APP_DIR / path.lstrip("/"))
            elif path == "/api/health":
                con = connect_db()
                try:
                    reviews = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
                    assignments = con.execute("SELECT COUNT(*) FROM active_assignments").fetchone()[0]
                    events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                finally:
                    con.close()
                json_response(self, {
                    "ok": True,
                    "service": "staticver-human-eval",
                    "port": PORT,
                    "reviews": reviews,
                    "activeAssignments": assignments,
                    "timingEvents": events,
                    "db": str(DB_PATH),
                })
            elif path == "/api/status":
                json_response(self, {"ok": True, "root": str(VALIDATION_ROOT), "samples": [sample_status(s) for s in SAMPLES]})
            elif path == "/api/history":
                json_response(self, {"ok": True, "history": review_history()})
            elif path == "/api/mytasks":
                q = self.query()
                reviewer = ((q.get("reviewer") or [""])[0] or "").strip()
                if not reviewer:
                    json_response(self, {"ok": False, "error": "reviewer_required"}, 400)
                else:
                    json_response(self, {"ok": True, **reviewer_tasks(reviewer)})
            elif path == "/api/taxonomy":
                rows, _ = load_taxonomy()
                json_response(self, {"ok": True, "taxonomy": rows})
            elif path.startswith("/api/next/"):
                sample = path.split("/")[-1]
                q = self.query()
                reviewer = ((q.get("reviewer") or [""])[0] or "").strip()
                mode = (q.get("mode") or ["fast"])[0]
                if not reviewer:
                    json_response(self, {"ok": False, "error": "reviewer_required", "message": "Type your reviewer name before starting."}, 400)
                else:
                    payload, status = next_item(sample, reviewer, mode)
                    json_response(self, payload, status)
            elif path.startswith("/api/item/"):
                sample = path.split("/")[-1]
                q = self.query()
                reviewer = ((q.get("reviewer") or [""])[0] or "").strip()
                mode = (q.get("mode") or ["fast"])[0]
                index = int((q.get("index") or ["0"])[0] or "0")
                if not reviewer:
                    json_response(self, {"ok": False, "error": "reviewer_required", "message": "Type your reviewer name before starting."}, 400)
                else:
                    payload, status = item_at_index(sample, reviewer, mode, index)
                    json_response(self, payload, status)
            elif path.startswith("/api/export/"):
                sample = path.split("/")[-1]
                export_path, rows = export_reviews(sample)
                json_response(self, {"ok": True, "path": str(export_path), "rows": len(rows)})
            elif path.startswith("/api/score/"):
                sample = path.split("/")[-1]
                q = self.query()
                mode = (q.get("mode") or [""])[0] or None
                reviewer = (q.get("reviewer") or [""])[0] or None
                summary = score_sample(sample, mode, reviewer)
                json_response(self, {"ok": True, "summary": summary})
            else:
                text_response(self, "Not found", 404)
        except Exception:
            traceback.print_exc()
            json_response(self, {"ok": False, "error": "server_error", "trace": traceback.format_exc()}, 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        try:
            path = self.parsed().path
            if path == "/api/review":
                payload, status = save_review(self.json_body())
                json_response(self, payload, status)
            elif path.startswith("/api/upload/"):
                sample = path.split("/")[-1]
                if sample not in SAMPLES:
                    json_response(self, {"ok": False, "error": "bad_sample"}, 400)
                else:
                    handle_upload(self, sample)
            else:
                text_response(self, "Not found", 404)
        except Exception:
            traceback.print_exc()
            json_response(self, {"ok": False, "error": "server_error", "trace": traceback.format_exc()}, 500)


def main():
    ensure_dirs()
    connect_db().close()
    print("CANDY 177M human eval")
    print("Validation root:", VALIDATION_ROOT)
    print("Database:", DB_PATH)
    print("Taxonomy:", TAXONOMY_PATH)
    print("Listening on http://%s:%s" % (HOST, PORT))
    ThreadedHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
