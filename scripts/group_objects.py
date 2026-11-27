#!/usr/bin/env python3
"""Repair native PPTX details and group panels only when paint order stays safe."""
import argparse
import json
import math
from pathlib import Path
import sys
import zipfile
from lxml import etree as E

NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
P, A = '{' + NS['p'] + '}', '{' + NS['a'] + '}'
EMU_PER_PT = 12700
DRAWABLE = {P + 'sp', P + 'pic', P + 'cxnSp', P + 'graphicFrame', P + 'grpSp'}


def read_json(file):
    return json.loads(file.read_text(encoding='utf-8'))


def nonvisual(element):
    for tag in ('nvSpPr', 'nvPicPr', 'nvCxnSpPr', 'nvGraphicFramePr', 'nvGrpSpPr'):
        value = element.find(f'./p:{tag}/p:cNvPr', NS)
        if value is not None:
            return value
    raise ValueError('Drawable element is missing its native identity.')


def transform_bounds(element, pad_stroke=False):
    transform = element.find('./p:spPr/a:xfrm', NS)
    if transform is None:
        transform = element.find('./p:grpSpPr/a:xfrm', NS)
    if transform is None:
        return None
    origin, extent = transform.find(A + 'off'), transform.find(A + 'ext')
    if origin is None or extent is None:
        return None
    x, y = float(origin.get('x')), float(origin.get('y'))
    w, h = float(extent.get('cx')), float(extent.get('cy'))
    angle = float(transform.get('rot', '0')) / 60000 * math.pi / 180
    c, s = abs(math.cos(angle)), abs(math.sin(angle))
    rw, rh = w * c + h * s, w * s + h * c
    pad = 0
    if pad_stroke:
        line = element.find('./p:spPr/a:ln', NS)
        if line is not None and line.find(A + 'noFill') is None:
            pad = float(line.get('w', '0')) / 2
            # Acute miters and square caps can protrude beyond half a stroke.
            miter = line.find(A + 'miter')
            factor = max(1.0, float(miter.get('lim', '800000')) / 100000) if miter is not None else 1.0
            if line.get('cap') == 'sq':
                factor = max(factor, math.sqrt(2))
            pad *= factor
    return [x + w / 2 - rw / 2 - pad, y + h / 2 - rh / 2 - pad,
            x + w / 2 + rw / 2 + pad, y + h / 2 + rh / 2 + pad]


def overlaps(first, second):
    # Unknown bounds are unsafe. A 1-EMU tolerance ignores rounding-only contact.
    if first is None or second is None:
        return True
    return min(first[2], second[2]) - max(first[0], second[0]) > 1 and min(first[3], second[3]) - max(first[1], second[1]) > 1


def image_distance(element, record):
    transform = element.find('./p:spPr/a:xfrm', NS)
    if transform is None:
        return math.inf
    origin, extent = transform.find(A + 'off'), transform.find(A + 'ext')
    if origin is None or extent is None:
        return math.inf
    x, y = float(origin.get('x')) / EMU_PER_PT, float(origin.get('y')) / EMU_PER_PT
    actual = [x, y, x + float(extent.get('cx')) / EMU_PER_PT, y + float(extent.get('cy')) / EMU_PER_PT]
    return sum((a - b) ** 2 for a, b in zip(actual, record['bbox']))


def map_objects(elements, index):
    """Use stable IDs/names; never reuse metadata when image bounds coincide."""
    by_id, by_name = {}, {}
    for i, record in enumerate(index):
        if record.get('id') is not None:
            key = str(record['id'])
            if key in by_id:
                raise ValueError(f'Duplicate object_index ID: {key}')
            by_id[key] = i
        for name in (record.get('name'), record.get('pptx_name')):
            if name:
                if name in by_name and by_name[name] != i:
                    raise ValueError(f'Duplicate object_index name: {name}')
                by_name[name] = i
    used, mapping, fallbacks = set(), [], []
    for element in elements:
        cn = nonvisual(element)
        item = by_id.get(cn.get('id'))
        if item is None:
            item = by_name.get(cn.get('name'))
        if item is not None and item in used:
            raise ValueError(f'Object metadata was already consumed: {cn.get("id")}')
        image = element.tag == P + 'pic'
        if item is None and image:
            candidates = [i for i, rec in enumerate(index) if rec['kind'] == 'image' and i not in used and image_distance(element, rec) < 0.01]
            if len(candidates) == 1:
                item = candidates[0]
                fallbacks.append({'id': cn.get('id'), 'method': 'unique_bounds'})
            elif len(candidates) > 1:
                # Older exports may lack IDs in the manifest. Creation order is
                # an explicit secondary identity, and each candidate is consumed.
                item = min(candidates, key=lambda i: index[i].get('z_index', i))
                fallbacks.append({'id': cn.get('id'), 'method': 'bounds_and_creation_order', 'candidate_count': len(candidates)})
        if item is None:
            raise ValueError(f'Cannot map native object {cn.get("id")} ({cn.get("name")!r}) to object_index.json.')
        record = index[item]
        if image != (record['kind'] == 'image'):
            raise ValueError(f'Native object kind differs from metadata for ID {cn.get("id")}.')
        if image and image_distance(element, record) >= 0.01:
            raise ValueError(f'Native image placement differs from metadata for {record["name"]}.')
        used.add(item)
        mapping.append(record)
    if len(used) != len(index):
        missing = [rec['name'] for i, rec in enumerate(index) if i not in used]
        raise ValueError(f'Objects missing from the PPTX: {missing[:10]}')
    return mapping, fallbacks


