#!/usr/bin/env python3
"""Extract a PDF page into editable geometry and explicitly identified rasters.

Coordinates are PDF points, measured from the page's top-left. This script never
saves to the source PDF. Document-specific selections belong in the JSON config.
The resulting files are candidates for rendering and visual review; limitations
are recorded in extraction_report.json, including effects PPT cannot reproduce.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re
import sys

import fitz
import numpy as np
from PIL import Image, ImageColor


class ExtractionError(ValueError):
    """Invalid configuration or an invariant which would lose source content."""


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def point(value):
    return [float(value.x), float(value.y)]


def same(a, b, tolerance=1e-4):
    return abs(a[0] - b[0]) < tolerance and abs(a[1] - b[1]) < tolerance


def rgb(color):
    if color is None:
        return None
    if len(color) == 1:
        color = color * 3
    if len(color) != 3:
        raise ExtractionError(f"Expected decoded RGB color, received {color!r}")
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02X}" for v in color)


def rect(value, label="bbox"):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ExtractionError(f"{label} must contain [x0,y0,x1,y1]")
    if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in value):
        raise ExtractionError(f"{label} must contain finite numbers")
    result = fitz.Rect(value)
    if result.is_empty or result.is_infinite:
        raise ExtractionError(f"{label} must have positive width and height")
    return result


def safe_name(value):
    value = re.sub(r"[^\w.-]+", "_", str(value), flags=re.UNICODE).strip("._")
    if not value:
        raise ExtractionError("An image or raster region has an empty filename")
    return value


def panel_for(box, panels):
    r = fitz.Rect(box)
    midpoint = (r.tl + r.br) * 0.5
    for name, bbox in panels.items():
        if fitz.Rect(bbox).contains(midpoint):
            return name
    return "page"


def commands(items, close=False):
    result, last = [], None

    def begin(value):
        nonlocal last
        if last is None or not same(last, value):
            result.append(["M", *value])
        last = value

    for item in items:
        kind = item[0]
        if kind == "l":
            a, b = point(item[1]), point(item[2])
            begin(a)
            result.append(["L", *b])
            last = b
        elif kind == "c":
            a, b, c, d = map(point, item[1:])
            begin(a)
            result.append(["C", *b, *c, *d])
            last = d
        elif kind in ("re", "qu"):
            if kind == "re":
                r = item[1]
                points = [[r.x0, r.y0], [r.x1, r.y0], [r.x1, r.y1], [r.x0, r.y1]]
                if len(item) > 2 and item[2] < 0:
                    points.reverse()
            else:
                q = item[1]
                points = [point(q.ul), point(q.ur), point(q.lr), point(q.ll)]
            result.append(["M", *points[0]])
            result.extend([["L", *q] for q in points[1:]])
            result.append(["Z"])
            last = None
        else:
            raise ExtractionError(f"Unsupported native path operation: {kind}")
    if close and last is not None:
        result.append(["Z"])
    return result


def bbox_commands(cmd):
    points = []
    for c in cmd:
        if c[0] in ("M", "L"):
            points.append(c[1:3])
        elif c[0] == "C":
            points.extend([c[1:3], c[3:5], c[5:7]])
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def flatten(cmd):
    """Flatten only geometry that actually requires clipping (0.25 pt steps)."""
    contours, current, last, first = [], [], None, None
    for c in cmd:
        if c[0] == "M":
            if current:
                contours.append(current)
            current = [c[1:3]]
            last = first = c[1:3]
        elif c[0] == "L":
            current.append(c[1:3])
            last = c[1:3]
        elif c[0] == "C":
            p0, p1, p2, p3 = last, c[1:3], c[3:5], c[5:7]
            length = sum(math.dist(a, b) for a, b in ((p0, p1), (p1, p2), (p2, p3)))
            steps = max(8, min(100, math.ceil(length / 0.25)))
            for k in range(1, steps + 1):
                t, u = k / steps, 1 - k / steps
                current.append([u**3 * p0[a] + 3*u*u*t*p1[a] + 3*u*t*t*p2[a] + t**3*p3[a]
                                for a in (0, 1)])
            last = p3
        elif c[0] == "Z":
            if current and not same(current[-1], current[0]):
                current.append(current[0])
            last = first
    if current:
        contours.append(current)
    return contours


def area(poly):
    return sum(a[0]*b[1] - b[0]*a[1] for a, b in zip(poly, poly[1:] + poly[:1])) / 2


def cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def convex_polygon(clip):
    contours = flatten(commands(clip["items"], True))
    if len(contours) != 1:
        return None
    poly = contours[0]
    if len(poly) > 1 and same(poly[0], poly[-1]):
        poly = poly[:-1]
    if len(poly) < 3 or abs(area(poly)) < 1e-9:
        return None
    if area(poly) < 0:
        poly.reverse()
    if any(cross(a, b, c) < -1e-7 for a, b, c in
           zip(poly, poly[1:] + poly[:1], poly[2:] + poly[:2])):
        return None
    return poly


def intersection(s, e, a, b):
    v, q = [e[0]-s[0], e[1]-s[1]], [b[0]-a[0], b[1]-a[1]]
    denominator = v[0]*q[1] - v[1]*q[0]
    if abs(denominator) < 1e-14:
        return e
    t = ((a[0]-s[0])*q[1] - (a[1]-s[1])*q[0]) / denominator
    return [s[0]+t*v[0], s[1]+t*v[1]]


def polygon_clip(subject, clip):
    result = subject
    for a, b in zip(clip, clip[1:] + clip[:1]):
        incoming, result = result, []
        if not incoming:
            break
        s = incoming[-1]
        for e in incoming:
            ei, si = cross(a, b, e) >= -1e-8, cross(a, b, s) >= -1e-8
            if ei:
                if not si:
                    result.append(intersection(s, e, a, b))
                result.append(e)
            elif si:
                result.append(intersection(s, e, a, b))
            s = e
    return result


def line_clip(a, b, clip):
    low, high = 0.0, 1.0
    for p, q in zip(clip, clip[1:] + clip[:1]):
        c0, c1 = cross(p, q, a), cross(p, q, b)
        if c0 < 0 and c1 < 0:
            return None
        if (c0 < 0) != (c1 < 0):
            t = c0 / (c0-c1)
            if c0 < 0:
                low = max(low, t)
            else:
                high = min(high, t)
    if low > high:
        return None
    return [[a[k]+(b[k]-a[k])*low for k in (0, 1)],
            [a[k]+(b[k]-a[k])*high for k in (0, 1)]]


def graphics_contexts(page):
    """Trace actual painting order, including image alpha and active page clips.

    MuPDF's public Python drawing inventory omits image graphics states. Its
    device wrapper supplies them without parsing or rewriting PDF content.
    """
    m = fitz.mupdf

    class Device(m.FzDevice2):
        def __init__(self):
            super().__init__()
            self.seqno, self.groups, self.clips, self.contexts = 0, [], [], {}
            self.mask_definitions = []
            for op in ("fill_path", "stroke_path", "fill_text", "stroke_text", "ignore_text",
                       "fill_shade", "fill_image", "fill_image_mask", "begin_group", "end_group",
                       "clip_path", "clip_stroke_path", "clip_text", "clip_stroke_text",
                       "clip_image_mask", "pop_clip", "begin_mask", "end_mask"):
                getattr(self, "use_virtual_" + op)()

        def record(self, kind, alpha=1.0):
            self.contexts[self.seqno] = {
                "kind": kind, "alpha": float(alpha), "groups": [dict(g) for g in self.groups],
                "clips": [dict(c) for c in self.clips],
                "mask_definition": bool(self.mask_definitions),
            }
            self.seqno += 1

        def fill_path(self, ctx, path, evenodd, matrix, cs, color, alpha, params):
            self.record("fill-path", alpha)

        def stroke_path(self, ctx, path, stroke, matrix, cs, color, alpha, params):
            self.record("stroke-path", alpha)

        def fill_text(self, ctx, text, matrix, cs, color, alpha, params):
            self.record("fill-text", alpha)

        def stroke_text(self, ctx, text, stroke, matrix, cs, color, alpha, params):
            self.record("stroke-text", alpha)

        def ignore_text(self, ctx, *args):
            self.record("ignore-text", 0.0)

        def fill_shade(self, ctx, shade, matrix, alpha, params):
            self.record("fill-shade", alpha)

        def fill_image(self, ctx, image, matrix, alpha, params):
            self.record("fill-image", alpha)

        def fill_image_mask(self, ctx, image, matrix, cs, color, alpha, params):
            self.record("fill-imgmask", alpha)

        def begin_group(self, ctx, bbox, cs, isolated, knockout, blend, alpha):
            self.groups.append({"blendmode": m.fz_blendmode_name(blend), "opacity": float(alpha),
                                "isolated": bool(isolated), "knockout": bool(knockout),
                                "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1]})

        def end_group(self, ctx):
            self.groups.pop()

        def clip_path(self, ctx, path, evenodd, matrix, scissor):
            box = m.ll_fz_bound_path(path, None, matrix)
            kind = "rect" if m.ll_fz_path_is_rect(path, matrix) else "path"
            self.clips.append({"kind": kind, "bbox": [box.x0, box.y0, box.x1, box.y1]})

        def clip_stroke_path(self, ctx, path, stroke, matrix, scissor):
            box = m.ll_fz_bound_path(path, stroke, matrix)
            self.clips.append({"kind": "stroked_path", "bbox": [box.x0, box.y0, box.x1, box.y1]})

        def clip_text(self, ctx, *args):
            self.clips.append({"kind": "text"})

        clip_stroke_text = clip_text

        def clip_image_mask(self, ctx, image, matrix, scissor):
            box = m.ll_fz_transform_rect(m.fz_unit_rect, matrix)
            self.clips.append({"kind": "image_mask", "bbox": [box.x0, box.y0, box.x1, box.y1]})

        def pop_clip(self, ctx):
            if self.clips:
                self.clips.pop()

        def begin_mask(self, ctx, bbox, luminosity, colorspace, background, params):
            self.mask_definitions.append([bbox.x0, bbox.y0, bbox.x1, bbox.y1])

        def end_mask(self, ctx, *args):
            self.clips.append({"kind": "graphics_softmask", "bbox": self.mask_definitions.pop()})

    dev = Device()
    m.fz_run_page(page.this, dev, m.FzMatrix(), m.FzCookie())
    m.fz_close_device(dev)
    expected = page.get_bboxlog()
    if len(expected) != dev.seqno or any(expected[k][0] != v["kind"] for k, v in dev.contexts.items()):
        raise ExtractionError("MuPDF graphics trace and painting-order inventory disagree")
    return dev.contexts


def group_opacity(context):
    return math.prod(g["opacity"] for g in context.get("groups", []))


def clipped_alpha(image, bbox, clip_bbox):
    """Bake exact fractional source-pixel coverage into straight alpha."""
    box, clip = fitz.Rect(bbox), fitz.Rect(clip_bbox)
    w, h = image.size
    left, right = (clip.x0-box.x0)/box.width*w, (clip.x1-box.x0)/box.width*w
    top, bottom = (clip.y0-box.y0)/box.height*h, (clip.y1-box.y0)/box.height*h
    x, y = np.arange(w), np.arange(h)
    xc = np.clip(np.minimum(x+1, right) - np.maximum(x, left), 0, 1)
    yc = np.clip(np.minimum(y+1, bottom) - np.maximum(y, top), 0, 1)
    rgba = image.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"), dtype=float)
    rgba.putalpha(Image.fromarray(np.rint(alpha * yc[:, None] * xc[None, :]).astype("uint8")))
    return rgba, [left, top, right, bottom]


def flatten_background(image, color):
    background = Image.new("RGBA", image.size, ImageColor.getrgb(color) + (255,))
    background.alpha_composite(image.convert("RGBA"))
    return background.convert("RGB")


def effective_shadow(source, page_index, spec, pixel_size):
    """Evaluate a configured grayscale form and encode its complete effect as alpha.

    The grayscale invariant is required: color Multiply artwork cannot generally
    be represented by a normal black image over arbitrary backgrounds.
    """
    width, height = pixel_size
    with fitz.open(source) as document:
        page = document[page_index]
        form = int(spec["form_xref"])
        name, state = spec["form_name"], spec["graphics_state"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.-]+", state):
            raise ExtractionError("Shadow form_name and graphics_state must be PDF resource names")
        stream = document.xref_stream(form).decode("latin1")
        number = r"([-+]?(?:\d*\.\d+|\d+\.?\d*))"
        match = re.search(rf"{number}\s+0\s+0\s+{number}\s+{number}\s+{number}\s+cm", stream)
        if not match:
            raise ExtractionError(f"Configured shadow form {form} has no supported image transform")
        a, d, e, f = map(float, match.groups())
        if a <= 0 or d <= 0:
            raise ExtractionError("Configured shadow form must have positive axis-aligned scale")
        sx, sy = width/a, height/d
        page.set_mediabox(fitz.Rect(0, 0, width, height))
        page.set_cropbox(fitz.Rect(0, 0, width, height))
        content = int(spec["page_content_xref"])
        replacement = (f"q\n{sx:.12f} 0 0 {sy:.12f} {-e*sx:.12f} {-f*sy:.12f} cm\n"
                       f"/{state} gs\n/{name} Do\nQ\n")
        document.update_stream(content, replacement.encode("ascii"))
        page.set_contents(content)
        pix = page.get_pixmap(alpha=False)
        reference = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if reference.size != (width, height):
            raise ExtractionError("Shadow native-pixel rendering changed its dimensions")
        array = np.asarray(reference)
        if not np.array_equal(array[:, :, 0], array[:, :, 1]) or not np.array_equal(array[:, :, 0], array[:, :, 2]):
            raise ExtractionError("Configured shadow is colored; black RGBA would change its appearance")
        rgba = Image.new("RGBA", pixel_size, (0, 0, 0, 0))
        rgba.putalpha(Image.fromarray(255-array[:, :, 0]))
        return rgba


def validate_tiles(tiles, width, height):
    """Reject omissions, overlaps and fractional crops before writing any tile."""
    coverage = np.zeros((height, width), dtype=np.uint16)
    for tile in tiles:
        crop = tile.get("crop_px")
        if (not isinstance(crop, list) or len(crop) != 4
                or any(not isinstance(v, int) or isinstance(v, bool) for v in crop)):
            raise ExtractionError("Each tile crop_px must contain four integer pixel boundaries")
        x0, y0, x1, y1 = crop
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ExtractionError(f"Tile crop {crop} is outside the {width}x{height} source image")
        coverage[y0:y1, x0:x1] += 1
    if not np.all(coverage == 1):
        raise ExtractionError("Image tiles must cover every source pixel exactly once; "
                              f"missing={int(np.count_nonzero(coverage == 0))}, "
                              f"overlapping={int(np.count_nonzero(coverage > 1))}")


class Extractor:
    def __init__(self, source, page_number, config, work_dir):
        self.source, self.page_number, self.config = Path(source).resolve(), page_number, config
        self.work_dir = Path(work_dir).resolve()
        self.asset_dir = self.work_dir / "image_assets"
        self.panels = {key: list(rect(value.get("bbox") if isinstance(value, dict) else value,
                                      f"panels.{key}.bbox"))
                       for key, value in config.get("panels", {}).items()}
        self.regions = []
        for index, region in enumerate(config.get("raster_regions", [])):
            value = dict(region)
            value["bbox"] = list(rect(value.get("bbox"), f"raster_regions[{index}].bbox"))
            value["display_name"] = str(value.get("name", f"raster_region_{index+1}"))
            value["name"] = safe_name(value.get("name", f"raster_region_{index+1}"))
            value["dpi"] = float(value.get("dpi", 432))
            if not 1 <= value["dpi"] <= 2400:
                raise ExtractionError("Raster region dpi must be between 1 and 2400")
            self.regions.append(value)
        self.warnings, self.unsupported, self.statistics = [], [], Counter()
        self.filenames = set()

    def issue(self, code, message, unsupported=False, **details):
        target = self.unsupported if unsupported else self.warnings
        item = {"code": code, "message": message, **details}
        if item not in target:
            target.append(item)

    def covered_region(self, box):
        r = fitz.Rect(box)
        for region in self.regions:
            if (fitz.Rect(region["bbox"]) + (-0.001, -0.001, 0.001, 0.001)).contains(r):
                return region
        return None

    def asset_path(self, name):
        name = safe_name(name)
        candidate, suffix = name, 2
        while candidate in self.filenames:
            candidate = f"{name}_{suffix}"
            suffix += 1
        self.filenames.add(candidate)
        return candidate, self.asset_dir / (candidate + ".png")

    def extract_paths(self, page, contexts):
        records, stack, cache = [], [], {}
        for drawing in page.get_drawings(extended=True):
            level = drawing["level"]
            stack = [item for item in stack if item["level"] < level]
            if drawing["type"] in ("clip", "group"):
                stack.append(drawing)
                continue
            box = fitz.Rect(drawing["rect"])
            if contexts.get(drawing["seqno"], {}).get("mask_definition"):
                self.statistics["paths_in_mask_definitions"] += 1
                continue
            if self.covered_region(box):
                self.statistics["paths_consolidated_into_rasters"] += 1
                continue
            for clip in contexts.get(drawing["seqno"], {}).get("clips", []):
                if clip["kind"] in ("graphics_softmask", "image_mask", "text", "stroked_path"):
                    self.issue("complex_path_mask", "This native path uses a PDF mask or stroked/text clip which requires a local raster region.",
                               unsupported=True, seqno=drawing["seqno"], clip_kind=clip["kind"])
            cmd = commands(drawing["items"], drawing.get("closePath", False))
            if not cmd:
                continue
            groups = [item for item in stack if item["type"] == "group"]
            opacity = math.prod(group["opacity"] for group in groups)
            blends = [g["blendmode"] for g in groups if g.get("blendmode", "Normal") != "Normal"]
            if blends:
                self.issue("native_blend_mode", "Native PPT paths do not implement the PDF blend mode; review this object or configure a local raster region.",
                           unsupported=True, seqno=drawing["seqno"], blend_modes=blends, bbox=list(box))
            if any(g.get("knockout") for g in groups):
                self.issue("knockout_group", "PDF knockout-group compositing requires a local raster region.",
                           unsupported=True, seqno=drawing["seqno"])
            if opacity != 1:
                self.statistics["paths_with_group_opacity"] += 1
            base = {
                "kind": "path", "seqno": drawing["seqno"], "draw_suborder": 0,
                "panel": panel_for(box, self.panels), "bbox": bbox_commands(cmd),
                "fill": rgb(drawing.get("fill")),
                "fill_opacity": (drawing.get("fill_opacity") or 0) * opacity,
                "stroke": rgb(drawing.get("color")),
                "stroke_opacity": (drawing.get("stroke_opacity") or 0) * opacity,
                "line_width": drawing.get("width") or 0, "dash": drawing.get("dashes", "[] 0"),
                "line_cap": max(drawing.get("lineCap", (0, 0, 0)) or (0, 0, 0)),
                "line_join": drawing.get("lineJoin", 0), "commands": cmd,
                "even_odd": bool(drawing.get("even_odd", False)), "group_opacity": opacity,
                "source_blend_modes": blends,
            }
            clip_polygons, must_clip, contours = [], False, None
            for item in [value for value in stack if value["type"] == "clip"]:
                key = id(item)
                if key not in cache:
                    cache[key] = convex_polygon(item)
                poly = cache[key]
                if poly is None:
                    self.issue("complex_vector_clip", "Compound, empty or concave clipping paths need a local raster region; the original path is retained for review.",
                               unsupported=True, seqno=drawing["seqno"])
                    continue
                is_rectangle = len(item["items"]) == 1 and item["items"][0][0] == "re"
                if is_rectangle and (fitz.Rect(item["scissor"]) + (-0.02, -0.02, 0.02, 0.02)).contains(box):
                    continue
                if contours is None:
                    contours = flatten(cmd)
                if any(cross(a, b, p) < -0.001 for a, b in zip(poly, poly[1:]+poly[:1])
                       for contour in contours for p in contour):
                    must_clip = True
                    clip_polygons.append(poly)
            if not must_clip:
                records.append(base)
                continue
            self.statistics["clipped_paths"] += 1
            contours = contours if contours is not None else flatten(cmd)
            if base["fill"]:
                clipped = []
                for contour in contours:
                    poly = contour
                    for clip_poly in clip_polygons:
                        poly = polygon_clip(poly, clip_poly)
                    if poly:
                        clipped.extend([["M", *poly[0]], *[["L", *p] for p in poly[1:]], ["Z"]])
                if clipped:
                    records.append({**base, "commands": clipped, "bbox": bbox_commands(clipped),
                                    "stroke": None, "line_width": 0, "clipped_part": "fill"})
            if base["stroke"]:
                clipped, last = [], None
                for contour in contours:
                    for a, b in zip(contour, contour[1:]):
                        line = [a, b]
                        for clip_poly in clip_polygons:
                            line = line_clip(*line, clip_poly) if line else None
                        if line:
                            if last is None or not same(last, line[0]):
                                clipped.append(["M", *line[0]])
                            clipped.append(["L", *line[1]])
                            last = line[1]
                        else:
                            last = None
                if clipped:
                    records.append({**base, "commands": clipped, "bbox": bbox_commands(clipped),
                                    "fill": None, "clipped_part": "stroke", "draw_suborder": 1})
                    self.issue("clipped_stroke_approximation", "Clipped curves use polylines and clip stroke centerlines; inspect line caps and edge pixels.",
                               seqno=drawing["seqno"])
        if self.statistics["paths_with_group_opacity"]:
            self.issue("group_opacity_compositing", "Group opacity is multiplied into individual native objects. Overlapping members of a transparency group may need local rasterization.",
                       object_count=self.statistics["paths_with_group_opacity"])
        return records

    def extract_texts(self, page, contexts):
        traces = page.get_texttrace()
        char_index = defaultdict(list)
        for trace in traces:
            for char in trace["chars"]:
                char_index[(round(char[2][0], 3), round(char[2][1], 3), char[0])].append(trace)
        result, suborders = [], Counter()
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if not span["text"].strip():
                        continue
                    region = self.covered_region(span["bbox"])
                    if region and not region.get("remove_text", True):
                        self.statistics["texts_consolidated_into_rasters"] += 1
                        continue
                    origin = span["origin"]
                    candidates = char_index.get((round(origin[0], 3), round(origin[1], 3), ord(span["text"][0])), [])
                    if not candidates and span["text"][0].isspace():
                        candidates = [trace for trace in traces if any(chr(char[0]).isspace()
                            and math.dist(char[2], origin) < 0.05 for char in trace["chars"])]
                    if not candidates:
                        candidates = [trace for trace in traces if any(
                            char[0] == ord(span["text"][0]) and math.dist(char[2], origin) < 0.05
                            for char in trace["chars"])]
                    trace = next((t for t in candidates if t["font"] == span["font"]), candidates[0] if candidates else None)
                    seqno = trace["seqno"] if trace else len(contexts) + len(result)
                    context = contexts.get(seqno, {})
                    quad = fitz.recover_quad(line["dir"], span)
                    opacity = float(trace["opacity"] if trace else span.get("alpha", 255)/255) * group_opacity(context)
                    if trace and trace.get("type") == 3:
                        opacity = 0.0
                    item = {"kind": "text", "text": span["text"], "font": span["font"], "font_size": span["size"],
                            "flags": span["flags"], "origin": list(origin), "bbox": list(span["bbox"]),
                            "width": math.dist(tuple(quad.ul), tuple(quad.ur)), "color": "#" + f"{span['color']:06X}",
                            "dir": list(line["dir"]), "panel": panel_for(span["bbox"], self.panels),
                            "ascender": span["ascender"], "descender": span["descender"],
                            "seqno": seqno, "draw_suborder": suborders[seqno], "opacity": opacity}
                    suborders[seqno] += 1
                    result.append(item)
                    if not trace:
                        self.issue("text_paint_order_unresolved", "This text span could not be matched to a source painting operation.",
                                   unsupported=True, text=span["text"], bbox=item["bbox"])
                    elif trace.get("type") not in (0, 3):
                        self.issue("stroked_text", "Native text stores the characters and geometry; PDF stroke text effects require review.",
                                   unsupported=True, seqno=seqno, text=span["text"])
                    if any(g.get("blendmode", "Normal") != "Normal" for g in context.get("groups", [])):
                        self.issue("text_blend_mode", "A PDF text blend mode requires review or local rasterization.",
                                   unsupported=True, seqno=seqno)
                    for clip in context.get("clips", []):
                        ink_bbox = trace["bbox"] if trace else span["bbox"]
                        if clip["kind"] != "rect" or not (fitz.Rect(clip["bbox"]) + (-0.1, -0.1, 0.1, 0.1)).contains(ink_bbox):
                            self.issue("text_clipping", "Native text does not implement this PDF clipping operation.",
                                       unsupported=True, seqno=seqno)
                            break
        return result

    def extract_images(self, document, page, contexts):
        config = self.config.get("images", {})
        names, tiles = config.get("names_by_xref", {}), config.get("tiles_by_xref", {})
        shadows, configured_clips = config.get("shadows", {}), config.get("clips_by_xref", {})
        infos = page.get_image_info(xrefs=True)
        blocks = {block["number"]: block for block in page.get_text("dict")["blocks"] if block["type"] == 1}
        masks = {row[0]: row[1] for row in page.get_images(full=True)}
        used, entries, occurrences, reconstruction, excluded = set(), [], [], [], []
        for seqno, (kind, bbox) in enumerate(page.get_bboxlog()):
            if kind != "fill-image":
                continue
            candidates = [(index, info) for index, info in enumerate(infos) if index not in used
                          and max(abs(a-b) for a, b in zip(info["bbox"], bbox)) < 0.01]
            if not candidates:
                self.issue("unresolved_image_operation", "A source image painting operation could not be matched to decoded pixels.",
                           unsupported=True, seqno=seqno, bbox=list(bbox))
                continue
            index, info = candidates[0]
            used.add(index)
            xref, key = int(info.get("xref", 0)), str(info.get("xref", 0))
            if contexts.get(seqno, {}).get("mask_definition"):
                self.statistics["images_in_mask_definitions"] += 1
                continue
            if self.covered_region(bbox):
                excluded.append({"seqno": seqno, "original_xref": xref, "bbox": list(bbox)})
                continue
            block = blocks.get(info["number"])
            if not block or not block.get("image"):
                self.issue("missing_decoded_image", "The image inventory contains no decodable source pixels.",
                           unsupported=True, seqno=seqno, xref=xref)
                continue
            original = Image.open(BytesIO(block["image"])).convert("RGB")
            if original.size != (info["width"], info["height"]):
                raise ExtractionError("Decoded source image dimensions do not match the PDF inventory")
            original_name = f"original_xref_{xref}" if xref else f"original_inline_{info['number']}"
            original_path = self.asset_dir / (original_name + ".png")
            original.save(original_path)
            mask_data = block.get("mask")
            if not mask_data and masks.get(xref):
                mask_data = document.extract_image(masks[xref])["image"]
            mask = Image.open(BytesIO(mask_data)).convert("L") if mask_data else None
            if mask and mask.size != original.size:
                raise ExtractionError("Image soft mask dimensions differ from the source image")
            if mask:
                mask.save(self.asset_dir / (original_name + "_mask.png"))
            context = contexts.get(seqno, {})
            metadata = {}
            if key in shadows:
                final = effective_shadow(self.source, self.page_number-1, shadows[key], original.size)
                opacity = 1.0
                metadata.update({"effective_opacity_baked": True,
                                 "source_blend_mode": [g["blendmode"] for g in context.get("groups", []) if g["blendmode"] != "Normal"],
                                 "source_group_opacity": group_opacity(context),
                                 "source_graphics_softmask_also_applied": True,
                                 "alpha_method": "Configured PDF form evaluated at native pixel size; complete grayscale compositing encoded as black RGBA alpha."})
            else:
                final = original.convert("RGBA") if mask else original.copy()
                if mask:
                    final.putalpha(mask)
                    metadata["alpha_method"] = "Decoded original soft mask combined with original RGB as straight RGBA alpha."
                opacity = float(context.get("alpha", 1)) * group_opacity(context)
                blends = [g["blendmode"] for g in context.get("groups", []) if g["blendmode"] != "Normal"]
                if blends:
                    self.issue("image_blend_mode", "This image's PDF blend mode needs a configured form evaluation or local raster region.",
                               unsupported=True, seqno=seqno, xref=xref, blend_modes=blends)
            transform = list(info["transform"])
            axis_aligned = abs(transform[1]) < 1e-6 and abs(transform[2]) < 1e-6 and transform[0] > 0 and transform[3] > 0
            if not axis_aligned:
                self.issue("image_affine_transform", "Rotated, reflected or sheared image placement requires a local raster region; the untransformed source pixels are retained for review.",
                           unsupported=True, seqno=seqno, xref=xref, transform=transform)
            clip_spec = configured_clips.get(key, {})
            clip = fitz.Rect(bbox)
            if key not in shadows:
                for value in context.get("clips", []):
                    if value["kind"] == "rect":
                        clip &= fitz.Rect(value["bbox"])
                    elif value["kind"] == "image_mask" and mask is not None:
                        continue
                    else:
                        self.issue("complex_image_clip", "This image uses a nonrectangular or additional graphics mask; configure a form evaluation or local raster region.",
                                   unsupported=True, seqno=seqno, xref=xref, clip_kind=value["kind"])
            if clip_spec.get("bbox") is not None:
                clip &= rect(clip_spec["bbox"], f"images.clips_by_xref.{key}.bbox")
            should_clip = axis_aligned and (clip_spec.get("bbox") is not None or
                max(abs(a-b) for a, b in zip(clip, bbox)) > 0.01)
            if should_clip:
                final, pixel_clip = clipped_alpha(final, bbox, clip)
                metadata.update({"native_clip_bbox": list(clip), "native_clip_baked_into_alpha": True,
                                 "source_px_clip_float": pixel_clip})
                self.statistics["images_with_baked_clip"] += 1
            if clip_spec.get("flatten_background"):
                clipped_path = self.asset_dir / (safe_name(names.get(key, original_name)) + "_clipped_rgba_internal.png")
                final.convert("RGBA").save(clipped_path)
                final = flatten_background(final, clip_spec["flatten_background"])
                metadata.update({"flatten_background": clip_spec["flatten_background"],
                                 "clipped_rgba_original_path": str(clipped_path),
                                 "alpha_method": "Original RGB, original soft mask and exact fractional native page clip composited onto the configured background."})
            configured_tiles = tiles.get(key)
            if configured_tiles:
                validate_tiles(configured_tiles, *original.size)
            selected = configured_tiles or [{"name": names.get(key, f"image_{xref or 'inline'}_{info['number']}")}]
            occurrence_entries = []
            for order, tile in enumerate(selected):
                crop = tile.get("crop_px")
                if crop:
                    image = final.crop(crop)
                    x0, y0, x1, y1 = crop
                    w, h = original.size
                    box = [bbox[0]+x0/w*(bbox[2]-bbox[0]), bbox[1]+y0/h*(bbox[3]-bbox[1]),
                           bbox[0]+x1/w*(bbox[2]-bbox[0]), bbox[1]+y1/h*(bbox[3]-bbox[1])]
                else:
                    image, box = final, list(bbox)
                name, path = self.asset_path(tile["name"])
                image.save(path)
                record = {"kind": "image", "name": name, "path": str(path), "bbox": box,
                          "display_name": str(tile["name"]),
                          "panel": panel_for(box, self.panels), "seqno": seqno, "draw_suborder": order,
                          "original_xref": xref, "source_px_crop": crop, "source_px_size": list(original.size),
                          "pixel_size": list(image.size), "original_bbox": list(bbox),
                          "source_original_path": str(original_path), "source_softmask_xref": masks.get(xref, 0),
                          "source_image_number": info["number"], "source_transform": transform,
                          "has_alpha": image.mode == "RGBA", "placement_opacity": opacity, **metadata}
                entries.append(record)
                occurrence_entries.append(record)
            if configured_tiles:
                canvas = Image.new(final.mode, final.size)
                for entry in occurrence_entries:
                    canvas.paste(Image.open(entry["path"]), tuple(entry["source_px_crop"][:2]))
                if not np.array_equal(np.asarray(canvas), np.asarray(final)):
                    raise ExtractionError(f"Image tiles do not reconstruct source occurrence {seqno} exactly")
                source_exact = np.array_equal(np.asarray(canvas.convert("RGB")), np.asarray(original))
                reconstruction.append({"xref": xref, "seqno": seqno, "tiles": len(selected),
                                       "pixel_exact": True, "source_rgb_pixel_exact": bool(source_exact),
                                       "every_source_pixel_exactly_once": True})
            occurrences.append({"seqno": seqno, "xref": xref, "bbox": list(bbox),
                                "px_size": list(original.size), "has_source_mask": mask is not None,
                                "placement_opacity": opacity})
        entries.sort(key=lambda item: (item["seqno"], item["draw_suborder"]))
        return entries, occurrences, reconstruction, excluded

    def extract_regions(self, document, page):
        records = []
        for region in self.regions:
            box = fitz.Rect(region["bbox"])
            if not (page.rect + (-0.001, -0.001, 0.001, 0.001)).contains(box):
                raise ExtractionError(f"Raster region {region['name']} extends outside the source page")
            if region.get("remove_text", True):
                clean = fitz.open(self.source)
                raster_page = clean[self.page_number-1]
                raster_page.add_redact_annot(raster_page.rect, fill=None, cross_out=False)
                raster_page.apply_redactions(images=0, graphics=0, text=0)
            else:
                clean, raster_page = None, page
            try:
                pix = raster_page.get_pixmap(matrix=fitz.Matrix(region["dpi"]/72, region["dpi"]/72), clip=box, alpha=True)
                name, path = self.asset_path(region["name"])
                pix.save(path)
            finally:
                if clean is not None:
                    clean.close()
            if "seqno" not in region:
                operations = [seq for seq, (_, bbox) in enumerate(page.get_bboxlog()) if box.contains(fitz.Rect(bbox))]
                seqno = min(operations, default=0)
                self.issue("raster_sequence_inferred", "Raster region draw order was inferred; specify seqno when the region overlaps native content.", name=name, seqno=seqno)
            else:
                seqno = int(region["seqno"])
            records.append({"kind": "image", "name": name, "path": str(path), "bbox": list(box),
                            "display_name": region["display_name"],
                            "seqno": seqno, "draw_suborder": 0, "panel": panel_for(box, self.panels),
                            "pixel_size": [pix.width, pix.height], "placement_opacity": 1.0,
                            "has_alpha": True, "dpi": region["dpi"], "remove_text": region.get("remove_text", True)})
        return records

    def run(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with fitz.open(self.source) as document:
            if not document.is_pdf:
                raise ExtractionError("Input must be a PDF document")
            if document.needs_pass:
                raise ExtractionError("Encrypted PDFs must be unlocked before extraction")
            if not 1 <= self.page_number <= len(document):
                raise ExtractionError(f"Page {self.page_number} is outside this {len(document)}-page PDF")
            page = document[self.page_number-1]
            if page.rotation:
                self.issue("page_rotation", "PDF page rotation is normalized in memory; review the source orientation and physical dimensions.", unsupported=True, degrees=page.rotation)
                page.set_rotation(0)
            contexts = graphics_contexts(page)
            paths = self.extract_paths(page, contexts)
            texts = self.extract_texts(page, contexts)
            images, occurrences, reconstruction, excluded = self.extract_images(document, page, contexts)
            rasters = self.extract_regions(document, page)
            for seqno, (kind, bbox) in enumerate(page.get_bboxlog()):
                context = contexts.get(seqno, {})
                visible_bbox = fitz.Rect(bbox)
                for item in context.get("clips", []) + context.get("groups", []):
                    if item.get("bbox"):
                        visible_bbox &= fitz.Rect(item["bbox"])
                if (kind in ("fill-shade", "fill-imgmask") and not context.get("mask_definition")
                        and not self.covered_region(visible_bbox)):
                    self.issue("unsupported_paint_operation", "This PDF painting operation is not editable native geometry; configure a local raster region.",
                               unsupported=True, seqno=seqno, operation=kind, bbox=list(visible_bbox))
            preview_dpi = float(self.config.get("preview_dpi", 144))
            if not 36 <= preview_dpi <= 600:
                raise ExtractionError("preview_dpi must be between 36 and 600")
            page.get_pixmap(matrix=fitz.Matrix(preview_dpi/72, preview_dpi/72), alpha=False).save(self.work_dir / "source.png")
            native = {"page": {"width": page.rect.width, "height": page.rect.height,
                                "page_number": self.page_number, "source_sha256": source_hash},
                      "panels": self.panels, "paths": paths, "texts": texts, "raster_regions": rasters}
            counts = {"paths": len(paths), "text_objects": len(texts), "image_assets": len(images),
                      "raster_regions": len(rasters), "image_total": len(images)+len(rasters),
                      "clipped_paths": self.statistics["clipped_paths"], **dict(self.statistics)}
            report = {"schema_version": 1, "status": "candidate_requires_visual_review",
                      "source": {"path": str(self.source), "sha256": source_hash, "page_number": self.page_number},
                      "page_size_pt": [page.rect.width, page.rect.height], "pymupdf_version": fitz.VersionBind,
                      "counts": counts, "output_asset_count": len(images), "source_occurrences": occurrences,
                      "image_reconstruction": reconstruction, "excluded_image_operations": excluded,
                      "warnings": self.warnings, "unsupported": self.unsupported,
                      "notes": ["Paths and text are native editable candidates; images remain raster assets.",
                                "source.png is a visual reference, never a full-page slide replacement.",
                                "Only configured local regions are consolidated into raster artwork.",
                                "Fractional image clipping preserves source dimensions; source pixels and masks are retained.",
                                "Render and visually inspect the completed PowerPoint before accepting fidelity."]}
            expected = self.config.get("expected", {})
            expected_map = {"text": len(texts), "vector": len(paths), "image": len(images)+len(rasters)}
            report["expected_counts"] = {key: {"expected": value, "actual": expected_map[key], "matches": value == expected_map[key]}
                                         for key, value in expected.items() if key in expected_map}
            mismatches = [key for key, value in report["expected_counts"].items() if not value["matches"]]
            write_json(self.work_dir / "native_elements.json", native)
            write_json(self.work_dir / "image_assets.json", images)
            write_json(self.work_dir / "extraction_report.json", report)
            if mismatches:
                raise ExtractionError(f"Configured source counts changed: {', '.join(mismatches)}; inspect extraction_report.json")
        if hashlib.sha256(self.source.read_bytes()).hexdigest() != source_hash:
            raise ExtractionError("Source PDF changed during extraction")
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Source PDF, opened read-only")
    parser.add_argument("--page", type=int, default=1, help="Page number (1-based, default 1)")
    parser.add_argument("--config", type=Path, help="Optional JSON with panels, local raster regions and image selections")
    parser.add_argument("--work-dir", required=True, type=Path, help="Directory for manifests, preview and extracted image assets")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        if not isinstance(config, dict):
            raise ExtractionError("Configuration must be a JSON object")
        report = Extractor(args.input, args.page, config, args.work_dir).run()
        print(json.dumps({"status": report["status"], "counts": report["counts"],
                          "warnings": len(report["warnings"]), "unsupported": len(report["unsupported"]),
                          "report": str(args.work_dir.resolve() / "extraction_report.json")}, ensure_ascii=False))
        return 0
    except Exception as exc:
        error = {"status": "error", "error": str(exc), "error_type": type(exc).__name__}
        if args.work_dir.exists():
            write_json(args.work_dir / "extraction_error.json", error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
