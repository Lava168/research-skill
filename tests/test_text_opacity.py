"""Source text opacity must survive the real native PPTX construction pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile


SKILL_ROOT = Path(__file__).resolve().parents[1]
NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


def text_shapes(pptx: Path):
    with ZipFile(pptx) as package:
        slide = ET.fromstring(package.read("ppt/slides/slide1.xml"))
    shapes = {}
    for shape in slide.findall(".//p:sp", NS):
        body = shape.find("p:txBody", NS)
        if body is not None:
            shapes["".join(node.text or "" for node in body.findall(".//a:t", NS))] = shape
    return slide, shapes


class TextOpacityTests(unittest.TestCase):
    def test_zero_and_half_opacity_preserve_editable_text_and_opaque_defaults(self):
        # Ignoring opacity would expose hidden OCR text; patching only run colors
        # would leave opaque inherited text formatting behind in the same body.
        with tempfile.TemporaryDirectory(prefix="pdf-ppt-opacity-") as temporary:
            work = Path(temporary) / "Opacity 构建"
            work.mkdir()
            texts = []
            for i, (text, opacity) in enumerate([
                ("Hidden OCR", 0), ("Half transparent", 0.5),
                ("Default opacity", None), ("Explicit opaque", 1),
            ]):
                origin_y = 30 + i * 25
                record = {
                    "kind": "text", "text": text, "font": "Helvetica",
                    "font_size": 10, "flags": 0, "origin": [10, origin_y],
                    "bbox": [10, origin_y - 10, 100, origin_y + 2],
                    "width": 90, "color": "#123456", "dir": [1, 0],
                    "panel": "page",
                }
                if opacity is not None:
                    record["opacity"] = opacity
                texts.append(record)
            native = {
                "page": {"width": 216, "height": 288, "page_number": 1},
                "panels": {}, "paths": [], "texts": texts, "raster_regions": [],
            }
            (work / "native_elements.json").write_text(json.dumps(native), encoding="utf-8")
            (work / "image_assets.json").write_text("[]", encoding="utf-8")
            config = work / "config.json"
            config.write_text("{}", encoding="utf-8")
            node = os.environ.get("CODEX_PRIMARY_RUNTIME_NODE") or shutil.which("node")
            self.assertTrue(node, "Node.js is required for the real builder contract test")
            for command in (
                [node, str(SKILL_ROOT / "scripts" / "build_pptx.mjs")],
                [sys.executable, str(SKILL_ROOT / "scripts" / "group_objects.py")],
            ):
                result = subprocess.run(command + ["--work-dir", str(work), "--config", str(config)],
                                        capture_output=True, text=True, timeout=180)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            _, raw_shapes = text_shapes(work / "candidate_raw.pptx")
            grouped_slide, grouped_shapes = text_shapes(work / "candidate_grouped.pptx")
            self.assertEqual(set(grouped_shapes), {"Hidden OCR", "Half transparent", "Default opacity", "Explicit opaque"})
            self.assertEqual(len(grouped_slide.findall(".//p:sp", NS)), 4)
            self.assertEqual(grouped_slide.findall(".//p:pic", NS), [])

            for text, expected_alpha in (("Hidden OCR", "0"), ("Half transparent", "50000")):
                with self.subTest(text=text):
                    body = grouped_shapes[text].find("p:txBody", NS)
                    fills = body.findall(".//a:solidFill", NS)
                    self.assertTrue(fills, "Native text should have explicit editable colors")
                    for fill in fills:
                        color = list(fill)[0]
                        alpha = color.find("a:alpha", NS)
                        self.assertIsNotNone(alpha, f"{text}: a text-body color lost its source opacity")
                        self.assertEqual(alpha.get("val"), expected_alpha)
                    self.assertIsNotNone(body.find(".//a:r/a:t", NS), "Text was replaced by raster artwork")

            for text in ("Default opacity", "Explicit opaque"):
                # Opacity 1 must leave the established export unchanged.
                self.assertEqual(ET.tostring(raw_shapes[text].find("p:txBody", NS)),
                                 ET.tostring(grouped_shapes[text].find("p:txBody", NS)))
            for text, shape in grouped_shapes.items():
                self.assertEqual(ET.tostring(raw_shapes[text].find("p:spPr/a:xfrm", NS)),
                                 ET.tostring(shape.find("p:spPr/a:xfrm", NS)))
            index = {record["text"]: record for record in json.loads((work / "object_index.json").read_text())}
            self.assertEqual(index["Hidden OCR"].get("text_opacity"), 0)
            self.assertEqual(index["Half transparent"].get("text_opacity"), 0.5)
            self.assertEqual(index["Default opacity"].get("text_opacity"), 1)


if __name__ == "__main__":
    unittest.main()
