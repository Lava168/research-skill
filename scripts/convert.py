#!/usr/bin/env python3
"""One PDF page → source-size editable PPTX; no network or implicit installs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def node_executable() -> str:
    node = os.environ.get("CODEX_PRIMARY_RUNTIME_NODE") or shutil.which("node")
    if not node:
        raise RuntimeError("Node.js was not found. This workflow requires Node.js and the Artifact Tool runtime.")
    return node


def presentation_root() -> Path:
    explicit = os.environ.get("PDF_PPT_PRESENTATIONS_SKILL_ROOT")
    return Path(explicit) if explicit else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills/builtins/presentations"


def runtime_environment() -> dict:
    env = os.environ.copy()
    env["PDF_PPT_PYTHON"] = sys.executable
    env.setdefault("RUNTIME_NODE", node_executable())
    env.setdefault("RUNTIME_PYTHON", sys.executable)
    if env.get("CODEX_PRIMARY_RUNTIME_NODE_MODULES"):
        env.setdefault("RUNTIME_NODE_MODULES", env["CODEX_PRIMARY_RUNTIME_NODE_MODULES"])
    runtime = env.get("CODEX_PRIMARY_RUNTIME_ROOT") or env.get("CODEX_PRIMARY_RUNTIME")
    if runtime:
        env.setdefault("RUNTIME_BIN_DIR", str(Path(runtime) / "dependencies/bin/override"))
    return env


def doctor() -> dict:
    python_deps = {}
    for distribution, module in (("PyMuPDF", "fitz"), ("Pillow", "PIL"), ("numpy", "numpy"), ("lxml", "lxml")):
        try:
            python_deps[distribution] = {"available": importlib.util.find_spec(module) is not None, "version": importlib.metadata.version(distribution)}
        except (ImportError, importlib.metadata.PackageNotFoundError):
            python_deps[distribution] = {"available": False}
    node = os.environ.get("CODEX_PRIMARY_RUNTIME_NODE") or shutil.which("node")
    node_deps = {}
    if node:
        # Read package metadata through the configured runtime or normal Node resolution.
        probe = r'''
const fs=require('node:fs'),path=require('node:path'),{createRequire}=require('node:module');
const bases=[process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES,path.resolve(process.cwd(),'node_modules')].filter(Boolean);
const out={};
for(const name of ['@oai/artifact-tool','@napi-rs/canvas']) {
 let found;
 for(const base of bases) { const p=path.join(base,name,'package.json');if(fs.existsSync(p)){found=p;break;} }
 if(!found) {try { let p=require.resolve(name);while(path.dirname(p)!==p){p=path.dirname(p);const q=path.join(p,'package.json');if(fs.existsSync(q)){found=q;break;}} }catch{} }
 out[name]=found?{available:true,version:JSON.parse(fs.readFileSync(found,'utf8')).version}:{available:false};
}
console.log(JSON.stringify(out));
'''
        process = subprocess.run([node, "-e", probe], cwd=SKILL, text=True, capture_output=True)
        if process.returncode == 0:
            node_deps = json.loads(process.stdout)
        else:
            node_deps = {"error": process.stderr.strip()}
    finalizer = presentation_root() / "container_tools/artifact_tool_utils.mjs"
    available = bool(node) and all(d["available"] for d in python_deps.values()) and all(node_deps.get(n, {}).get("available", False) for n in ("@oai/artifact-tool", "@napi-rs/canvas")) and finalizer.is_file()
    return {
        "ready": available,
        "python": sys.executable,
        "python_dependencies": python_deps,
        "node": node,
        "node_dependencies": node_deps,
        "presentations_finalizer": {"available": finalizer.is_file(), "root": str(presentation_root())},
        "optional_rendering": {"libreoffice": shutil.which("soffice") or shutil.which("libreoffice")},
        "dependency_note": "Artifact Tool and the Presentations finalizer are runtime-provided; this package does not redistribute or install them. See references/runtime.md.",
    }


def open_page(source: Path, page_number: int):
    import fitz
    if not source.is_file():
        raise FileNotFoundError(f"Input PDF does not exist: {source}")
    doc = fitz.open(source)
    if doc.needs_pass:
        raise ValueError("The PDF requires a password. Provide an authorized, decrypted working copy.")
    if page_number < 1 or page_number > len(doc):
        raise ValueError(f"Page must be between 1 and {len(doc)}; got {page_number}.")
    page = doc[page_number - 1]
    if page.rotation:
        raise ValueError("Rotated PDF page metadata is not supported by this extractor. Normalize a copy's visible rotation before conversion; do not merely change the rotation flag.")
    return doc, page


def inspect_pdf(source: Path, page_number: int, work: Path) -> dict:
    import fitz
    doc, page = open_page(source, page_number)
    work.mkdir(parents=True, exist_ok=True)
    drawings = page.get_drawings(extended=True)
    source_text = page.get_text("dict")
    images = page.get_image_info(xrefs=True)
    text_spans = [span for block in source_text["blocks"] if "lines" in block for line in block["lines"] for span in line["spans"] if span["text"].strip()]
    report = {
        "schema_version": 1, "source_file_name": source.name, "source_sha256": sha256(source),
        "page_number": page_number, "page_count": len(doc),
        "page_size_pt": [page.rect.width, page.rect.height],
        "slide_size_emu": [round(page.rect.width * 12700), round(page.rect.height * 12700)],
        "orientation": "portrait" if page.rect.height > page.rect.width else "landscape",
        "text_spans": len(text_spans),
        "fonts": sorted({span["font"] for span in text_spans}),
        "vector_paints": sum(d["type"] not in ("group", "clip") for d in drawings),
        "clip_count": sum(d["type"] == "clip" for d in drawings),
        "group_count": sum(d["type"] == "group" for d in drawings),
        "images": [{key: info.get(key) for key in ("number", "xref", "bbox", "width", "height", "transform")} for info in images],
        "clips": [{"bbox": list(d["scissor"]), "level": d["level"]} for d in drawings if d["type"] == "clip"],
        "readiness": "manual_reconstruction_required" if not text_spans and len(drawings) < 3 else "inspect_preview_and_configure_exceptions",
    }
    page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(work / "source.png")
    write_json(work / "inventory.json", report)
    doc.close()
    return report


def validate_config(config: dict, source: Path, page_number: int) -> None:
    if config.get("schema_version", 1) != 1:
        raise ValueError("Unsupported config schema_version; expected 1.")
    source_contract = config.get("source", {})
    if source_contract.get("sha256") and sha256(source) != source_contract["sha256"]:
        raise ValueError("Source SHA-256 does not match this figure preset. Use the default config or create a config for this PDF.")
    if source_contract.get("page_number", page_number) != page_number:
        raise ValueError("The selected page does not match the figure preset.")
    image_options = config.get("images", {})
    if any(image_options.get(k) for k in ("shadows", "tiles_by_xref", "clips_by_xref")) and not source_contract.get("sha256"):
        raise ValueError("Image xref-specific operations require source.sha256 in the config.")
    doc, page = open_page(source, page_number)
    for key, actual in (("width_pt", page.rect.width), ("height_pt", page.rect.height)):
        if key in source_contract and abs(float(source_contract[key]) - actual) > .02:
            raise ValueError(f"Page size does not match preset: {key}.")
    doc.close()


def run_stage(command: list[str], work: Path, env: dict) -> None:
    print(f"Running {Path(command[1]).name}", flush=True)
    process = subprocess.run(command, text=True, capture_output=True, env=env)
    with (work / "pipeline.log").open("a", encoding="utf-8") as log:
        log.write(json.dumps(command, ensure_ascii=False) + "\n" + process.stdout + process.stderr + "\n")
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()[-7000:]
        raise RuntimeError(f"{Path(command[1]).name} failed (exit {process.returncode}).\n{detail}")
    if process.stdout.strip():
        print(process.stdout.strip()[-2500:], flush=True)


def check_expected(config: dict, work: Path) -> dict:
    index = json.loads((work / "object_index.json").read_text(encoding="utf-8"))
    grouping = json.loads((work / "grouping_report.json").read_text(encoding="utf-8"))
    counts = {kind: sum(item["kind"] == kind for item in index) for kind in ("text", "vector", "image")}
    groups = grouping.get("groups", {})
    counts["groups"] = len(groups)
    for key, expected in config.get("expected", {}).items():
        if key in counts and counts[key] != expected:
            raise ValueError(f"Figure preset expectation failed: {key}: expected {expected}, got {counts[key]}.")
    return counts


def render_candidate(work: Path, env: dict) -> dict:
    import fitz
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise RuntimeError("--render requires LibreOffice (soffice or libreoffice on PATH).")
    render_dir = work / "rendered"
    render_dir.mkdir()
    with tempfile.TemporaryDirectory(prefix="pdf-ppt-lo-") as profile:
        command = [executable, "-env:UserInstallation=" + Path(profile).as_uri(), "--headless", "--convert-to", "pdf", "--outdir", str(render_dir), str(work / "candidate_grouped.pptx")]
        process = subprocess.run(command, text=True, capture_output=True, env=env)
        (work / "render.log").write_text(process.stdout + process.stderr, encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"LibreOffice rendering failed: {process.stderr.strip()}")
    pdf = render_dir / "candidate_grouped.pdf"
    if not pdf.is_file():
        raise RuntimeError("LibreOffice did not produce the expected rendered PDF.")
    with fitz.open(pdf) as document:
        document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(work / "preview.png")
    return {"rendered_pdf": str(pdf), "preview_png": str(work / "preview.png"), "visual_review_required": True}


def build(source: Path, page_number: int, config_path: Path, work: Path, output: Path, render: bool) -> dict:
    if output.suffix.lower() != ".pptx":
        raise ValueError("Output must have a .pptx extension.")
    if output.exists():
        raise FileExistsError(f"Output already exists; choose a new output filename: {output}")
    if work == output.parent or work.is_relative_to(output.parent):
        raise ValueError("Keep private build reports outside the final delivery directory. Choose separate sibling paths, for example --work-dir build-v1 --output deliverables/figure.pptx.")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config(config, source, page_number)
    for name in ("native_elements.json", "candidate_raw.pptx", "candidate_grouped.pptx", "finalization.json"):
        if (work / name).exists():
            raise FileExistsError(f"Work directory contains a previous build ({name}); choose a fresh --work-dir.")
    dependencies = doctor()
    if not dependencies["ready"]:
        raise RuntimeError("Required runtime dependencies are unavailable. Run `python scripts/convert.py doctor` and see references/runtime.md.")
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    original_digest = sha256(source)
    write_json(work / "resolved_config.json", config)
    write_json(work / "runtime.json", dependencies)
    node, env = node_executable(), runtime_environment()
    run_stage([sys.executable, str(HERE / "extract_pdf.py"), "--input", str(source), "--page", str(page_number), "--config", str(config_path), "--work-dir", str(work)], work, env)
    run_stage([node, str(HERE / "build_pptx.mjs"), "--work-dir", str(work), "--config", str(config_path)], work, env)
    run_stage([sys.executable, str(HERE / "group_objects.py"), "--work-dir", str(work), "--config", str(config_path)], work, env)
    counts = check_expected(config, work)
    run_stage([sys.executable, str(HERE / "validate_pptx.py"), "--pptx", str(work / "candidate_grouped.pptx"), "--work-dir", str(work)], work, env)
    rendering = render_candidate(work, env) if render else {"visual_review_required": True, "preview_png": None}
    if sha256(source) != original_digest:
        raise RuntimeError("Source PDF changed during processing; final output was not published.")
    run_stage([node, str(HERE / "finalize_pptx.mjs"), "--work-dir", str(work), "--output", str(output)], work, env)
    if sha256(output) != sha256(work / "candidate_grouped.pptx"):
        raise RuntimeError("Final output does not match the validated candidate.")
    report = {"status": "built_and_structurally_validated", "output": str(output), "sha256": sha256(output), "source_sha256": original_digest, "source_unchanged": True, "counts": counts, "rendering": rendering,
              "claim_boundary": "Editable text, native vector shapes and independent image components. Visual review remains required. No raw chart data or native PowerPoint execution is claimed."}
    write_json(work / "delivery.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Report dependencies without installing anything")
    for name in ("inspect", "build"):
        command = sub.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--page", type=int, default=1, help="1-based page number; exactly one page per invocation")
        command.add_argument("--work-dir", type=Path, required=True)
        if name == "build":
            command.add_argument("--config", type=Path, default=SKILL / "assets/default.json")
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--render", action="store_true", help="Also render candidate through LibreOffice for visual review")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ready"] else 2
        source, work = args.input.resolve(), args.work_dir.resolve()
        if args.command == "inspect":
            result = inspect_pdf(source, args.page, work)
            print(json.dumps({k: v for k, v in result.items() if k not in ("images", "clips")}, ensure_ascii=False, indent=2))
        else:
            result = build(source, args.page, args.config.resolve(), work, args.output.resolve(), args.render)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