def repair_native_details(element, record):
    cn = nonvisual(element)
    if record['kind'] == 'image':
        description = str(record.get('description', record['name']))
        cn.set('name', record.get('pptx_name', record['name'] + '_' + description))
        cn.set('descr', description)
        alpha = float(record.get('placement_opacity', 1))
        if not math.isfinite(alpha) or not 0 <= alpha <= 1:
            raise ValueError(f'Invalid placement_opacity for {record["name"]}.')
        blip = element.find('./p:blipFill/a:blip', NS)
        if blip is None:
            raise ValueError(f'Image {record["name"]} is missing its blip.')
        for old in list(blip.findall(A + 'alphaModFix')):
            blip.remove(old)
        if alpha != 1:
            E.SubElement(blip, A + 'alphaModFix', amt=str(round(alpha * 100000)))
    if record['kind'] == 'text':
        alpha = float(record.get('text_opacity', 1))
        if not math.isfinite(alpha) or not 0 <= alpha <= 1:
            raise ValueError(f'Invalid text_opacity for {record["name"]}.')
        if alpha != 1:
            body = element.find('p:txBody', NS)
            fills = body.findall('.//a:solidFill', NS) if body is not None else []
            if not fills or any(len(fill) != 1 for fill in fills):
                raise ValueError(f'Cannot apply text opacity to {record["name"]}: explicit text colors are missing.')
            for fill in fills:
                # Apply to runs and every inherited paragraph/list text style.
                # Retain a:t and all native text geometry, including hidden OCR.
                color = fill[0]
                for old in list(color.findall(A + 'alpha')):
                    color.remove(old)
                E.SubElement(color, A + 'alpha', val=str(round(alpha * 100000)))
    for lock in element.findall('.//a:spLocks', NS) + element.findall('.//a:picLocks', NS):
        for attr in ('noGrp', 'noSelect', 'noTextEdit', 'noMove', 'noResize'):
            lock.attrib.pop(attr, None)
    if record['kind'] == 'vector':
        line = element.find('./p:spPr/a:ln', NS)
        if line is not None:
            cap = record.get('line_cap', 0)
            if isinstance(cap, (list, tuple)):
                cap = max(cap, default=0)
            line.set('cap', {0: 'flat', 1: 'rnd', 2: 'sq'}.get(int(cap or 0), 'flat'))
            for tag in ('round', 'bevel', 'miter'):
                for old in list(line.findall(A + tag)):
                    line.remove(old)
            join = {0: 'miter', 1: 'round', 2: 'bevel'}.get(int(record.get('line_join') or 0), 'miter')
            node = E.SubElement(line, A + join)
            if join == 'miter':
                node.set('lim', '800000')


def grouped_order(records, candidates):
    members = {panel: [] for panel in candidates}
    for i, record in enumerate(records):
        if record.get('panel') in members:
            members[record['panel']].append(i)
    order, seen = [], set()
    for i, record in enumerate(records):
        panel = record.get('panel')
        if panel in candidates:
            if panel not in seen:
                order.extend(members[panel])
                seen.add(panel)
        else:
            order.append(i)
    return order, members


def safe_groups(elements, records, candidates):
    """Reject groups that would invert any overlapping object pair."""
    candidates = set(candidates)
    skipped, examples = {}, []
    bounds = [transform_bounds(el, pad_stroke=True) for el in elements]
    while True:
        order, members = grouped_order(records, candidates)
        positions = {original: now for now, original in enumerate(order)}
        risky = set()
        for first in range(len(records)):
            for second in range(first + 1, len(records)):
                if positions[first] <= positions[second] or not overlaps(bounds[first], bounds[second]):
                    continue
                panels = {records[first].get('panel'), records[second].get('panel')} & candidates
                risky.update(panels)
                if len(examples) < 30:
                    examples.append({'before': records[first]['name'], 'after': records[second]['name'], 'panels': sorted(panels)})
        if not risky:
            return candidates, members, skipped, examples
        for panel in risky:
            skipped[panel] = 'Grouping interleaved objects would change the paint order of overlapping object bounds.'
        candidates.difference_update(risky)


