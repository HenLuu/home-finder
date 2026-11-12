"""Synthetic regression test for A10 move, re-export, and photo-route behavior.

Run with HOME_FINDER_LAB_DIR pointing at a disposable Lab Test directory. The
fixture contains only generated records and a tiny embedded JPEG, never a real
household catalog, room name, source video, or photo.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = Path(os.environ.get("HOME_FINDER_SKILL_ROOT", str(REPO_ROOT / "skills" / "home-finder"))).expanduser().resolve()
CATALOG_CLI = SKILL_ROOT / "scripts" / "home_finder.py"
EXPORT_CLI = SKILL_ROOT / "scripts" / "export_guide.py"
TEMPLATE = SKILL_ROOT / "assets" / "guide.template.html"
MOVE_ID = "fixture-move-001"
PHOTO_ID = "fixture-photo-001"
MOVE_ALIAS = "合成收纳包"

# A valid 8x8 JPEG generated from a solid synthetic color; no EXIF or user data.
SYNTHETIC_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAABQf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdACKg/9k="
)


class DataScriptParser(HTMLParser):
    def __init__(self, target_id: str) -> None:
        super().__init__(convert_charrefs=False)
        self.target_id = target_id
        self.matches: list[str] = []
        self.collecting = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr = dict(attrs)
        if attr.get("id") == self.target_id:
            if self.collecting:
                raise AssertionError(f"duplicate {self.target_id} script")
            self.collecting = True
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.collecting:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.collecting:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.collecting:
            self.parts.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.collecting:
            self.matches.append("".join(self.parts))
            self.collecting = False
            self.parts = []


def embedded_json(page: str, script_id: str) -> Any:
    parser = DataScriptParser(script_id)
    parser.feed(page)
    parser.close()
    if parser.collecting or len(parser.matches) != 1:
        raise AssertionError(f"expected one complete #{script_id} script")
    return json.loads(parser.matches[0])


def replace_script_json(page: str, script_id: str, value: Any) -> str:
    pattern = re.compile(
        r'(<script\b[^>]*\bid="' + re.escape(script_id) + r'"[^>]*>).*?(</script>)',
        re.DOTALL,
    )
    raw_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    updated, replacements = pattern.subn(lambda match: match.group(1) + raw_json + match.group(2), page)
    if replacements != 1:
        raise AssertionError(f"expected one #{script_id} script to replace")
    return updated


def run_guide_for(template: Path, item: dict[str, Any], recipe: dict[str, Any]) -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node.js is required to exercise the template's actual guideFor function")
    script = f"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync({json.dumps(str(template))}, "utf8");
const signature = "function guideFor(item) {{";
const start = source.indexOf(signature);
if (start < 0) throw new Error("guideFor function not found in template");
const open = source.indexOf("{{", start + signature.length - 1);
let depth = 0;
let end = -1;
for (let i = open; i < source.length; i++) {{
  if (source[i] === "{{") depth++;
  else if (source[i] === "}}" && --depth === 0) {{ end = i + 1; break; }}
}}
if (end < 0) throw new Error("guideFor function has unbalanced braces");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const context = {{
  input,
  legacy: new Map([[input.recipe.id, input.recipe]]),
  wholeHome: true,
  displayName: candidate => candidate.name,
}};
vm.createContext(context);
vm.runInContext(source.slice(start, end) + "\\nthis.output = guideFor(this.input.item);", context);
process.stdout.write(JSON.stringify(context.output));
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps({"item": item, "recipe": recipe}, ensure_ascii=False),
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"guideFor harness failed: {result.stderr}")
    return json.loads(result.stdout)


def item_record(item_id: str, name: str, aliases: list[str], location: str, photo: str,
                recorded_at: str, at: str, note: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "aliases": aliases,
        "location": location,
        "detail": "合成测试物品",
        "recorded_at": recorded_at,
        "evidence": {
            "kind": "visual",
            "source": "fixture-source",
            "at": at,
            "photos": [{"file": photo, "at": at, "label": "合成物品实拍"}],
            "note": note,
        },
        "history": [],
    }


def location_record(path: list[str], steps: list[str], photo: str, at: str) -> dict[str, Any]:
    return {
        "path": path,
        "steps": steps,
        "photos": [{"file": photo, "at": at, "label": "合成位置参照"}],
    }


def route_for_projection(item: dict[str, Any], location_path: list[str],
                         location_at: str, item_at: str) -> dict[str, Any]:
    data_uri = "data:image/jpeg;base64," + base64.b64encode(SYNTHETIC_JPEG).decode("ascii")
    return {
        "id": item["id"],
        "date": item["recorded_at"],
        "proof": "实拍",
        "source": "synthetic-source.mp4",
        "path": location_path,
        "steps": [
            {"at": location_at, "src": data_uri, "point": [50, 50],
             "marker": {"button": [70, 70], "target": [60, 60]}},
            {"at": item_at, "src": data_uri},
        ],
    }


class A10MoveReexportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        lab_value = os.environ.get("HOME_FINDER_LAB_DIR")
        if not lab_value:
            raise RuntimeError("set HOME_FINDER_LAB_DIR to the assigned Lab Test directory")
        lab_root = Path(lab_value).expanduser().resolve()
        lab_root.mkdir(parents=True, exist_ok=True)
        cls.run_dir = Path(tempfile.mkdtemp(prefix="a10-move-reexport-", dir=lab_root))
        (cls.run_dir / "synthetic-photos").mkdir()
        cls.catalog_path = cls.run_dir / "catalog.json"
        cls.old_guide_path = cls.run_dir / "before-move.html"
        cls.current_guide_path = cls.run_dir / "after-move.html"
        cls.backup_dir = cls.run_dir / "catalog-backups"
        cls.backup_dir.mkdir()
        (cls.run_dir / "synthetic-photos" / "old-location.jpg").write_bytes(SYNTHETIC_JPEG)
        (cls.run_dir / "synthetic-photos" / "moving-item.jpg").write_bytes(SYNTHETIC_JPEG)
        (cls.run_dir / "synthetic-photos" / "stable-location.jpg").write_bytes(SYNTHETIC_JPEG)
        (cls.run_dir / "synthetic-photos" / "stable-item.jpg").write_bytes(SYNTHETIC_JPEG)

        old_path = ["测试房间", "合成木柜", "上层"]
        stable_path = ["保留房间", "合成柜体", "合成层"]
        cls.old_location = location_record(
            old_path, ["进入合成房间", "打开合成木柜", "查看上层"],
            "synthetic-photos/old-location.jpg", "00:01.0",
        )
        cls.stable_location = location_record(
            stable_path, ["进入保留房间", "打开合成柜体", "查看合成层"],
            "synthetic-photos/stable-location.jpg", "00:03.0",
        )
        cls.move_item = item_record(
            MOVE_ID, "合成物品", [MOVE_ALIAS], "location-old",
            "synthetic-photos/moving-item.jpg", "2026-11-12", "00:02.0", "仅供A10隔离测试",
        )
        cls.stable_item = item_record(
            PHOTO_ID, "保留照片物品", ["合成照片路线"], "location-stable",
            "synthetic-photos/stable-item.jpg", "2026-11-12", "00:04.0", "仅供A10隔离测试",
        )
        catalog = {
            "schema_version": 1,
            "sources": {
                "fixture-source": {"file": "synthetic-source.mp4", "recorded_at": "2026-11-12"}
            },
            "locations": {
                "location-old": cls.old_location,
                "location-stable": cls.stable_location,
            },
            "items": [cls.move_item, cls.stable_item],
        }
        cls.initial_catalog_data = copy.deepcopy(catalog)
        cls.catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        cls.run_cli(["check"])
        cls.run_export(cls.old_guide_path)
        old_page = cls.old_guide_path.read_text(encoding="utf-8")
        previous_routes = [
            route_for_projection(cls.move_item, old_path, "00:01.0", "00:02.0"),
            route_for_projection(cls.stable_item, stable_path, "00:03.0", "00:04.0"),
        ]
        old_page = replace_script_json(old_page, "guide-data", previous_routes)
        cls.old_guide_path.write_text(old_page, encoding="utf-8")

    @classmethod
    def run_process(cls, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(arguments, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(
                f"command failed ({result.returncode}): {' '.join(arguments)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    @classmethod
    def run_cli(cls, args: list[str]) -> dict[str, Any]:
        result = cls.run_process([sys.executable, str(CATALOG_CLI), "--catalog", str(cls.catalog_path), *args])
        return json.loads(result.stdout)

    @classmethod
    def run_export(cls, output_path: Path) -> subprocess.CompletedProcess[str]:
        return cls.run_process([
            sys.executable, str(EXPORT_CLI),
            "--catalog", str(cls.catalog_path),
            "--template", str(TEMPLATE),
            "--out", str(output_path),
        ])

    def test_coordinate_bounds_are_enforced_for_previous_routes(self) -> None:
        valid_page = self.old_guide_path.read_text(encoding="utf-8")
        valid_routes = embedded_json(valid_page, "guide-data")
        cases = [
            ("bool", [True, 50], "coordinates must be numbers from 0 to 100"),
            ("out-of-range", [101, 50], "coordinates must be numbers from 0 to 100"),
            ("nan", [float("nan"), 50], "invalid JSON constant: NaN"),
        ]
        rejected = []
        for name, point, expected_error in cases:
            routes = copy.deepcopy(valid_routes)
            routes[0]["steps"][0]["point"] = point
            malformed_previous = self.run_dir / f"previous-{name}.html"
            malformed_previous.write_text(
                replace_script_json(valid_page, "guide-data", routes), encoding="utf-8"
            )
            output_path = self.run_dir / f"should-not-export-{name}.html"
            result = subprocess.run(
                [
                    sys.executable, str(EXPORT_CLI),
                    "--catalog", str(self.catalog_path),
                    "--template", str(TEMPLATE),
                    "--previous", str(malformed_previous),
                    "--out", str(output_path),
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(expected_error, result.stderr)
            self.assertFalse(output_path.exists())
            rejected.append(name)
        self.__class__.coordinate_validation_cases = rejected

    def test_move_keeps_id_updates_search_route_and_drops_stale_photo_route(self) -> None:
        moved = self.run_cli([
            "move", "--id", MOVE_ID, "--from", "location-old", "--to", "location-new",
            "--at", "2026-11-13", "--detail", "透明盒内，右侧",
            "--new-path", "新测试房间/白色桌旁/透明收纳盒",
            "--step", "进入新测试房间", "--step", "找到白色桌旁的透明收纳盒",
            "--backup-dir", str(self.backup_dir), "--note", "合成口述搬动",
        ])
        self.assertEqual(moved["status"], "moved")
        self.assertEqual(moved["id"], MOVE_ID)
        self.assertEqual(moved["from"], "location-old")
        self.assertEqual(moved["to"], "location-new")
        self.assertTrue(Path(moved["backup"]).is_file())

        checked = self.run_cli(["check"])
        self.assertEqual(checked["status"], "ok")

        found = self.run_cli(["find", MOVE_ALIAS])
        self.assertEqual(found["status"], "found")
        self.assertEqual([entry["item"]["id"] for entry in found["matches"]], [MOVE_ID])
        self.assertEqual(found["matches"][0]["location"]["path"], ["新测试房间", "白色桌旁", "透明收纳盒"])
        by_id = self.run_cli(["find", MOVE_ID, "--by-id"])
        self.assertEqual(by_id["matches"][0]["item"]["id"], MOVE_ID)
        self.assertEqual(by_id["matches"][0]["location"]["id"], "location-new")

        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        moved_record = next(item for item in catalog["items"] if item["id"] == MOVE_ID)
        self.assertEqual(moved_record["id"], self.move_item["id"])
        self.assertEqual(moved_record["location"], "location-new")
        self.assertEqual(moved_record["history"][0]["location"], "location-old")
        self.assertEqual(moved_record["history"][0]["evidence"]["photos"][0]["file"], "synthetic-photos/moving-item.jpg")
        self.assertEqual(moved_record["evidence"]["kind"], "user_report")
        self.assertEqual(moved_record["evidence"]["photos"], [])

        export_result = self.run_process([
            sys.executable, str(EXPORT_CLI),
            "--catalog", str(self.catalog_path),
            "--template", str(TEMPLATE),
            "--previous", str(self.old_guide_path),
            "--out", str(self.current_guide_path),
        ])
        self.assertIn("Exported 2 items and 1 verified guide routes", export_result.stdout)
        page = self.current_guide_path.read_text(encoding="utf-8")
        projection = embedded_json(page, "catalog-data")
        routes = embedded_json(page, "guide-data")
        current_items = {item["id"]: item for item in projection["items"]}

        moved_export = current_items[MOVE_ID]
        self.assertEqual(moved_export["path"], ["新测试房间", "白色桌旁", "透明收纳盒"])
        self.assertNotIn("测试房间", moved_export["path"])
        self.assertEqual(moved_export["kind"], "user_report")
        self.assertEqual(moved_export["locationPhotos"], [])
        self.assertEqual(moved_export["itemPhotos"], [])
        self.assertNotIn(MOVE_ID, {route["id"] for route in routes})

        stable_export = current_items[PHOTO_ID]
        self.assertEqual(stable_export["path"], ["保留房间", "合成柜体", "合成层"])
        self.assertEqual(len(stable_export["locationPhotos"]), 1)
        self.assertEqual(len(stable_export["itemPhotos"]), 1)
        stable_route = next(route for route in routes if route["id"] == PHOTO_ID)
        self.assertEqual(stable_route["path"], stable_export["path"])
        self.assertTrue(all(step["src"].startswith("data:image/jpeg;base64,") for step in stable_route["steps"]))
        self.assertTrue(projection["images"])
        for key in ("label", "title", "instruction", "action", "evidenceLabel", "alt"):
            self.assertNotIn(key, stable_route["steps"][0], f"fixture must remain minimal: {key}")
        normalized_route = run_guide_for(TEMPLATE, stable_export, stable_route)
        self.assertEqual(normalized_route["target"], "保留房间 › 合成柜体 › 合成层")
        self.assertEqual(len(normalized_route["steps"]), 2)
        for step in normalized_route["steps"]:
            for key in ("label", "title", "instruction", "action", "evidenceLabel", "alt"):
                self.assertIsInstance(step.get(key), str)
                self.assertTrue(step[key].strip(), f"guideFor left {key} empty")
        self.assertEqual(normalized_route["steps"][0]["action"], "下一步")
        self.assertEqual(normalized_route["steps"][1]["action"], "查看证据")

        manual_recipe = copy.deepcopy(stable_route)
        manual_recipe["target"] = "人工填写的收纳路径"
        manual_copy = {
            "label": "人工步骤名称",
            "title": "人工步骤标题",
            "instruction": "沿人工说明查看照片。",
            "action": "人工按钮文字",
            "evidenceLabel": "人工证据标签",
            "alt": "人工填写的照片说明",
        }
        manual_recipe["steps"][0].update(manual_copy)
        preserved_route = run_guide_for(TEMPLATE, stable_export, manual_recipe)
        self.assertEqual(preserved_route["target"], manual_recipe["target"])
        for key, value in manual_copy.items():
            self.assertEqual(preserved_route["steps"][0][key], value)

        # The page indexes current path text and the stable ID remains the route fragment.
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("path:normalized(item.path.join(' '))", template)
        self.assertIn("entry.path.includes(term)", template)
        self.assertIn("const routeLink = id => '#' + encodeURIComponent(id);", template)
        self.assertIn("browse.renderIndex(roomIndex);", template)
        self.assertIn("新测试房间", json.dumps(projection, ensure_ascii=False))

        manifest = {
            "scope": "synthetic A10 only",
            "real_home_archive_or_photos_read": False,
            "tested_skill_root": str(SKILL_ROOT),
            "template_sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
            "exporter_sha256": hashlib.sha256(EXPORT_CLI.read_bytes()).hexdigest(),
            "catalog": str(self.catalog_path),
            "before_move_html": str(self.old_guide_path),
            "after_move_html": str(self.current_guide_path),
            "backup_dir": str(self.backup_dir),
            "item_id_preserved": MOVE_ID,
            "stale_route_removed": MOVE_ID not in {route["id"] for route in routes},
            "unaffected_photo_route_preserved": PHOTO_ID in {route["id"] for route in routes},
            "minimal_route_defaults_complete": all(
                all(isinstance(step.get(key), str) and step[key].strip()
                    for key in ("label", "title", "instruction", "action", "evidenceLabel", "alt"))
                for step in normalized_route["steps"]
            ),
            "manual_step_copy_preserved": all(
                preserved_route["steps"][0][key] == value for key, value in manual_copy.items()
            ),
            "coordinate_validation_cases_rejected": getattr(
                self.__class__, "coordinate_validation_cases", []
            ),
        }
        (self.run_dir / "result-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.assertFalse(manifest["real_home_archive_or_photos_read"])

    def test_spoken_route_uses_record_copy_without_claiming_item_photo_proof(self) -> None:
        spoken_dir = Path(tempfile.mkdtemp(prefix="spoken-route-", dir=self.run_dir))
        spoken_photos = spoken_dir / "synthetic-photos"
        shutil.copytree(self.run_dir / "synthetic-photos", spoken_photos)
        spoken_catalog_path = spoken_dir / "catalog.json"
        spoken_catalog = copy.deepcopy(self.__class__.initial_catalog_data)
        spoken_item = next(item for item in spoken_catalog["items"] if item["id"] == PHOTO_ID)
        spoken_item["evidence"]["kind"] = "spoken"
        spoken_item["evidence"]["photos"][0]["label"] = "仅容器画面参照"
        spoken_catalog_path.write_text(
            json.dumps(spoken_catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        spoken_previous = spoken_dir / "before-move.html"
        page = self.__class__.old_guide_path.read_text(encoding="utf-8")
        spoken_routes = embedded_json(page, "guide-data")
        route = next(route for route in spoken_routes if route["id"] == PHOTO_ID)
        route["proof"] = "旁白"
        spoken_previous.write_text(
            replace_script_json(page, "guide-data", spoken_routes), encoding="utf-8"
        )

        check = self.run_process([
            sys.executable, str(CATALOG_CLI), "--catalog", str(spoken_catalog_path), "check",
        ])
        self.assertEqual(json.loads(check.stdout)["status"], "ok")
        spoken_output = spoken_dir / "after-move.html"
        export = self.run_process([
            sys.executable, str(EXPORT_CLI),
            "--catalog", str(spoken_catalog_path),
            "--template", str(TEMPLATE),
            "--previous", str(spoken_previous),
            "--out", str(spoken_output),
        ])
        self.assertIn("2 verified guide routes", export.stdout)

        exported_page = spoken_output.read_text(encoding="utf-8")
        projection = embedded_json(exported_page, "catalog-data")
        retained_routes = embedded_json(exported_page, "guide-data")
        exported_item = next(item for item in projection["items"] if item["id"] == PHOTO_ID)
        retained_route = next(route for route in retained_routes if route["id"] == PHOTO_ID)
        self.assertEqual(exported_item["kind"], "spoken")
        self.assertEqual(retained_route["proof"], "旁白")
        self.assertEqual(exported_item["itemPhotos"][0]["label"], "仅容器画面参照")
        self.assertTrue(all(step["src"].startswith("data:image/jpeg;base64,") for step in retained_route["steps"]))

        normalized = run_guide_for(TEMPLATE, exported_item, retained_route)
        last_step = normalized["steps"][-1]
        self.assertEqual(last_step["label"], "查看记录")
        self.assertEqual(last_step["title"], "查看旁白记录")
        self.assertEqual(last_step["action"], "查看记录")
        self.assertEqual(last_step["evidenceLabel"], "位置参照")
        self.assertEqual(last_step["alt"], "仅容器画面参照")
        self.assertIn("旁白记录", last_step["instruction"])
        self.assertIn("照片只作收纳范围参照", last_step["instruction"])
        self.assertNotIn("物品实拍", last_step["instruction"])
        self.assertNotIn("核对身份与外观", last_step["instruction"])
        for step in normalized["steps"]:
            for key in ("label", "title", "instruction", "action", "evidenceLabel", "alt"):
                self.assertIsInstance(step.get(key), str)
                self.assertTrue(step[key].strip(), f"spoken guideFor left {key} empty")

        manifest = {
            "scope": "synthetic spoken A10 only",
            "real_home_archive_or_photos_read": False,
            "tested_skill_root": str(SKILL_ROOT),
            "template_sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
            "exporter_sha256": hashlib.sha256(EXPORT_CLI.read_bytes()).hexdigest(),
            "catalog": str(spoken_catalog_path),
            "before_move_html": str(spoken_previous),
            "after_move_html": str(spoken_output),
            "route_proof": retained_route["proof"],
            "item_kind": exported_item["kind"],
            "container_photo_label_preserved": last_step["alt"],
            "item_visual_claims_absent": (
                "物品实拍" not in last_step["instruction"]
                and "核对身份与外观" not in last_step["instruction"]
            ),
            "spoken_display_fields_complete": all(
                all(isinstance(step.get(key), str) and step[key].strip()
                    for key in ("label", "title", "instruction", "action", "evidenceLabel", "alt"))
                for step in normalized["steps"]
            ),
        }
        (spoken_dir / "spoken-result-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.assertFalse(manifest["real_home_archive_or_photos_read"])


if __name__ == "__main__":
    unittest.main()
