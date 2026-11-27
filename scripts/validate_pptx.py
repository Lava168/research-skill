#!/usr/bin/env python3
"""Validate the editable candidate against its extracted source manifests.

This checks package structure and preservation of native objects. It does not
claim visual equivalence, font rendering, or execution in native PowerPoint.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import posixpath
import sys
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"p": P, "a": A, "r": R}
KINDS = ("text", "vector", "image")


def _relationship_source(name: str) -> str:
    if name == "_rels/.rels":
        return ""
    parent, filename = posixpath.split(name)
    if posixpath.basename(parent) != "_rels" or not filename.endswith(".rels"):
        raise ValueError("Relationship part has an invalid location")
    return posixpath.join(posixpath.dirname(parent), filename[:-5])


def _relationship_target(source: str, target: str) -> str:
    uri = urlsplit(target)
    if uri.scheme or uri.netloc:
        raise ValueError("Internal relationship target is a URL")
    path = unquote(uri.path)
    if not path or "\\" in path:
        raise ValueError("Internal relationship target has an invalid path")
    resolved = posixpath.normpath(path.lstrip("/") if path.startswith("/")
                                  else posixpath.join(posixpath.dirname(source), path))
    if resolved in (".", "..") or resolved.startswith("../"):
        raise ValueError("Internal relationship target leaves the package")
    return resolved


def validate(pptx: Path, work: Path) -> dict:
    """Write work/verification.json and return a non-throwing validation report.

    Input or package failures are reported in ``errors``. A failure to write the
    report itself is an I/O error and is allowed to propagate to the caller.
    """
    pptx, work = Path(pptx), Path(work)
    report = {
        "schema_version": "pdf-figure-verification.v1",
        "pptx": str(pptx.resolve()),
        "passed": False,
        "status": "fail",
        "errors": [],
        "checks": {},
        "claim_boundary": "Source text, dimensions, native-object counts and package structure only; visual fidelity and native PowerPoint behavior require separate review.",
    }

    def error(code: str, message: str, **details) -> None:
        report["errors"].append({"code": code, "message": message, **details})

    def manifest(name: str, kind: type):
        try:
            value = json.loads((work / name).read_text(encoding="utf8"))
            if not isinstance(value, kind):
                raise ValueError(f"Expected {kind.__name__}")
            return value
        except (OSError, ValueError, TypeError) as exc:
            error("invalid_manifest", str(exc), file=name)
            return kind()

    def finish() -> dict:
        report["error_count"] = len(report["errors"])
        report["passed"] = not report["errors"]
        report["status"] = "pass" if report["passed"] else "fail"
        work.mkdir(parents=True, exist_ok=True)
        (work / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
        return report

    native = manifest("native_elements.json", dict)
    index = manifest("object_index.json", list)
    grouping = manifest("grouping_report.json", dict)
    expected_size = None
    try:
        width, height = (float(native["page"][key]) for key in ("width", "height"))
        if not all(math.isfinite(value) and value > 0 for value in (width, height)):
            raise ValueError("PDF dimensions must be finite and positive")
        # Positive nearest-integer rounding matches the JS builder's Math.round.
        expected_size = [math.floor(width * 12700 + 0.5), math.floor(height * 12700 + 0.5)]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        error("invalid_source_dimensions", str(exc))

    expected_text = Counter()
    try:
        text_values = [item["text"] for item in native["texts"]]
        if not all(isinstance(value, str) for value in text_values):
            raise ValueError("Every source text span must contain a string")
        expected_text.update(text_values)
    except (KeyError, TypeError, ValueError) as exc:
        error("invalid_source_text", str(exc))

    expected_counts = Counter()
    for item in index:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            error("invalid_object_index", "Object index contains an unsupported or missing kind")
        else:
            expected_counts[item["kind"]] += 1
    group_mapping = grouping.get("groups")
    if not isinstance(group_mapping, dict):
        error("invalid_grouping_report", "grouping_report.json must contain a groups mapping")
        group_mapping = {}

    try:
        payload = pptx.read_bytes()
        report["pptx_sha256"] = hashlib.sha256(payload).hexdigest()
        report["byte_count"] = len(payload)
        with ZipFile(pptx) as package:
            bad_member = package.testzip()
            if bad_member:
                error("invalid_zip", "ZIP CRC check failed", part=bad_member)
                return finish()
            members = [item.filename for item in package.infolist() if not item.is_dir()]
            duplicates = [name for name, count in Counter(members).items() if count > 1]
            if duplicates:
                error("duplicate_package_part", "ZIP contains duplicate member names", parts=duplicates)
            names = set(members)
            for required in ("[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"):
                if required not in names:
                    error("missing_package_part", "Required package part is absent", part=required)
            xml = {}
            for name in sorted(names):
                if name.endswith((".xml", ".rels")):
                    try:
                        xml[name] = ET.fromstring(package.read(name))
                    except ET.ParseError as exc:
                        error("invalid_xml", str(exc), part=name)
            report["checks"]["zip_xml"] = {"parts": len(names), "parsed_xml_parts": len(xml)}

            content_types = xml.get("[Content_Types].xml")
            if content_types is not None:
                defaults = {item.get("Extension", "").lower() for item in content_types.findall(f"{{{CT}}}Default")}
                overrides = {unquote(item.get("PartName", "")).lstrip("/") for item in content_types.findall(f"{{{CT}}}Override")}
                for name in sorted(names - {"[Content_Types].xml"}):
                    if name not in overrides and name.rsplit(".", 1)[-1].lower() not in defaults:
                        error("missing_content_type", "Package part has no content type", part=name)

            relationships = {}
            relationship_count = 0
            for name, tree in xml.items():
                if not name.endswith(".rels"):
                    continue
                try:
                    source = _relationship_source(name)
                except ValueError as exc:
                    error("invalid_relationship_part", str(exc), part=name)
                    continue
                if source and source not in names:
                    error("missing_relationship_source", "Relationship source part is absent", part=name, source=source)
                if tree.tag != f"{{{REL}}}Relationships":
                    error("invalid_relationship_part", "Relationship part has an unexpected root", part=name)
                mapping = {}
                for relation in tree.findall(f"{{{REL}}}Relationship"):
                    relationship_count += 1
                    rid, target, reltype = (relation.get(key, "") for key in ("Id", "Target", "Type"))
                    if not rid or not target or not reltype:
                        error("invalid_relationship", "Relationship requires Id, Target and Type", part=name)
                        continue
                    if rid in mapping:
                        error("duplicate_relationship_id", "Relationship ID is not unique", part=name, id=rid)
                    external = relation.get("TargetMode") == "External"
                    resolved = target
                    if not external:
                        try:
                            resolved = _relationship_target(source, target)
                        except ValueError as exc:
                            error("invalid_relationship_target", str(exc), part=name, target=target)
                            continue
                        if resolved not in names:
                            error("missing_relationship_target", "Internal relationship target is absent", part=name, target=resolved)
                    mapping[rid] = {"target": resolved, "type": reltype, "external": external}
                relationships[source] = mapping
            report["checks"]["relationships"] = {"parts": len(relationships), "relationships": relationship_count}

            office_relations = relationships.get("", {}).values()
            if not any(item["type"].endswith("/officeDocument") and item["target"] == "ppt/presentation.xml" and not item["external"] for item in office_relations):
                error("missing_presentation_relationship", "Package root does not reference the presentation")
            for name, tree in xml.items():
                if name.endswith(".rels"):
                    continue
                mapping = relationships.get(name, {})
                for node in tree.iter():
                    for attr in (f"{{{R}}}id", f"{{{R}}}embed", f"{{{R}}}link"):
                        if attr in node.attrib and node.get(attr) not in mapping:
                            error("missing_relationship_reference", "XML references an unknown relationship ID", part=name, id=node.get(attr))

            slides = {name: tree for name, tree in xml.items() if tree.tag == f"{{{P}}}sld"}
            presentation = xml.get("ppt/presentation.xml")
            slide_refs = presentation.findall("p:sldIdLst/p:sldId", NS) if presentation is not None else []
            if len(slides) != 1 or len(slide_refs) != 1:
                error("slide_count_mismatch", "Exactly one selected-page slide is required", package_slides=len(slides), presentation_slides=len(slide_refs))
            report["checks"]["slides"] = {"expected": 1, "package": len(slides), "presentation": len(slide_refs)}
            for reference in slide_refs:
                relation = relationships.get("ppt/presentation.xml", {}).get(reference.get(f"{{{R}}}id"))
                if not relation or not relation["type"].endswith("/slide") or relation["target"] not in slides or relation["external"]:
                    error("invalid_slide_relationship", "Presentation slide reference does not resolve to a native slide")

            actual_size = None
            if presentation is not None:
                try:
                    size = presentation.find("p:sldSz", NS)
                    actual_size = [int(size.get(key)) for key in ("cx", "cy")]
                except (AttributeError, TypeError, ValueError):
                    error("invalid_slide_size", "Presentation does not declare valid dimensions")
            if expected_size is not None and actual_size != expected_size:
                error("slide_size_mismatch", "Slide dimensions differ from the source PDF", expected=expected_size, actual=actual_size)
            report["checks"]["slide_size_emu"] = {"expected": expected_size, "actual": actual_size}

            actual_counts, actual_text = Counter(), Counter()
            actual_groups = 0
            picture_count = 0
            for name, tree in slides.items():
                identifiers = [node.get("id") for node in tree.findall(".//p:cNvPr", NS)]
                repeated = [identifier for identifier, count in Counter(identifiers).items() if count > 1]
                if repeated:
                    error("duplicate_object_id", "Native object IDs are not unique within the slide", part=name, ids=repeated)
                if any(identifier is None or not identifier.isdigit() for identifier in identifiers):
                    error("invalid_object_id", "Native object ID is absent or nonnumeric", part=name)
                actual_groups += len(tree.findall(".//p:grpSp", NS))
                for shape in tree.findall(".//p:sp", NS):
                    body = shape.find("p:txBody", NS)
                    if body is None:
                        actual_counts["vector"] += 1
                    else:
                        actual_counts["text"] += 1
                        actual_text["".join(node.text or "" for node in body.findall(".//a:t", NS))] += 1
                actual_counts["vector"] += len(tree.findall(".//p:cxnSp", NS))
                pictures = tree.findall(".//p:pic", NS)
                actual_counts["image"] += len(pictures)
                for picture in pictures:
                    picture_count += 1
                    blip = picture.find("p:blipFill/a:blip", NS)
                    rid = blip.get(f"{{{R}}}embed") if blip is not None else None
                    relation = relationships.get(name, {}).get(rid)
                    if not relation or relation["external"] or not relation["type"].endswith("/image"):
                        error("missing_picture_relationship", "Picture has no resolvable embedded image relationship", part=name, id=rid)
                    elif relation["target"] not in names:
                        error("missing_picture_media", "Picture image bytes are absent", part=name, target=relation["target"])
                    elif not package.getinfo(relation["target"]).file_size:
                        error("empty_picture_media", "Picture image part is empty", part=name, target=relation["target"])

            if actual_text != expected_text:
                missing, extra = expected_text - actual_text, actual_text - expected_text
                error("source_text_mismatch", "Native text-box contents differ from the exact source text multiset",
                      missing=dict(missing), extra=dict(extra))
            report["checks"]["source_text"] = {"expected_spans": sum(expected_text.values()), "actual_textboxes": sum(actual_text.values()), "exact_multiset_match": actual_text == expected_text}
            for kind in KINDS:
                if actual_counts[kind] != expected_counts[kind]:
                    error("object_count_mismatch", "Native object count differs from object_index.json", kind=kind, expected=expected_counts[kind], actual=actual_counts[kind])
            report["checks"]["object_counts"] = {"expected": {kind: expected_counts[kind] for kind in KINDS}, "actual": {kind: actual_counts[kind] for kind in KINDS}}
            if actual_groups != len(group_mapping):
                error("group_count_mismatch", "Native group count differs from grouping_report.json", expected=len(group_mapping), actual=actual_groups)
            report["checks"]["groups"] = {"expected": len(group_mapping), "actual": actual_groups}
            report["checks"]["pictures"] = {"picture_count": picture_count, "media_parts": sum(name.startswith("ppt/media/") for name in names)}
    except (BadZipFile, OSError, RuntimeError, EOFError) as exc:
        error("invalid_zip", str(exc))
    return finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pptx", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate(args.pptx, args.work_dir)
    except OSError as exc:
        report = {"schema_version": "pdf-figure-verification.v1", "passed": False, "status": "fail", "errors": [{"code": "report_io_error", "message": str(exc)}]}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