def make_group(elements, name, native_id):
    group = E.Element(P + 'grpSp')
    nv = E.SubElement(group, P + 'nvGrpSpPr')
    E.SubElement(nv, P + 'cNvPr', id=str(native_id), name=name)
    E.SubElement(nv, P + 'cNvGrpSpPr')
    E.SubElement(nv, P + 'nvPr')
    boxes = [transform_bounds(el) for el in elements]
    if any(b is None for b in boxes):
        raise ValueError(f'Cannot group {name}: an object has no supported transform.')
    x0 = math.floor(min(b[0] for b in boxes)); y0 = math.floor(min(b[1] for b in boxes))
    x1 = math.ceil(max(b[2] for b in boxes)); y1 = math.ceil(max(b[3] for b in boxes))
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)
    transform = E.SubElement(E.SubElement(group, P + 'grpSpPr'), A + 'xfrm')
    for tag, attrs in (('off', {'x': x0, 'y': y0}), ('ext', {'cx': w, 'cy': h}),
                       ('chOff', {'x': x0, 'y': y0}), ('chExt', {'cx': w, 'cy': h})):
        E.SubElement(transform, A + tag, **{key: str(value) for key, value in attrs.items()})
    for element in elements:
        group.append(element)
    return group


def run(work_dir, config):
    raw, output = work_dir / 'candidate_raw.pptx', work_dir / 'candidate_grouped.pptx'
    index = read_json(work_dir / 'object_index.json')
    native_file = work_dir / 'native_elements.json'
    native = read_json(native_file) if native_file.exists() else {}
    panels = {**native.get('panels', {}), **config.get('panels', {})}
    names = {str(panel): (value.get('name', str(panel)) if isinstance(value, dict) else str(panel)) for panel, value in panels.items()}
    names.update({str(k): str(v) for k, v in config.get('group_names', {}).items()})
    warnings = []
    with zipfile.ZipFile(raw) as source:
        parser = E.XMLParser(resolve_entities=False, no_network=True)
        root = E.fromstring(source.read('ppt/slides/slide1.xml'), parser)
        tree = root.find('p:cSld/p:spTree', NS)
        if tree is None:
            raise ValueError('The PPTX has no slide1 shape tree.')
        children = list(tree)
        elements = [el for el in children if el.tag in DRAWABLE]
        records, fallbacks = map_objects(elements, index)
        for element, record in zip(elements, records):
            repair_native_details(element, record)
        candidates = {rec.get('panel') for rec in records if rec.get('panel') in names and rec.get('panel') != 'page'}
        if config.get('grouping', {}).get('enabled', True) is False:
            candidates = set()
        candidates, members, skipped, conflicts = safe_groups(elements, records, candidates)
        if skipped:
            warnings.append('Some panels remain ungrouped to preserve overlapping objects\' original paint order. See skipped_panels and overlap_conflicts.')
        if fallbacks:
            warnings.append('Some images used fallback identity matching; see image_matching_fallbacks. Each manifest record was consumed exactly once.')
        max_id = max((int(n.get('id')) for n in root.findall('.//p:cNvPr', NS)), default=0)
        groups, emitted = {}, set()
        element_index = {id(el): i for i, el in enumerate(elements)}
        for child in children:
            tree.remove(child)
        for child in children:
            i = element_index.get(id(child))
            panel = records[i].get('panel') if i is not None else None
            if panel in candidates:
                if panel not in emitted:
                    max_id += 1
                    panel_elements = [elements[j] for j in members[panel]]
                    tree.append(make_group(panel_elements, names[panel], max_id))
                    groups[panel] = len(panel_elements)
                    emitted.add(panel)
            else:
                tree.append(child)
        temporary = output.with_suffix('.tmp.pptx')
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                content = E.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True) if info.filename == 'ppt/slides/slide1.xml' else source.read(info.filename)
                target.writestr(info, content)
    temporary.replace(output)
    grouped_count = sum(groups.values())
    report = {'groups': groups, 'group_names': {panel: names[panel] for panel in groups},
              'ungrouped_page_objects': sum(rec.get('panel') == 'page' for rec in records),
              'ungrouped_objects': len(records) - grouped_count, 'objects': len(records), 'file': str(output),
              'z_order_safe': True, 'grouping_strategy': 'first_member_anchor_with_overlap_inversion_check',
              'skipped_panels': skipped, 'overlap_conflicts': conflicts, 'image_matching_fallbacks': fallbacks,
              'warnings': warnings,
              'bounds_limitation': 'Overlap checks use per-object rotated axis-aligned bounds with a conservative stroke cap/join allowance; this can leave visually separable objects ungrouped.'}
    (work_dir / 'grouping_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for warning in warnings:
        print('WARNING: ' + warning, file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    try:
        run(args.work_dir.resolve(), read_json(args.config) if args.config else {})
    except (OSError, ValueError, KeyError, E.XMLSyntaxError, zipfile.BadZipFile) as error:
        parser.exit(1, f'group_objects: {error}\n')


if __name__ == '__main__':
    main()
