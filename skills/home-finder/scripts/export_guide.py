#!/usr/bin/env python3
"""Render a Home Finder HTML guide from a catalog and a local template."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any


class ExportError(Exception):
    """An input or output could not be safely exported."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExportError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ExportError(f"invalid JSON constant: {value}")


def load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read JSON from {path}: {exc}") from exc


def _require_string(value: Any, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ExportError(f"{label} must be a{' non-empty' if not allow_empty else ''} string")
    if "\x00" in value:
        raise ExportError(f"{label} contains a NUL character")
    return value


def _source_basename(value: Any, label: str) -> str:
    """Return only the basename of a catalog source path, never its full path."""
    text = _require_string(value, label)
    if not text:
        return ""
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    if name in ("", ".", "..") or any(ord(c) < 32 for c in name):
        raise ExportError(f"{label} has an unsafe basename")
    return name


def _legacy_source_basename(value: Any, label: str) -> str:
    text = _require_string(value, label)
    if not text:
        return ""
    if "/" in text or "\\" in text or Path(text).is_absolute() or text in (".", ".."):
        raise ExportError(f"{label} must be a basename, not a path")
    if any(ord(c) < 32 for c in text):
        raise ExportError(f"{label} contains a control character")
    return text


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ExportError(f"{label} must be an array")
    return [_require_string(item, f"{label}[{i}]", allow_empty=False) for i, item in enumerate(value)]


def _photo_array(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ExportError(f"{label} must be an array")
    result: list[dict[str, str]] = []
    for i, photo in enumerate(value):
        if not isinstance(photo, dict):
            raise ExportError(f"{label}[{i}] must be an object")
        result.append(
            {
                "file": _require_string(photo.get("file"), f"{label}[{i}].file", allow_empty=False),
                "at": _require_string(photo.get("at"), f"{label}[{i}].at"),
                "label": _require_string(photo.get("label"), f"{label}[{i}].label"),
            }
        )
    return result


def _safe_photo_path(catalog_dir: Path, relative: str) -> tuple[str, bytes]:
    if "\\" in relative:
        raise ExportError(f"photo path must use safe relative POSIX syntax: {relative!r}")
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or (pure.parts and ":" in pure.parts[0])
    ):
        raise ExportError(f"unsafe photo path: {relative!r}")
    if pure.suffix.lower() not in (".jpg", ".jpeg"):
        raise ExportError(f"photo path is not a JPEG: {relative!r}")

    root = catalog_dir.resolve(strict=True)
    path = catalog_dir.joinpath(*pure.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ExportError(f"photo is not a regular file: {relative!r}")
        data = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        raise ExportError(f"unsafe or missing photo {relative!r}: {exc}") from exc
    if len(data) < 3 or data[:3] != b"\xff\xd8\xff":
        raise ExportError(f"photo is not JPEG data: {relative!r}")
    # Retain the catalog's canonical relative spelling in the exported map.
    return relative, data


class _GuideDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.matches: list[str] = []
        self._collecting = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr = dict(attrs)
        if attr.get("id") != "guide-data":
            return
        if self._collecting:
            raise ExportError("nested guide-data script tags are malformed")
        if attr.get("type", "").lower() != "application/json":
            raise ExportError("guide-data script must have type application/json")
        self._collecting = True
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._collecting:
            self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._collecting:
            self._chunks.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._collecting:
            self.matches.append("".join(self._chunks))
            self._collecting = False
            self._chunks = []


def load_legacy_routes(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ExportError(f"cannot read previous guide {path}: {exc}") from exc
    parser = _GuideDataParser()
    try:
        parser.feed(text)
        parser.close()
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"malformed previous guide HTML: {exc}") from exc
    if parser._collecting or len(parser.matches) != 1:
        raise ExportError("previous guide must contain exactly one complete guide-data JSON script")
    try:
        value = json.loads(
            parser.matches[0],
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ExportError) as exc:
        raise ExportError(f"previous guide-data is malformed JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ExportError("previous guide-data must be a JSON array")
    for index, route in enumerate(value):
        _validate_legacy_route_shape(route, index)
    return value


def _validate_legacy_route_shape(route: Any, index: int) -> None:
    label = f"previous guide-data[{index}]"
    if not isinstance(route, dict):
        raise ExportError(f"{label} must be an object")
    for key in ("id", "date", "proof", "source"):
        _require_string(route.get(key), f"{label}.{key}", allow_empty=False)
    _legacy_source_basename(route["source"], f"{label}.source")
    _string_list(route.get("path"), f"{label}.path")
    steps = route.get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        raise ExportError(f"{label}.steps must contain at least two steps")
    for step_index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ExportError(f"{label}.steps[{step_index}] must be an object")
        _require_string(step.get("at"), f"{label}.steps[{step_index}].at", allow_empty=False)
        _decode_legacy_image(step.get("src"), f"{label}.steps[{step_index}].src")
        step_label = f"{label}.steps[{step_index}]"
        if "point" in step and step["point"] is not None:
            _validate_coordinate_pair(step["point"], f"{step_label}.point")
        if "marker" in step and step["marker"] is not None:
            marker = step["marker"]
            if not isinstance(marker, dict):
                raise ExportError(f"{step_label}.marker must be an object")
            _validate_coordinate_pair(marker.get("button"), f"{step_label}.marker.button")
            if marker.get("target") is not None:
                _validate_coordinate_pair(marker["target"], f"{step_label}.marker.target")


def _validate_coordinate_pair(value: Any, label: str) -> None:
    if not isinstance(value, list) or len(value) != 2:
        raise ExportError(f"{label} must contain two percentage coordinates")
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not 0 <= coordinate <= 100:
            raise ExportError(f"{label} coordinates must be numbers from 0 to 100")


def _decode_legacy_image(value: Any, label: str) -> bytes:
    text = _require_string(value, label, allow_empty=False)
    prefix = "data:image/jpeg;base64,"
    if not text.startswith(prefix):
        raise ExportError(f"{label} must be an inline JPEG data URI")
    try:
        image = base64.b64decode(text[len(prefix) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExportError(f"{label} has invalid base64: {exc}") from exc
    if len(image) < 3 or image[:3] != b"\xff\xd8\xff":
        raise ExportError(f"{label} is not JPEG data")
    return image


def _proof_kind(value: str) -> str | None:
    return {
        "实拍": "visual",
        "实拍与旁白": "visual+spoken",
        "旁白": "spoken",
        "visual": "visual",
        "visual+spoken": "visual+spoken",
        "spoken": "spoken",
    }.get(value)


def _escape_script_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # JSON is placed inside a raw-text <script>; prevent HTML parsing or line-separator hazards.
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_projection(catalog_path: Path, room: str | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    catalog = load_json(catalog_path)
    if not isinstance(catalog, dict):
        raise ExportError("catalog root must be an object")
    items = catalog.get("items")
    locations = catalog.get("locations")
    sources = catalog.get("sources", {})
    if not isinstance(items, list) or not isinstance(locations, dict) or not isinstance(sources, dict):
        raise ExportError("catalog must contain items[], locations{}, and sources{}")

    ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ExportError(f"catalog items[{index}] must be an object")
        item_id = _require_string(item.get("id"), f"catalog items[{index}].id", allow_empty=False)
        if item_id in ids:
            raise ExportError(f"duplicate catalog item id: {item_id}")
        ids.add(item_id)

    projected: list[dict[str, Any]] = []
    projected_locations: dict[str, Any] = {}
    images: dict[str, str] = {}
    photo_bytes: dict[str, bytes] = {}

    def register_photos(records: list[dict[str, str]]) -> list[dict[str, str]]:
        for photo in records:
            if photo["file"] in photo_bytes:
                continue
            rel, data = _safe_photo_path(catalog_path.parent, photo["file"])
            photo_bytes[rel] = data
            images[rel] = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        return records

    for location_id, location in locations.items():
        if not isinstance(location, dict):
            raise ExportError(f"location {location_id} must be an object")
        path = _string_list(location.get("path"), f"location {location_id}.path")
        if not path:
            raise ExportError(f"location {location_id} must have a non-empty path")
        if room is not None and path[0] != room:
            continue
        projected_locations[location_id] = {
            "path": path,
            "steps": _string_list(location.get("steps", []), f"location {location_id}.steps"),
            "photos": register_photos(_photo_array(location.get("photos", []), f"location {location_id}.photos")),
        }

    for item in items:
        item_id = item["id"]
        location_id = _require_string(item.get("location"), f"item {item_id}.location", allow_empty=False)
        if location_id not in locations:
            raise ExportError(f"item {item_id} refers to missing location {location_id!r}")
        location = projected_locations.get(location_id)
        if location is None:
            continue
        evidence = item.get("evidence", {})
        if not isinstance(evidence, dict):
            raise ExportError(f"item {item_id}.evidence must be an object")
        item_photos = register_photos(_photo_array(evidence.get("photos", []), f"item {item_id}.evidence.photos"))

        source_ref = evidence.get("source", "")
        source_name = ""
        if source_ref:
            source_ref = _require_string(source_ref, f"item {item_id}.evidence.source", allow_empty=False)
            source_record = sources.get(source_ref)
            if not isinstance(source_record, dict):
                raise ExportError(f"item {item_id} refers to missing source {source_ref!r}")
            source_name = _source_basename(source_record.get("file", ""), f"source {source_ref}.file")

        aliases = item.get("aliases", [])
        aliases = _string_list(aliases, f"item {item_id}.aliases")
        projected.append(
            {
                "id": item_id,
                "location": location_id,
                "name": _require_string(item.get("name"), f"item {item_id}.name", allow_empty=False),
                "aliases": aliases,
                "path": location["path"],
                "steps": location["steps"],
                "detail": _require_string(item.get("detail", ""), f"item {item_id}.detail"),
                "recorded_at": _require_string(item.get("recorded_at", ""), f"item {item_id}.recorded_at"),
                "source": source_name,
                "kind": _require_string(evidence.get("kind", ""), f"item {item_id}.evidence.kind"),
                "at": _require_string(evidence.get("at", ""), f"item {item_id}.evidence.at"),
                "note": _require_string(evidence.get("note", ""), f"item {item_id}.evidence.note"),
                "locationPhotos": location["photos"],
                "itemPhotos": item_photos,
            }
        )

    return {"items": projected, "locations": projected_locations, "images": images}, photo_bytes


def applicable_routes(
    legacy_routes: list[dict[str, Any]], catalog_projection: dict[str, Any], photo_bytes: dict[str, bytes]
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in catalog_projection["items"]}
    result: list[dict[str, Any]] = []
    for index, route in enumerate(legacy_routes):
        item = by_id.get(route["id"])
        if item is None or route["path"] != item["path"] or route["date"] != item["recorded_at"]:
            continue
        if route["source"] != item["source"]:
            continue
        if _proof_kind(route["proof"]) != item["kind"]:
            continue

        first_step, last_step = route["steps"][0], route["steps"][-1]
        first_bytes = _decode_legacy_image(first_step["src"], f"previous guide-data[{index}].steps[0].src")
        last_bytes = _decode_legacy_image(last_step["src"], f"previous guide-data[{index}].steps[-1].src")
        # Route endpoint photos must bind by both timestamp and decoded byte equality.
        # The full catalog projection has already resolved and validated every selected JPEG.
        def matches(photo_records: list[dict[str, str]], at: str, image: bytes) -> bool:
            matched = [photo_bytes[photo["file"]] for photo in photo_records if photo["at"] == at and photo["file"] in photo_bytes]
            return len(matched) == 1 and matched[0] == image

        if matches(item["locationPhotos"], first_step["at"], first_bytes) and matches(
            item["itemPhotos"], last_step["at"], last_bytes
        ):
            result.append(route)
    return result


def atomic_write(path: Path, data: str) -> None:
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, existing_mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ExportError(f"cannot atomically write {path}: {exc}") from exc


def export(args: argparse.Namespace) -> int:
    catalog_path = Path(args.catalog)
    template_path = Path(args.template)
    output_path = Path(args.out)
    try:
        template = template_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ExportError(f"cannot read template {template_path}: {exc}") from exc
    for token in ("__GUIDE_DATA__", "__CATALOG_DATA__"):
        if template.count(token) != 1:
            raise ExportError(f"template must contain exactly one {token} placeholder")

    # Validate the selected catalog and resolve its photos before reading any
    # optional previous guide, which may contain coordinate-bearing recipes.
    projection, photo_bytes = build_projection(catalog_path, args.room)
    routes = load_legacy_routes(Path(args.previous)) if args.previous else []
    guide_routes = applicable_routes(routes, projection, photo_bytes) if routes else []
    replacements = {
        "__GUIDE_DATA__": _escape_script_json(guide_routes),
        "__CATALOG_DATA__": _escape_script_json(projection),
    }
    pattern = re.compile(r"__GUIDE_DATA__|__CATALOG_DATA__")
    output = pattern.sub(lambda match: replacements[match.group(0)], template)
    atomic_write(output_path, output)
    print(f"Exported {len(projection['items'])} items and {len(guide_routes)} verified guide routes to {output_path}")
    return len(projection["items"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, help="catalog JSON file")
    parser.add_argument("--template", required=True, help="HTML template with the two data placeholders")
    parser.add_argument("--out", required=True, help="HTML output path")
    parser.add_argument("--room", help="include only items whose first location path segment matches this name")
    parser.add_argument("--previous", help="optional previous guide HTML containing guide-data JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        export(parse_args(argv))
        return 0
    except ExportError as exc:
        print(f"export_guide.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
