"""Independent PDF smoke test for the documented public conversion CLI."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import fitz
from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONVERT = SKILL_ROOT / "scripts" / "convert.py"
NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


def create_source(path: Path) -> None:
    """No original-figure assets, coordinates, xrefs or content are reused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=288, height=432)
    page.insert_text((24, 32), "Portable first page", fontsize=12)
    page.insert_text((260, 150), "Rotated label", fontsize=10, rotate=90)
    # Deliberately inside the old source-specific acquisition exclusion region.
    page.draw_rect(fitz.Rect(180, 75, 215, 95), color=(0, 0, 1), fill=(0.2, 0.5, 0.9))
    page.draw_line((30, 130), (160, 130), color=(0.6, 0.1, 0.1), width=1.5)
    shape = page.new_shape()
    shape.draw_bezier((30, 270), (60, 230), (110, 310), (150, 270))
    shape.finish(color=(0.1, 0.6, 0.2), width=2)
    shape.commit()
    image = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
    for y in range(4, 12):
        for x in range(4, 12):
            image.putpixel((x, y), (220, 40, 70, 128 if x < 8 else 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    page.insert_image(fitz.Rect(30, 160, 94, 224), stream=buffer.getvalue())
    second = doc.new_page(width=216, height=360)
    second.insert_text((20, 30), "Selected second page", fontsize=11)
    second.draw_rect(fitz.Rect(20, 55, 120, 100), color=(0, 0.5, 0))
    doc.save(path)
    doc.close()


def invoke(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CONVERT), *map(str, args)], text=True,
                          capture_output=True, timeout=timeout)


class PortableSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="pdf-ppt-portable-")
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "输入 源" / "unrelated portrait 图.pdf"
        create_source(cls.source)
        cls.source_hash = sha256(cls.source.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_doctor_and_inspect_use_the_public_cli(self):
        # Catches undocumented dependencies and an inspect path tied to old files.
        self.assert_success(invoke("doctor"))
        work = self.root / "inspect 检查"
        self.assert_success(invoke("inspect", "--input", self.source, "--page", "1", "--work-dir", work))
        self.assertTrue(list(work.glob("*.json")), "Inspect did not write an inventory")
        self.assertTrue(list(work.glob("*.png")), "Inspect did not write a page preview")
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_default_build_retains_native_objects_at_source_size(self):
        # Catches whole-page rasterization and the old a-j/acquisition assumptions.
        work = self.root / "default 构建"
        output = self.root / "输出 folder" / "editable portrait.pptx"
        self.assert_success(invoke("build", "--input", self.source, "--page", "1", "--work-dir", work, "--output", output))
        self.assertTrue(output.is_file())
        native = json.loads((work / "native_elements.json").read_text())
        index = json.loads((work / "object_index.json").read_text())
        groups = json.loads((work / "grouping_report.json").read_text())
        self.assertEqual(native["panels"], {})
        self.assertEqual(native.get("raster_regions", []), [])
        self.assertEqual(Counter(item["kind"] for item in index), {"text": 2, "vector": 3, "image": 1})
        self.assertTrue(any(all(abs(v-w) < 0.01 for v, w in zip(item["bbox"], [180, 75, 215, 95]))
                            for item in index if item["kind"] == "vector"), "Former acquisition-region vector disappeared")
        self.assertEqual(len(groups["groups"]), 0)
        self.assertTrue(json.loads((work / "verification.json").read_text())["passed"])
        with ZipFile(output) as package:
            slide = ET.fromstring(package.read("ppt/slides/slide1.xml"))
            presentation = ET.fromstring(package.read("ppt/presentation.xml"))
            size = presentation.find("p:sldSz", NS)
            self.assertEqual((int(size.get("cx")), int(size.get("cy"))), (3657600, 5486400))
            self.assertEqual(Counter(node.text for node in slide.findall(".//a:t", NS)),
                             {"Portable first page": 1, "Rotated label": 1})
            self.assertEqual(len(slide.findall(".//p:pic", NS)), 1)
            self.assertEqual(len(slide.findall(".//p:grpSp", NS)), 0)
            self.assertEqual(len(slide.findall(".//a:custGeom", NS)), 3)
            media = [name for name in package.namelist() if name.startswith("ppt/media/")]
            self.assertTrue(media)
            embedded = Image.open(BytesIO(package.read(media[0])))
            self.assertIn("A", embedded.getbands())
            self.assertEqual(embedded.getchannel("A").getextrema(), (0, 255))
            names = [node.get("name", "") for node in slide.findall(".//p:cNvPr", NS)]
            self.assertFalse(any(name.startswith(tuple(f"{letter}__" for letter in "abcdefghij")) for name in names))
        # Reusing an output must fail before touching the validated file.
        before = output.read_bytes()
        rejected = invoke("build", "--input", self.source, "--work-dir", self.root / "overwrite work", "--output", output)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_page_two_selects_its_text_and_dimensions(self):
        # Catches implicit page-zero selection and stale page dimensions.
        work = self.root / "page 2 work"
        output = self.root / "输出 folder" / "second page.pptx"
        self.assert_success(invoke("build", "--input", self.source, "--page", "2", "--work-dir", work, "--output", output))
        native = json.loads((work / "native_elements.json").read_text())
        self.assertEqual(native["page"]["page_number"], 2)
        with ZipFile(output) as package:
            slide = ET.fromstring(package.read("ppt/slides/slide1.xml"))
            presentation = ET.fromstring(package.read("ppt/presentation.xml"))
            self.assertEqual([node.text for node in slide.findall(".//a:t", NS)], ["Selected second page"])
            size = presentation.find("p:sldSz", NS)
            self.assertEqual((int(size.get("cx")), int(size.get("cy"))), (2743200, 4572000))
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_source_preset_hash_mismatch_is_rejected(self):
        # Catches accidentally applying private xrefs/crops to a different PDF.
        output = self.root / "输出 folder" / "mismatch.pptx"
        result = invoke("build", "--input", self.source, "--config", SKILL_ROOT / "assets" / "nature-figure1.json",
                        "--work-dir", self.root / "mismatch work", "--output", output)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex((result.stdout + result.stderr).lower(), r"sha256|hash|source")
        self.assertFalse(output.exists())
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_rejects_work_inside_output_parent_before_extraction(self):
        # Catches late finalizer failure after work artifacts pollute deliverables.
        for layout in ("same", "nested"):
            with self.subTest(layout=layout):
                output_parent = self.root / f"unsafe {layout} 输出"
                work = output_parent if layout == "same" else output_parent / "work"
                output = output_parent / "figure.pptx"
                result = invoke("build", "--input", self.source, "--page", "1",
                                "--work-dir", work, "--output", output)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertRegex((result.stdout + result.stderr).lower(), r"deliverables")
                self.assertFalse(output.exists())
                for name in ("native_elements.json", "image_assets.json", "extraction_report.json",
                             "candidate_raw.pptx", "candidate_grouped.pptx"):
                    self.assertFalse((work / name).exists(), f"Created {name} before rejecting the layout")
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), self.source_hash)


if __name__ == "__main__":
    unittest.main()
