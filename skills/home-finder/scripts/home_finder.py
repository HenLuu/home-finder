#!/usr/bin/env python3
"""Small, standard-library-only CLI for a local home-finder catalog."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import unicodedata
from typing import Any


TOP_KEYS = {"schema_version", "sources", "locations", "items"}
SOURCE_KEYS = {"file", "recorded_at"}
LOCATION_KEYS = {"path", "steps", "photos"}
PHOTO_KEYS = {"file", "at", "label"}
ITEM_KEYS = {"id", "name", "aliases", "location", "detail", "recorded_at", "evidence", "history"}
SNAPSHOT_KEYS = {"name", "aliases", "location", "detail", "recorded_at", "evidence"}
EVIDENCE_KEYS = {"kind", "source", "at", "photos", "note"}
EVIDENCE_KINDS = {"visual", "spoken", "visual+spoken", "user_report"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_POINT_RE = re.compile(r"^(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?$")
TIME_SEPARATOR_RE = re.compile(r"\s*(?:[-–—~～,，、;/；+&]|至|到|\b(?:and|to)\b)\s*", re.IGNORECASE)


class CliError(Exception):
    pass


def fail(message: str) -> None:
    raise CliError(message)


def is_date(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        if DATE_RE.fullmatch(value):
            dt.date.fromisoformat(value)
        else:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def is_timeline(value: Any) -> bool:
    """Accept a source-media timepoint or a simple range/combination of them."""
    if not isinstance(value, str) or not value.strip():
        return False
    parts = TIME_SEPARATOR_RE.split(value.strip())
    return bool(parts) and all(TIME_POINT_RE.fullmatch(part.strip()) for part in parts)


def is_evidence_time(value: Any, kind: Any) -> bool:
    if kind == "user_report":
        return is_date(value)
    return is_timeline(value)


def require_string(value: Any, label: str, *, nonempty: bool = True) -> bool:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        return False
    return True


def exact_keys(obj: Any, expected: set[str], label: str, errors: list[str]) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{label}: expected object")
        return False
    actual = set(obj)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        errors.append(f"{label}: fields must be exactly {sorted(expected)} ({'; '.join(detail)})")
        return False
    return True


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def safe_photo_path(raw_path: Any, catalog_dir: Path, label: str, errors: list[str]) -> None:
    if not require_string(raw_path, label):
        errors.append(f"{label}: file must be a non-empty relative path")
        return
    # Paths are catalog-relative POSIX paths. Reject alternate separators and
    # traversal before resolving symlinks and checking containment.
    if "\\" in raw_path:
        errors.append(f"{label}: backslashes are not allowed in photo paths")
        return
    rel = PurePosixPath(raw_path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in raw_path.split("/")):
        errors.append(f"{label}: photo path must stay relative to the catalog directory")
        return
    try:
        root = catalog_dir.resolve(strict=True)
        target = (root / Path(*rel.parts)).resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        errors.append(f"{label}: photo file is missing or escapes the catalog directory")
        return
    if not target.is_file():
        errors.append(f"{label}: photo path is not a file")


def validate_photo(photo: Any, catalog_dir: Path, label: str, errors: list[str]) -> None:
    if not exact_keys(photo, PHOTO_KEYS, label, errors):
        return
    if not require_string(photo["file"], f"{label}.file"):
        errors.append(f"{label}.file: expected non-empty string")
    else:
        safe_photo_path(photo["file"], catalog_dir, f"{label}.file", errors)
    if not is_timeline(photo["at"]):
        errors.append(f"{label}.at: expected a media timepoint or time range/combination")
    if not require_string(photo["label"], f"{label}.label"):
        errors.append(f"{label}.label: expected non-empty string")


def validate_photos(photos: Any, catalog_dir: Path, label: str, errors: list[str]) -> None:
    if not isinstance(photos, list):
        errors.append(f"{label}: expected array")
        return
    for index, photo in enumerate(photos):
        validate_photo(photo, catalog_dir, f"{label}[{index}]", errors)


def validate_evidence(evidence: Any, source_ids: set[str], catalog_dir: Path, label: str, errors: list[str]) -> None:
    if not exact_keys(evidence, EVIDENCE_KEYS, label, errors):
        return
    kind = evidence["kind"]
    kind_valid = isinstance(kind, str) and kind in EVIDENCE_KINDS
    if not kind_valid:
        errors.append(f"{label}.kind: expected one of {sorted(EVIDENCE_KINDS)}")
    source = evidence["source"]
    if source is not None:
        if not require_string(source, f"{label}.source") or source not in source_ids:
            errors.append(f"{label}.source: source ID is not present in sources")
    if not is_evidence_time(evidence["at"], kind):
        expected = "an ISO date" if kind == "user_report" else "a media timepoint or time range/combination"
        errors.append(f"{label}.at: expected {expected}")
    validate_photos(evidence["photos"], catalog_dir, f"{label}.photos", errors)
    if not isinstance(evidence["note"], str):
        errors.append(f"{label}.note: expected string")
    if kind_valid and kind in {"visual", "visual+spoken"} and isinstance(evidence["photos"], list) and not evidence["photos"]:
        errors.append(f"{label}.photos: visual evidence requires at least one photo")
    if kind == "user_report" and (source is not None or evidence["photos"] != []):
        errors.append(f"{label}: user_report evidence must have source=null and photos=[]")


def validate_snapshot(snapshot: Any, source_ids: set[str], location_ids: set[str], catalog_dir: Path, label: str, errors: list[str]) -> None:
    if not exact_keys(snapshot, SNAPSHOT_KEYS, label, errors):
        return
    if not require_string(snapshot["name"], f"{label}.name"):
        errors.append(f"{label}.name: expected non-empty string")
    if not isinstance(snapshot["aliases"], list) or any(not require_string(a, f"{label}.aliases") for a in snapshot["aliases"]):
        errors.append(f"{label}.aliases: expected array of non-empty strings")
    if not isinstance(snapshot["location"], str) or snapshot["location"] not in location_ids:
        errors.append(f"{label}.location: location ID is not present in locations")
    if not isinstance(snapshot["detail"], str):
        errors.append(f"{label}.detail: expected string")
    if not is_date(snapshot["recorded_at"]):
        errors.append(f"{label}.recorded_at: expected an ISO date or datetime")
    validate_evidence(snapshot["evidence"], source_ids, catalog_dir, f"{label}.evidence", errors)


def validate_catalog(data: Any, catalog_dir: Path) -> list[str]:
    errors: list[str] = []
    if not exact_keys(data, TOP_KEYS, "catalog", errors):
        return errors
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        errors.append("catalog.schema_version: expected 1")

    sources = data["sources"]
    if not isinstance(sources, dict):
        errors.append("catalog.sources: expected object")
        sources = {}
    for source_id, source in sources.items():
        label = f"sources[{source_id!r}]"
        if not require_string(source_id, "source ID"):
            errors.append(f"{label}: source ID must be non-empty string")
        if exact_keys(source, SOURCE_KEYS, label, errors):
            if not require_string(source["file"], f"{label}.file"):
                errors.append(f"{label}.file: expected non-empty string")
            if not is_date(source["recorded_at"]):
                errors.append(f"{label}.recorded_at: expected an ISO date or datetime")
    source_ids = set(sources)

    locations = data["locations"]
    if not isinstance(locations, dict):
        errors.append("catalog.locations: expected object")
        locations = {}
    for location_id, location in locations.items():
        label = f"locations[{location_id!r}]"
        if not require_string(location_id, "location ID"):
            errors.append(f"{label}: location ID must be non-empty string")
        if not exact_keys(location, LOCATION_KEYS, label, errors):
            continue
        path = location["path"]
        if not isinstance(path, list) or not path or any(not require_string(part, f"{label}.path") for part in path):
            errors.append(f"{label}.path: expected a non-empty array of non-empty strings")
        steps = location["steps"]
        if not isinstance(steps, list) or any(not require_string(step, f"{label}.steps") for step in steps):
            errors.append(f"{label}.steps: expected an array of non-empty strings")
        validate_photos(location["photos"], catalog_dir, f"{label}.photos", errors)
    location_ids = set(locations)

    items = data["items"]
    if not isinstance(items, list):
        errors.append("catalog.items: expected array")
        return errors
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not exact_keys(item, ITEM_KEYS, label, errors):
            continue
        item_id = item["id"]
        if not require_string(item_id, f"{label}.id"):
            errors.append(f"{label}.id: expected non-empty string")
        elif item_id in seen_ids:
            errors.append(f"{label}.id: duplicate ID {item_id!r}")
        else:
            seen_ids.add(item_id)
        if not require_string(item["name"], f"{label}.name"):
            errors.append(f"{label}.name: expected non-empty string")
        aliases = item["aliases"]
        if not isinstance(aliases, list) or any(not require_string(alias, f"{label}.aliases") for alias in aliases):
            errors.append(f"{label}.aliases: expected array of non-empty strings")
        if not isinstance(item["location"], str) or item["location"] not in location_ids:
            errors.append(f"{label}.location: location ID is not present in locations")
        if not isinstance(item["detail"], str):
            errors.append(f"{label}.detail: expected string")
        if not is_date(item["recorded_at"]):
            errors.append(f"{label}.recorded_at: expected an ISO date or datetime")
        validate_evidence(item["evidence"], source_ids, catalog_dir, f"{label}.evidence", errors)
        history = item["history"]
        if not isinstance(history, list):
            errors.append(f"{label}.history: expected array")
        else:
            for history_index, snapshot in enumerate(history):
                validate_snapshot(
                    snapshot,
                    source_ids,
                    location_ids,
                    catalog_dir,
                    f"{label}.history[{history_index}]",
                    errors,
                )
    return errors


def resolve_catalog(path_text: str) -> Path:
    raw = Path(path_text).expanduser()
    if raw.is_symlink():
        fail("catalog path may not be a symlink")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        fail(f"cannot open catalog: {exc}")
    if not path.is_file():
        fail("catalog path is not a file")
    return path


def read_json_bytes(path: Path) -> tuple[bytes, Any]:
    try:
        content = path.read_bytes()
        data = json.loads(content.decode("utf-8"))
        return content, data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid UTF-8 JSON from {path}: {exc}")


def load_batch(path_text: str) -> Any:
    path = Path(path_text).expanduser()
    if path.is_symlink():
        fail("batch path may not be a symlink")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read batch JSON: {exc}")


def resolved_location(data: dict[str, Any], location_id: str) -> dict[str, Any]:
    return {"id": location_id, **copy.deepcopy(data["locations"][location_id])}


def emit(payload: Any, exit_code: int = 0) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return exit_code


def check_command(catalog_path: Path) -> int:
    _, data = read_json_bytes(catalog_path)
    errors = validate_catalog(data, catalog_path.parent)
    status = "ok" if not errors else "invalid"
    return emit({"status": status, "catalog": str(catalog_path), "errors": errors}, 0 if not errors else 1)


def list_command(catalog_path: Path, summary: bool = False) -> int:
    _, data = read_json_bytes(catalog_path)
    errors = validate_catalog(data, catalog_path.parent)
    if errors:
        return emit({"status": "error", "errors": errors}, 1)
    if summary:
        entries = [
            {
                "id": item["id"],
                "name": item["name"],
                "aliases": item["aliases"],
                "location": item["location"],
                "path": data["locations"][item["location"]]["path"],
            }
            for item in data["items"]
        ]
    else:
        entries = [
            {"item": copy.deepcopy(item), "location": resolved_location(data, item["location"])}
            for item in data["items"]
        ]
    return emit({"status": "ok", "count": len(entries), "items": entries})


def find_command(catalog_path: Path, query: str, by_id: bool = False) -> int:
    _, data = read_json_bytes(catalog_path)
    errors = validate_catalog(data, catalog_path.parent)
    if errors:
        return emit({"status": "error", "errors": errors}, 1)
    query_norm = normalized(query)
    if not query_norm:
        return emit({"status": "error", "error": "query must not be empty", "matches": []}, 2)

    raw_matches: list[tuple[int, str, str]] = []
    for index, item in enumerate(data["items"]):
        if by_id:
            if item["id"] == query:
                raw_matches.append((index, item["id"], item["id"]))
            continue
        terms = [item["name"], *item["aliases"]]
        seen: set[str] = set()
        for term in terms:
            term_norm = normalized(term)
            if not term_norm or term_norm in seen:
                continue
            seen.add(term_norm)
            if term_norm in query_norm:
                raw_matches.append((index, term, term_norm))

    surviving = []
    for match in raw_matches:
        term_norm = match[2]
        is_less_specific = any(
            len(other[2]) > len(term_norm) and term_norm in other[2]
            for other in raw_matches
        )
        if not is_less_specific:
            surviving.append(match)

    best: dict[int, tuple[int, str, str]] = {}
    for match in surviving:
        index = match[0]
        old = best.get(index)
        rank = (match[2] == query_norm, len(match[2]))
        if old is None or rank > (old[2] == query_norm, len(old[2])):
            best[index] = match

    matches = []
    for index in range(len(data["items"])):
        if index not in best:
            continue
        item = data["items"][index]
        match = best[index]
        matches.append(
            {
                "item": copy.deepcopy(item),
                "location": resolved_location(data, item["location"]),
                "matched_word": match[1],
            }
        )
    status = "not_found" if not matches else ("found" if len(matches) == 1 else "multiple")
    return emit({"status": status, "query": query, "matches": matches})


def exclusive_lock(catalog_path: Path):
    lock_path = catalog_path.with_name(catalog_path.name + ".lock")
    try:
        os.mkdir(lock_path, 0o700)
    except FileExistsError:
        fail("another home-finder mutation is in progress (catalog lock exists)")
    except OSError as exc:
        fail(f"cannot acquire catalog lock: {exc}")

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                os.rmdir(lock_path)
            except OSError:
                pass

    return Lock()


def create_backup_dir(path_text: str) -> tuple[Path, list[Path]]:
    raw = Path(path_text).expanduser()
    if raw.exists() and raw.is_symlink():
        fail("backup directory may not be a symlink")
    target = raw.resolve(strict=False)
    if target.exists():
        if not target.is_dir():
            fail("backup-dir is not a directory")
        return target, []
    missing: list[Path] = []
    cursor = target
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            fail("cannot find an existing parent for backup-dir")
        cursor = parent
    if not cursor.is_dir():
        fail("backup-dir parent is not a directory")
    created: list[Path] = []
    try:
        for directory in reversed(missing):
            directory.mkdir(exist_ok=False)
            created.append(directory)
    except OSError:
        remove_created_dirs(list(reversed(created)))
        raise
    return target, created


def remove_created_dirs(created: list[Path]) -> None:
    for path in created:
        try:
            path.rmdir()
        except OSError:
            pass


def write_mutation(catalog_path: Path, original: bytes, updated: dict[str, Any], backup_dir_text: str) -> Path:
    encoded = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        initial_stat = catalog_path.stat()
    except OSError as exc:
        fail(f"cannot stat catalog before write: {exc}")

    backup_dir: Path | None = None
    created_dirs: list[Path] = []
    backup_path: Path | None = None
    backup_created = False
    temp_path: Path | None = None
    committed = False
    try:
        # Build the replacement in the catalog directory to keep os.replace atomic.
        fd, temp_name = tempfile.mkstemp(prefix=f".{catalog_path.name}.", suffix=".tmp", dir=catalog_path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), stat.S_IMODE(initial_stat.st_mode))

        backup_dir, created_dirs = create_backup_dir(backup_dir_text)
        digest = hashlib.sha256(original).hexdigest()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        base_name = f"{catalog_path.name}.{stamp}.{digest[:12]}.bak"
        backup_path = backup_dir / base_name
        suffix = 1
        while backup_path.exists():
            backup_path = backup_dir / f"{base_name}.{suffix}"
            suffix += 1
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        backup_fd = os.open(backup_path, flags, 0o600)
        backup_created = True
        with os.fdopen(backup_fd, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())

        # Detect edits made outside this CLI while the operation was prepared.
        current_stat = catalog_path.stat()
        if (current_stat.st_dev, current_stat.st_ino) != (initial_stat.st_dev, initial_stat.st_ino):
            fail("catalog changed while mutation was prepared; no replacement performed")
        if catalog_path.read_bytes() != original:
            fail("catalog bytes changed while mutation was prepared; no replacement performed")

        os.replace(temp_path, catalog_path)
        temp_path = None
        committed = True
        try:
            dir_fd = os.open(catalog_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # The file replacement has succeeded; directory fsync is best effort.
            pass
        return backup_path
    except Exception:
        if not committed and backup_created and backup_path is not None:
            try:
                backup_path.unlink()
            except OSError:
                pass
        remove_created_dirs(created_dirs)
        raise
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def merge_batch(current: dict[str, Any], batch: Any) -> dict[str, Any]:
    errors = validate_batch_shape(batch)
    if errors:
        fail("invalid batch: " + "; ".join(errors))
    merged = copy.deepcopy(current)
    for section in ("sources", "locations"):
        for entry_id, entry in batch[section].items():
            if entry_id in merged[section]:
                if merged[section][entry_id] != entry:
                    fail(f"batch {section[:-1]} {entry_id!r} conflicts with existing object")
            else:
                merged[section][entry_id] = copy.deepcopy(entry)
    existing_ids = {item["id"] for item in merged["items"] if isinstance(item, dict) and isinstance(item.get("id"), str)}
    batch_ids: set[str] = set()
    for item in batch["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            fail("invalid batch: each item must have a string id")
        item_id = item["id"]
        if item_id in existing_ids or item_id in batch_ids:
            fail(f"batch item ID already exists or is duplicated: {item_id!r}")
        batch_ids.add(item_id)
        merged["items"].append(copy.deepcopy(item))
    return merged


def validate_batch_shape(batch: Any) -> list[str]:
    errors: list[str] = []
    if not exact_keys(batch, TOP_KEYS, "batch", errors):
        return errors
    if type(batch["schema_version"]) is not int or batch["schema_version"] != 1:
        errors.append("batch.schema_version: expected 1")
    for section in ("sources", "locations"):
        if not isinstance(batch[section], dict):
            errors.append(f"batch.{section}: expected object")
    if not isinstance(batch["items"], list):
        errors.append("batch.items: expected array")
    return errors


def add_command(catalog_path: Path, batch_path: str, backup_dir: str) -> int:
    with exclusive_lock(catalog_path):
        original, current = read_json_bytes(catalog_path)
        current_errors = validate_catalog(current, catalog_path.parent)
        if current_errors:
            return emit({"status": "error", "error": "catalog is invalid", "errors": current_errors}, 1)
        batch = load_batch(batch_path)
        merged = merge_batch(current, batch)
        errors = validate_catalog(merged, catalog_path.parent)
        if errors:
            return emit({"status": "error", "error": "merged catalog is invalid", "errors": errors}, 1)
        backup = write_mutation(catalog_path, original, merged, backup_dir)
        return emit({"status": "added", "added": len(batch["items"]), "total": len(merged["items"]), "backup": str(backup)})


def strict_move_date(value: str) -> bool:
    if not DATE_RE.fullmatch(value):
        return False
    try:
        dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def move_command(
    catalog_path: Path,
    item_id: str,
    from_location: str,
    to_location: str,
    at: str,
    detail: str,
    backup_dir: str,
    new_path: str | None,
    steps: list[str] | None,
    note: str,
) -> int:
    if not strict_move_date(at):
        return emit({"status": "error", "error": "--at must be a valid YYYY-MM-DD date"}, 2)
    if not item_id.strip() or not from_location.strip() or not to_location.strip():
        return emit({"status": "error", "error": "--id, --from, and --to must be non-empty"}, 2)
    steps = steps or []

    with exclusive_lock(catalog_path):
        original, data = read_json_bytes(catalog_path)
        errors = validate_catalog(data, catalog_path.parent)
        if errors:
            return emit({"status": "error", "error": "catalog is invalid", "errors": errors}, 1)
        matches = [item for item in data["items"] if item["id"] == item_id]
        if not matches:
            return emit({"status": "error", "error": f"item ID not found: {item_id}"}, 1)
        item = matches[0]
        if item["location"] == to_location and item["detail"] == detail:
            return emit(
                {
                    "status": "no_change",
                    "id": item_id,
                    "location": resolved_location(data, to_location),
                    "item": copy.deepcopy(item),
                }
            )
        if item["location"] != from_location:
            return emit(
                {
                    "status": "error",
                    "error": "item location does not match --from",
                    "expected_from": item["location"],
                    "provided_from": from_location,
                },
                1,
            )

        if to_location in data["locations"]:
            if new_path is not None or steps:
                return emit({"status": "error", "error": "--new-path/--step are only allowed for a new target location"}, 2)
        else:
            if new_path is None or not steps:
                return emit(
                    {"status": "error", "error": "unknown --to location requires --new-path and at least one --step"},
                    2,
                )
            path_parts = [part.strip() for part in new_path.split("/")]
            if not path_parts or any(not part or part in {".", ".."} or "\\" in part for part in path_parts):
                return emit({"status": "error", "error": "--new-path must be slash-separated non-empty location parts"}, 2)
            if any(not step.strip() for step in steps):
                return emit({"status": "error", "error": "each --step must be non-empty"}, 2)
            data["locations"][to_location] = {"path": path_parts, "steps": steps, "photos": []}

        snapshot = {key: copy.deepcopy(item[key]) for key in SNAPSHOT_KEYS}
        item["history"].append(snapshot)
        item["location"] = to_location
        item["detail"] = detail
        item["recorded_at"] = at
        item["evidence"] = {
            "kind": "user_report",
            "source": None,
            "at": at,
            "photos": [],
            "note": note,
        }
        errors = validate_catalog(data, catalog_path.parent)
        if errors:
            return emit({"status": "error", "error": "moved catalog is invalid", "errors": errors}, 1)
        backup = write_mutation(catalog_path, original, data, backup_dir)
        return emit(
            {
                "status": "moved",
                "id": item_id,
                "from": from_location,
                "to": to_location,
                "backup": str(backup),
                "item": copy.deepcopy(item),
                "location": resolved_location(data, to_location),
            }
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find household items in a local JSON catalog")
    parser.add_argument("--catalog", required=True, help="catalog JSON file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate catalog structure and photo references")
    list_parser = commands.add_parser("list", help="list all items with resolved locations")
    list_parser.add_argument("--summary", action="store_true", help="show names, aliases and paths only")
    find_parser = commands.add_parser("find", help="find by name or alias substring")
    find_parser.add_argument("query")
    find_parser.add_argument("--by-id", action="store_true", help="retrieve one exact item ID")
    add_parser = commands.add_parser("add", help="merge a batch without overwriting existing entries")
    add_parser.add_argument("--batch", required=True)
    add_parser.add_argument("--backup-dir", required=True)
    move_parser = commands.add_parser("move", help="record an item move and preserve its prior state")
    move_parser.add_argument("--id", required=True)
    move_parser.add_argument("--from", dest="from_location", required=True)
    move_parser.add_argument("--to", dest="to_location", required=True)
    move_parser.add_argument("--at", required=True)
    move_parser.add_argument("--detail", required=True)
    move_parser.add_argument("--backup-dir", required=True)
    move_parser.add_argument("--new-path")
    move_parser.add_argument("--step", action="append", default=[])
    move_parser.add_argument("--note", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        catalog_path = resolve_catalog(args.catalog)
        if args.command == "check":
            return check_command(catalog_path)
        if args.command == "list":
            return list_command(catalog_path, args.summary)
        if args.command == "find":
            return find_command(catalog_path, args.query, args.by_id)
        if args.command == "add":
            return add_command(catalog_path, args.batch, args.backup_dir)
        if args.command == "move":
            return move_command(
                catalog_path,
                args.id,
                args.from_location,
                args.to_location,
                args.at,
                args.detail,
                args.backup_dir,
                args.new_path,
                args.step,
                args.note,
            )
        parser.error("unknown command")
    except CliError as exc:
        return emit({"status": "error", "error": str(exc)}, 2)
    except OSError as exc:
        return emit({"status": "error", "error": f"filesystem operation failed: {exc}"}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
