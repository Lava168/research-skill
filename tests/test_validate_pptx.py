"""Focused source/OOXML regression checks, without a presentation engine."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


SKILL_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = SKILL_ROOT / "scripts" / "validate_pptx.py"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"p": P, "a": A, "r": R}


def package_parts() -> dict[str, bytes]:
    """A small literal package whose correct counts are independently known."""
    slide = f'''<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">
      <p:cSld><p:spTree>
        <p:nvGrpSpPr><p:cNvPr id="1" name="Slide"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
        <p:grpSp><p:nvGrpSpPr><p:cNvPr id="2" name="Content"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
          <p:sp><p:nvSpPr><p:cNvPr id="3" name="text_one"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>
            <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Portable</a:t></a:r></a:p></p:txBody></p:sp>
          <p:sp><p:nvSpPr><p:cNvPr id="4" name="vector_one"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:custGeom/></p:spPr></p:sp>
          <p:pic><p:nvPicPr><p:cNvPr id="5" name="image_one"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
            <p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill><p:spPr/></p:pic>
          <p:sp><p:nvSpPr><p:cNvPr id="6" name="text_two"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>
            <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Portable</a:t></a:r></a:p></p:txBody></p:sp>
        </p:grpSp>
      </p:spTree></p:cSld>
    </p:sld>'''
    return {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>''',
        "_rels/.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rIdOffice" Type="{R}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'.encode(),
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="rIdSlide"/></p:sldIdLst><p:sldSz cx="3657600" cy="5486400"/></p:presentation>'.encode(),
        "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rIdSlide" Type="{R}/slide" Target="slides/slide1.xml"/></Relationships>'.encode(),
        "ppt/slides/slide1.xml": slide.encode(),
        "ppt/slides/_rels/slide1.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rIdImage" Type="{R}/image" Target="../media/pixel.png"/></Relationships>'.encode(),
        "ppt/media/pixel.png": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="),
    }


def make_fixture(root: Path, parts: dict[str, bytes] | None = None) -> tuple[Path, Path]:
    work = root / "验证 work"
    work.mkdir(parents=True)
    (work / "native_elements.json").write_text(json.dumps({
        "page": {"width": 288, "height": 432, "page_number": 1},
        "texts": [{"text": "Portable"}, {"text": "Portable"}],
    }), encoding="utf8")
    (work / "object_index.json").write_text(json.dumps([
        {"name": "text_one", "kind": "text"},
        {"name": "text_two", "kind": "text"},
        {"name": "vector_one", "kind": "vector"},
        {"name": "image_one", "kind": "image"},
    ]), encoding="utf8")
    (work / "grouping_report.json").write_text(json.dumps({
        "groups": {"content": 4}, "ungrouped_page_objects": 0, "objects": 4,
    }), encoding="utf8")
    pptx = root / "portable output.pptx"
    with ZipFile(pptx, "w", ZIP_DEFLATED) as package:
        for name, content in (package_parts() if parts is None else parts).items():
            package.writestr(name, content)
    return pptx, work


def import_validator():
    spec = importlib.util.spec_from_file_location("pdf_figure_validator", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidatorTests(unittest.TestCase):
    def test_cli_accepts_source_matching_package_and_writes_json(self):
        # A missing validator, wrong CLI, or report-only success breaks this.
        with tempfile.TemporaryDirectory(prefix="pdf-ppt-validator-") as directory:
            pptx, work = make_fixture(Path(directory))
            result = subprocess.run([
                sys.executable, str(VALIDATOR), "--pptx", str(pptx), "--work-dir", str(work),
            ], text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["passed"], report)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(json.loads((work / "verification.json").read_text()), report)

    def test_importable_validation_rejects_source_and_package_regressions(self):
        # Each mutation changes a user-visible integrity/editability guarantee.
        self.assertTrue(VALIDATOR.is_file(), "Validator entrypoint has not been implemented")
        validator = import_validator()
        mutations = [
            ("text multiplicity", "source_text_mismatch"),
            ("slide dimensions", "slide_size_mismatch"),
            ("duplicate object ID", "duplicate_object_id"),
            ("missing image bytes", "missing_relationship_target"),
            ("missing picture reference", "missing_picture_relationship"),
            ("invalid XML", "invalid_xml"),
            ("extra slide", "slide_count_mismatch"),
            ("native object count", "object_count_mismatch"),
            ("native group count", "group_count_mismatch"),
        ]
        for mutation, code in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="pdf-ppt-corrupt-") as directory:
                parts = package_parts()
                if mutation == "text multiplicity":
                    parts["ppt/slides/slide1.xml"] = parts["ppt/slides/slide1.xml"].replace(b">Portable<", b">Changed<", 1)
                elif mutation == "slide dimensions":
                    parts["ppt/presentation.xml"] = parts["ppt/presentation.xml"].replace(b'cy="5486400"', b'cy="5486401"')
                elif mutation == "duplicate object ID":
                    parts["ppt/slides/slide1.xml"] = parts["ppt/slides/slide1.xml"].replace(b'id="6"', b'id="3"')
                elif mutation == "missing image bytes":
                    del parts["ppt/media/pixel.png"]
                elif mutation == "missing picture reference":
                    parts["ppt/slides/slide1.xml"] = parts["ppt/slides/slide1.xml"].replace(b'r:embed="rIdImage"', b'r:embed="unresolved"')
                elif mutation == "invalid XML":
                    parts["ppt/slides/slide1.xml"] = b"<p:sld"
                elif mutation == "extra slide":
                    parts["ppt/slides/slide2.xml"] = parts["ppt/slides/slide1.xml"]
                elif mutation in ("native object count", "native group count"):
                    tree = ET.fromstring(parts["ppt/slides/slide1.xml"])
                    group = tree.find("p:cSld/p:spTree/p:grpSp", NS)
                    if mutation == "native object count":
                        group.remove(group.findall("p:sp", NS)[1])
                    else:
                        parent = tree.find("p:cSld/p:spTree", NS)
                        for child in list(group):
                            if child.tag in (f"{{{P}}}sp", f"{{{P}}}pic"):
                                parent.append(child)
                        parent.remove(group)
                    parts["ppt/slides/slide1.xml"] = ET.tostring(tree)
                pptx, work = make_fixture(Path(directory), parts)
                report = validator.validate(pptx, work)
                self.assertFalse(report["passed"], report)
                self.assertIn(code, [error["code"] for error in report["errors"]])

    def test_cli_rejects_invalid_zip_without_mutating_it(self):
        with tempfile.TemporaryDirectory(prefix="pdf-ppt-invalid-") as directory:
            pptx, work = make_fixture(Path(directory))
            pptx.write_bytes(b"not a zip file")
            result = subprocess.run([
                sys.executable, str(VALIDATOR), "--pptx", str(pptx), "--work-dir", str(work),
            ], text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip(), result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["passed"])
            self.assertIn("invalid_zip", [error["code"] for error in report["errors"]])
            self.assertEqual(pptx.read_bytes(), b"not a zip file")


if __name__ == "__main__":
    unittest.main()
