#!/usr/bin/env python3
"""Build the self-contained permit bundle for the static reviewer."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
MIT_ROOT = APP_ROOT.parent
VALIDATION_ROOT = MIT_ROOT / "validation_177m_20260602"
DB_PATH = MIT_ROOT / "human_eval_webapp_177m" / "instance" / "audit.sqlite3"
PRIMARY_QUEUE = (
    VALIDATION_ROOT
    / "release_versions"
    / "taxonomy81-sota-candidate-v0.13.0-real-human-calibrated"
    / "professional_lockbox_review_package_ready"
    / "global_all_permit_lockbox_review_queue.csv"
)
RAW_ROOT = (
    VALIDATION_ROOT
    / "release_versions"
    / "taxonomy81-v15-learned-supervised"
    / "permit150m_20260908"
    / "data"
)
QUEUE_PATHS = [
    PRIMARY_QUEUE,
    PRIMARY_QUEUE.parent / "category_stratified_review_queue.csv",
    VALIDATION_ROOT / "release_versions" / "taxonomy81-sota-candidate-v0.12.0" / "remote_v12_duckdb_smoke_250" / "review_package_smoke" / "global_all_permit_lockbox_review_queue.csv",
    VALIDATION_ROOT / "release_versions" / "taxonomy81-sota-candidate-v0.12.0" / "remote_v12_duckdb_smoke_250" / "review_package_smoke" / "category_stratified_review_queue.csv",
]
OUT_PATH = APP_ROOT / "data" / "permits.json"
MANIFEST_PATH = APP_ROOT / "data" / "manifest.json"

TARGETS = [
    ("ALTERATION_REMODEL", "Alteration / remodel"),
    ("BATTERY_STORAGE", "Battery storage"),
    ("DECK_PATIO_PORCH", "Deck / patio / porch"),
    ("DEMOLITION", "Demolition"),
    ("DRIVEWAY_PAVING", "Driveway / paving"),
    ("ELECTRICAL_PANEL_UPGRADE", "Electrical panel upgrade"),
    ("EV_CHARGER", "EV charger"),
    ("FENCE_WALL", "Fence / wall"),
    ("FIRE_PROTECTION", "Fire protection"),
    ("GARAGE_ACCESSORY", "Garage / accessory"),
    ("GAS", "Gas"),
    ("GRADING_SITEWORK", "Grading / sitework"),
    ("MECHANICAL_HVAC", "Mechanical / HVAC"),
    ("NEW_CONSTRUCTION", "New construction"),
    ("PLUMBING", "Plumbing"),
    ("ROOFING", "Roofing"),
    ("SEWER_UTILITY", "Sewer / utility"),
    ("SIGN", "Sign"),
    ("STRUCTURAL", "Structural"),
    ("WATER_HEATER", "Water heater"),
    ("WINDOW_DOOR", "Window / door"),
]
LABELS = dict(TARGETS)

# Fine taxonomy labels are the preferred evidence. The keyword rules supply
# recall for broad queues that do not have a one-to-one fine label.
FINE_RULES = {
    "BATTERY_STORAGE": ("battery_storage", "resilient_solar_storage"),
    "DECK_PATIO_PORCH": ("patios", "decks", "porches"),
    "DEMOLITION": ("full_demolition", "partial_demolition"),
    "DRIVEWAY_PAVING": ("driveways", "paving"),
    "ELECTRICAL_PANEL_UPGRADE": ("electrical_panel", "electrical_work"),
    "EV_CHARGER": ("electric_vehicle_infrastructure", "ev_charger"),
    "FENCE_WALL": ("fences", "walls"),
    "FIRE_PROTECTION": ("fire_protection", "fire_alarm", "sprinkler"),
    "GARAGE_ACCESSORY": ("garage_construction", "accessory_structure"),
    "MECHANICAL_HVAC": ("mechanical_work", "air_source_heat_pumps", "heat_pump"),
    "NEW_CONSTRUCTION": ("new_construction",),
    "PLUMBING": ("plumbing",),
    "ROOFING": ("reroof", "roofing", "roof"),
    "SEWER_UTILITY": ("utility_connection", "sewer", "water_service"),
    "SIGN": ("signage", "sign"),
    "STRUCTURAL": ("structural_work", "foundation", "load_bearing"),
    "WATER_HEATER": ("water_heater",),
    "WINDOW_DOOR": ("windows_glazing", "window", "door"),
    "ALTERATION_REMODEL": ("interior_remodel", "tenant_improvement", "home_additions", "interior_partitions"),
    "GAS": ("gas",),
    "GRADING_SITEWORK": ("grading", "sitework", "rain_garden", "bioswale", "drainage"),
}

KEYWORDS = {
    "BATTERY_STORAGE": r"battery|powerwall|energy storage|solar storage",
    "DECK_PATIO_PORCH": r"\bdeck\b|patio|porch|terrace",
    "DEMOLITION": r"demolition|demo\b|demolish|remove (the )?(existing )?(house|building|structure)",
    "DRIVEWAY_PAVING": r"driveway|paving|pavement|asphalt|concrete approach",
    "ELECTRICAL_PANEL_UPGRADE": r"electrical panel|service upgrade|service change|breaker panel|main panel|meter upgrade",
    "EV_CHARGER": r"electric vehicle|\bev\b|car charger|vehicle charging|evse|tesla charger",
    "FENCE_WALL": r"\bfence\b|fencing|retaining wall|block wall|privacy wall",
    "FIRE_PROTECTION": r"fire alarm|fire sprinkler|sprinkler system|fire protection|smoke detector",
    "GARAGE_ACCESSORY": r"garage|carport|accessory structure|detached structure|adu\b",
    "GAS": r"\bgas\b|gas line|gas piping|propane|natural gas",
    "GRADING_SITEWORK": r"grading|sitework|earthwork|drainage|stormwater|bioswale|rain garden|excavat",
    "MECHANICAL_HVAC": r"hvac|air condition|a/c|furnace|heat pump|mechanical|ductwork|condenser|boiler",
    "NEW_CONSTRUCTION": r"new construction|new residential|new dwelling|new single family|new commercial",
    "PLUMBING": r"plumb|water line|fixture|backflow|sewer lateral|septic",
    "ROOFING": r"roof|reroof|re-roof|shingle|tile roof",
    "SEWER_UTILITY": r"sewer|utility connection|water service|utility trench|tap connection",
    "SIGN": r"\bsign\b|signage|monument sign|channel letter|billboard",
    "STRUCTURAL": r"structural|foundation|load bearing|framing|beam|footing|seismic",
    "WATER_HEATER": r"water heater|tankless heater|hot water heater",
    "WINDOW_DOOR": r"window|glazing|door replacement|exterior door|sliding door",
    "ALTERATION_REMODEL": r"remodel|renovation|alteration|tenant improvement|interior finish|addition|partition",
}


def compact(value: str | None, limit: int = 600) -> str:
    value = re.sub(r"\s+", " ", (value or "")).strip()
    return value[:limit]


def original_ids_from_db() -> set[str]:
    """Collect every reviewed source id, including V4 targeted wrappers."""
    found: set[str] = set()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT sample_name, review_id FROM reviews").fetchall()
    for sample, review_id in rows:
        for value in (sample, review_id):
            if value:
                found.add(str(value).strip())
        rid = str(review_id or "")
        if "__" in rid:
            found.add(rid.split("__", 1)[1])
        sample_text = str(sample or "")
        if "__" in sample_text:
            found.add(sample_text.split("__", 1)[-1])
    return found


def load_rows() -> list[dict[str, str]]:
    paths = [p for p in QUEUE_PATHS if p.exists()]
    if not paths:
        raise FileNotFoundError("No lockbox queue was found")
    rows: list[dict[str, str]] = []
    seen_rows: set[tuple[str, str]] = set()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                stable = compact(row.get("stable_permit_identifier"), 300)
                review_id = compact(row.get("review_id"), 300)
                key = (stable or review_id, compact(row.get("source_description"), 600))
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                row["_source_file"] = str(path)
                rows.append(row)
    return rows


def _raw_value(row: dict[str, str], names: tuple[str, ...]) -> str:
    lowered = {str(k).strip().lower(): (v or "") for k, v in row.items()}
    for name in names:
        for key, value in lowered.items():
            if name in key and value.strip():
                return value.strip()
    return ""


def load_raw_rows(missing_codes: set[str], existing_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Add a small, evidence-backed fallback from the latest raw exports."""
    if not missing_codes or not RAW_ROOT.exists():
        return []
    patterns = [KEYWORDS[code] for code in missing_codes]
    pattern = "|".join(f"(?:{value})" for value in patterns)
    try:
        result = subprocess.run(
            ["rg", "-i", "-l", "--glob", "*.csv", "-e", pattern, str(RAW_ROOT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    files = [Path(line) for line in result.stdout.splitlines() if line.strip()]
    # Raw source files vary from small monthly extracts to multi-gigabyte
    # archives. The compact files are sufficient for filling a small shortage.
    files = sorted(
        (path for path in files if path.exists() and path.stat().st_size <= 25_000_000),
        key=lambda path: (path.stat().st_size, str(path)),
    )[:180]
    known_signatures = {
        "|".join(
            compact(row.get(key), 180).lower()
            for key in ("source_state", "source_city", "permit_number", "source_description")
        )
        for row in existing_rows
    }
    raw_rows: list[dict[str, str]] = []
    raw_match_count = Counter()
    for path in files:
        try:
            handle = path.open("r", encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.DictReader(handle)
            for line_number, source in enumerate(reader, start=2):
                description = " ".join(
                    part
                    for part in (
                        _raw_value(source, ("description", "work type", "worktype", "project name", "permit name", "short notes", "record type")),
                        _raw_value(source, ("application type", "permit type", "type")),
                    )
                    if part
                )
                permit_number = _raw_value(source, ("permit number", "record number", "record id", "permit id", "id"))
                address = _raw_value(source, ("address", "location", "permit address", "site address"))
                signature = "|".join(
                    compact(value, 180).lower()
                    for value in ("", "", permit_number, description)
                )
                if not description or signature in known_signatures:
                    continue
                raw = {
                    "review_id": "raw_" + hashlib.sha1(f"{path}:{line_number}".encode()).hexdigest()[:20],
                    "stable_permit_identifier": "raw_" + hashlib.sha1(f"{path}:{line_number}:{permit_number}".encode()).hexdigest()[:24],
                    "source_state": "",
                    "source_city": "",
                    "source_county": "",
                    "source_jurisdiction": path.parent.name,
                    "permit_number": permit_number,
                    "source_description": description,
                    "source_permit_type": _raw_value(source, ("permit type", "record type", "application type", "type")),
                    "source_work_type": _raw_value(source, ("work type", "worktype", "project name", "permit name")),
                    "candidate_predicted_flags": "",
                    "_source_file": str(path),
                }
                if any(evidence(raw, code)[0] > 0 for code in missing_codes):
                    raw_rows.append(raw)
                    known_signatures.add(signature)
                    for code in missing_codes:
                        if evidence(raw, code)[0] > 0:
                            raw_match_count[code] += 1
                if any(raw_match_count[code] >= 100 for code in missing_codes):
                    handle.close()
                    return raw_rows
            handle.close()
        except (OSError, UnicodeError, csv.Error):
            continue
    return raw_rows


def evidence(row: dict[str, str], code: str) -> tuple[int, str]:
    target = row.get("target_flag", "").lower()
    predicted = row.get("candidate_predicted_flags", "").lower()
    description = " ".join(
        row.get(k, "")
        for k in ("source_description", "source_permit_type", "source_work_type")
    ).lower()
    terms = FINE_RULES.get(code, ())
    score = 0
    basis: list[str] = []
    for term in terms:
        if term in target:
            score += 100
            basis.append("target flag")
        elif term in predicted:
            score += 50
            basis.append("candidate flag")
        elif term in description:
            score += 10
            basis.append("description")
    match = re.search(KEYWORDS[code], description, flags=re.I)
    if match:
        score += 5
        basis.append("description keyword")
    return score, ", ".join(dict.fromkeys(basis))


def permit_key(row: dict[str, str]) -> str:
    stable = compact(row.get("stable_permit_identifier"), 300)
    if stable:
        return stable
    return f"{compact(row.get('source_state'), 30)}|{compact(row.get('source_city'), 80)}|{compact(row.get('permit_number'), 100)}|{compact(row.get('review_id'), 200)}"


def is_reviewed(row: dict[str, str], reviewed: set[str]) -> bool:
    values = {
        compact(row.get("stable_permit_identifier"), 300),
        compact(row.get("review_id"), 300),
    }
    for value in list(values):
        if "__" in value:
            values.add(value.split("__", 1)[-1])
    return bool(values & reviewed)


def select_bundle(rows: list[dict[str, str]], reviewed: set[str]) -> tuple[list[dict[str, object]], dict[str, int]]:
    # Keep one source row per permit. A permit may appear in two category queues
    # only when it has strong evidence for both categories.
    unique: dict[str, dict[str, str]] = {}
    excluded = 0
    for row in rows:
        key = permit_key(row)
        if is_reviewed(row, reviewed):
            excluded += 1
            continue
        old = unique.get(key)
        if old is None or len(row.get("candidate_predicted_flags", "")) > len(old.get("candidate_predicted_flags", "")):
            unique[key] = row

    by_category: dict[str, list[tuple[int, str, dict[str, str]]]] = defaultdict(list)
    for row in unique.values():
        for code, _label in TARGETS:
            score, basis = evidence(row, code)
            if score > 0:
                by_category[code].append((score, basis, row))

    selected: list[dict[str, object]] = []
    globally_selected: set[str] = set()
    used_categories: Counter[str] = Counter()
    # Allocate scarce categories first. This keeps the final bundle close to
    # 1,050 distinct permits and uses cross-category overlap only as needed.
    category_plan = sorted(TARGETS, key=lambda item: (len(by_category[item[0]]), item[0]))
    for code, label in category_plan:
        candidates = sorted(
            by_category[code],
            key=lambda item: (-item[0], permit_key(item[2])),
        )
        chosen: list[tuple[int, str, dict[str, str]]] = []
        for item in candidates:
            if len(chosen) >= 50:
                break
            if permit_key(item[2]) not in globally_selected:
                chosen.append(item)
        # A few source pools do not have 50 distinct rows after exclusions.
        # Fill only the remaining slots with evidence-backed cross-category
        # permits so every queue remains usable.
        for item in candidates:
            if len(chosen) >= 50:
                break
            if item not in chosen:
                chosen.append(item)
        if len(chosen) < 50:
            raise RuntimeError(f"Only {len(chosen)} eligible permits found for {label}; 50 are required")
        for score, basis, row in chosen:
            key = permit_key(row)
            globally_selected.add(key)
            selected.append(
                {
                    "id": key,
                    "category": code,
                    "categoryLabel": label,
                    "description": compact(row.get("source_description"), 700),
                    "permitNumber": compact(row.get("permit_number"), 120),
                    "permitType": compact(row.get("source_permit_type"), 160),
                    "workType": compact(row.get("source_work_type"), 160),
                    "state": compact(row.get("source_state"), 40),
                    "city": compact(row.get("source_city"), 120),
                    "county": compact(row.get("source_county"), 120),
                    "jurisdiction": compact(row.get("source_jurisdiction"), 160),
                    "sourceReviewId": compact(row.get("review_id"), 300),
                    "selectionBasis": basis,
                    "selectionScore": score,
                }
            )
            used_categories[key] += 1

    stats = {
        "sourceRows": len(rows),
        "uniqueEligiblePermits": len(unique),
        "rowsExcludedAsPreviouslyReviewed": excluded,
        "categorySlots": len(selected),
        "uniquePermitsInBundle": len({str(item["id"]) for item in selected}),
        "overlapPermitCount": sum(1 for count in used_categories.values() if count > 1),
    }
    return selected, stats


def main() -> None:
    reviewed = original_ids_from_db()
    rows = load_rows()
    raw_added = 0
    for _ in range(len(TARGETS) * 2):
        try:
            permits, stats = select_bundle(rows, reviewed)
            break
        except RuntimeError as error:
            print(f"queue pass {_ + 1}: {error}", flush=True)
            match = re.search(r"for (.*?); 50", str(error))
            missing_label = match.group(1) if match else ""
            missing_codes = {code for code, label in TARGETS if label == missing_label}
            additions = load_raw_rows(missing_codes, rows)
            if not additions:
                raise RuntimeError(f"No additional raw permits found for {missing_label}") from error
            rows.extend(additions)
            raw_added += len(additions)
    else:
        raise RuntimeError("Unable to complete the 50-per-category export")
    category_counts = Counter(str(item["category"]) for item in permits)
    manifest = {
        "formatVersion": 1,
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "staticMode": True,
        "reviewerSlotsPerPermit": 2,
        "permitsPerCategory": 50,
        "categoryCount": len(TARGETS),
        "categorySlotCount": len(permits),
        "uniquePermitCount": len({str(item["id"]) for item in permits}),
        "categories": [
            {"code": code, "label": label, "permitCount": category_counts[code]}
            for code, label in TARGETS
        ],
        "source": "validation_177m_20260602/release_versions/taxonomy81-sota-candidate-v0.13.0-real-human-calibrated/professional_lockbox_review_package_ready/global_all_permit_lockbox_review_queue.csv",
        "excludedReviewDatabase": "human_eval_webapp_177m/instance/audit.sqlite3 (read locally during build; not copied)",
        "stats": stats,
        "rawFallbackRowsAdded": raw_added,
        "sourceQueueFiles": sorted(
            {
                str(Path(row["_source_file"]).resolve().relative_to(MIT_ROOT))
                if str(row.get("_source_file", "")).startswith(str(MIT_ROOT))
                else str(row.get("_source_file", ""))
                for row in rows
                if row.get("_source_file")
            }
        ),
        "coordinationNote": "Static localStorage state only; shared cross-device claims require a backend.",
    }
    OUT_PATH.write_text(json.dumps({"permits": permits}, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": manifest, "reviewedIds": len(reviewed)}, indent=2))


if __name__ == "__main__":
    main()
