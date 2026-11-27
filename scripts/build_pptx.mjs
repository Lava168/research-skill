#!/usr/bin/env node
/** Build a source-sized, editable candidate from extract_pdf.py's manifests. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const PT_TO_PX = 4 / 3;
const PT_TO_EMU = 12700;
const NONE = {style: 'solid', fill: 'none', width: 0};

function argumentsFor(argv) {
  const values = {};
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === '--help' || key === '-h') return {help: true};
    if (!['--work-dir', '--config'].includes(key) || !argv[i + 1]) {
      throw new Error(`Unknown or incomplete argument: ${key}`);
    }
    values[key.slice(2)] = argv[++i];
  }
  if (!values['work-dir']) throw new Error('--work-dir DIR is required.');
  return values;
}

/** Resolve through the active runtime, then ordinary Node package resolution. */
export function resolveRuntimePackage(name) {
  const roots = [];
  if (process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES) {
    roots.push(createRequire(path.join(path.resolve(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES), '__pdf_ppt_resolver__.cjs')));
  }
  roots.push(createRequire(import.meta.url));
  roots.push(createRequire(path.join(process.cwd(), '__pdf_ppt_resolver__.cjs')));
  const failures = [];
  for (const resolver of roots) {
    try { return resolver.resolve(name); } catch (error) { failures.push(error.code ?? error.message); }
  }
  throw new Error(`Required package ${name} is unavailable (${[...new Set(failures)].join(', ')}). ` +
    'Provide an installed runtime through CODEX_PRIMARY_RUNTIME_NODE_MODULES or normal Node resolution; this script does not install packages.');
}

function finite(value, label, fallback) {
  const n = value == null && fallback !== undefined ? fallback : Number(value);
  if (!Number.isFinite(n)) throw new Error(`${label} must be finite.`);
  return n;
}

function box(value, label) {
  if (!Array.isArray(value) || value.length !== 4) throw new Error(`${label} must contain four PDF point coordinates.`);
  const b = value.map((v, i) => finite(v, `${label}[${i}]`));
  if (b[2] < b[0] || b[3] < b[1]) throw new Error(`${label} has reversed coordinates.`);
  return b;
}

function opacity(value, label) {
  const n = finite(value, label, 1);
  if (n < 0 || n > 1) throw new Error(`${label} must be between 0 and 1.`);
  return n;
}

function solid(color, alpha = 1) {
  if (!color || alpha <= 0) return 'none';
  if (!/^#[0-9a-f]{6}$/i.test(color)) throw new Error(`Unsupported color ${JSON.stringify(color)}; expected #RRGGBB.`);
  return {type: 'solid', color: {type: 'rgb', value: color.slice(1), transform: {opacity: alpha}}};
}

function dashStyle(dash) {
  if (!dash || dash === '[] 0') return 'solid';
  const numbers = (String(dash).match(/[\d.]+/g) || []).map(Number);
  return numbers.length && numbers[0] < 1 ? 'dotted' : 'dashed';
}

function pathCommands(rec, b) {
  if (!Array.isArray(rec.commands) || !rec.commands.length) throw new Error('A native path has no commands.');
  return rec.commands.map(command => {
    const x = i => (finite(command[i], 'path x') - b[0]) * PT_TO_PX;
    const y = i => (finite(command[i], 'path y') - b[1]) * PT_TO_PX;
    switch (command[0]) {
      case 'M': return {moveTo: {x: x(1), y: y(2)}};
      case 'L': return {lineTo: {x: x(1), y: y(2)}};
      case 'Q': return {quadBezTo: {x1: x(1), y1: y(2), x: x(3), y: y(4)}};
      case 'C': return {cubicBezTo: {x1: x(1), y1: y(2), x2: x(3), y2: y(4), x: x(5), y: y(6)}};
      case 'Z': case 'h': return {close: {}};
      default: throw new Error(`Unsupported path command: ${command[0]}`);
    }
  });
}

function compileRegex(pattern, label, flags = '') {
  try { return new RegExp(pattern, flags); } catch (error) { throw new Error(`${label}: ${error.message}`); }
}

function matches(regex, value) {
  regex.lastIndex = 0;
  return regex.test(value);
}

function prepareFonts(GlobalFonts, requested, config, configDir, warn) {
  const registered = new Set();
  const records = [];
  const files = config.text?.font_files ?? config.font_files ?? [];
  if (!Array.isArray(files)) throw new Error('text.font_files must be an array of paths or {path, family} records.');
  for (const item of files) {
    const entry = typeof item === 'string' ? {path: item} : item;
    if (!entry?.path) throw new Error('Every font_files entry needs a path.');
    const fontPath = path.resolve(configDir, entry.path);
    if (!GlobalFonts.registerFromPath(fontPath, entry.family)) throw new Error(`Could not register configured font: ${fontPath}`);
    if (entry.family) registered.add(entry.family);
    records.push({family: entry.family ?? null, path: fontPath, source: 'configured'});
  }
  for (const family of requested) {
    if (registered.has(family)) continue;
    const matchesForFamily = [];
    for (const style of ['Regular', 'Bold', 'Italic', 'Bold Italic']) {
      const result = spawnSync('fc-match', ['-f', '%{family}\t%{file}\n', `${family}:style=${style}`], {encoding: 'utf8'});
      if (result.status !== 0 || !result.stdout.trim()) break;
      const [actualFamily, fontPath] = result.stdout.trim().split('\n')[0].split('\t');
      if (!fontPath || !GlobalFonts.registerFromPath(fontPath, family)) continue;
      matchesForFamily.push({family, actual_family: actualFamily, style, path: fontPath, source: 'fontconfig'});
    }
    if (matchesForFamily.length) {
      records.push(...matchesForFamily);
      const actual = matchesForFamily[0].actual_family;
      if (!actual.split(',').some(name => name.trim().toLocaleLowerCase() === family.toLocaleLowerCase())) {
        warn(`Font fallback: ${JSON.stringify(family)} is measured using ${JSON.stringify(actual)}. ` +
          `The PPTX retains ${JSON.stringify(family)}; use text.font_files for an exact font.`);
      }
    } else if (GlobalFonts.has(family)) {
      records.push({family, source: 'system'});
    } else {
      const available = GlobalFonts.families;
      if (!available.length) throw new Error(`No usable font found for ${family}. Configure text.font_files.`);
      warn(`Font fallback: ${JSON.stringify(family)} is unavailable and fontconfig could not resolve it. ` +
        'Canvas will use its system fallback; configure text.font_files before accepting text geometry.');
      records.push({family, source: 'unresolved-system-fallback'});
    }
  }
  return records;
}

async function build(workDir, configFile) {
  const config = configFile ? JSON.parse(await fs.readFile(configFile, 'utf8')) : {};
  const configDir = configFile ? path.dirname(configFile) : workDir;
  const data = JSON.parse(await fs.readFile(path.join(workDir, 'native_elements.json'), 'utf8'));
  const imageData = JSON.parse(await fs.readFile(path.join(workDir, 'image_assets.json'), 'utf8'));
  const images = Array.isArray(imageData) ? imageData : (imageData.assets ?? imageData.images);
  if (!Array.isArray(images)) throw new Error('image_assets.json must contain an array, assets array, or images array.');
  if (!Array.isArray(data.paths) || !Array.isArray(data.texts)) throw new Error('native_elements.json must contain paths and texts arrays.');
  const widthPt = finite(data.page?.width, 'page.width');
  const heightPt = finite(data.page?.height, 'page.height');
  if (widthPt <= 0 || heightPt <= 0) throw new Error('PDF page dimensions must be positive.');
  const width = widthPt * PT_TO_PX, height = heightPt * PT_TO_PX;
  const warnings = [];
  const warn = message => { if (!warnings.includes(message)) { warnings.push(message); console.error(`WARNING: ${message}`); } };
  const artifactPath = resolveRuntimePackage('@oai/artifact-tool');
  const canvasPath = resolveRuntimePackage('@napi-rs/canvas');
  const {Presentation, PresentationFile} = await import(pathToFileURL(artifactPath).href);
  const canvas = await import(pathToFileURL(canvasPath).href);
  const {createCanvas, GlobalFonts} = canvas.default ?? canvas;
  const presentation = Presentation.create({slideSize: {width, height}});
  const slide = presentation.slides.add();
  slide.background.fill = config.background ?? '#FFFFFF';
  const panelBoxes = {};
  for (const [id, panel] of Object.entries({...data.panels, ...config.panels})) {
    const rect = Array.isArray(panel) ? panel : panel.bbox;
    if (rect) panelBoxes[id] = box(rect, `panels.${id}.bbox`);
  }
  const panelFor = rec => {
    if (rec.panel != null) return String(rec.panel);
    const b = box(rec.bbox, 'object.bbox'), x = (b[0] + b[2]) / 2, y = (b[1] + b[3]) / 2;
    for (const [id, r] of Object.entries(panelBoxes)) if (x >= r[0] && x <= r[2] && y >= r[1] && y <= r[3]) return id;
    return 'page';
  };
  const fontMap = (config.text?.font_map ?? []).map((entry, i) => {
    if (!entry.family) throw new Error(`text.font_map[${i}].family is required.`);
    return {...entry, matcher: compileRegex(entry.pattern ?? '.*', `text.font_map[${i}].pattern`, entry.flags ?? '')};
  });
  const textFamily = rec => fontMap.find(entry => matches(entry.matcher, rec.font ?? ''))?.family ?? config.text?.default_font ?? 'Arial';
  const fontReport = prepareFonts(GlobalFonts, new Set(data.texts.map(textFamily)), config, configDir, warn);
  const ctx = createCanvas(10, 10).getContext('2d');
  const baselineFactor = finite(config.text?.baseline_factor, 'text.baseline_factor', 0.994);
  const adjustments = (config.text?.adjustments ?? []).map((entry, i) => ({
    ...entry, matcher: compileRegex(entry.regex ?? '.*', `text.adjustments[${i}].regex`, entry.flags ?? ''),
    dx: finite(entry.dx_pt, `text.adjustments[${i}].dx_pt`, 0), dy: finite(entry.dy_pt, `text.adjustments[${i}].dy_pt`, 0),
  }));
  const index = [];
  let drawIndex = 0, textIndex = 0;
  function addText(rec) {
    const b = box(rec.bbox, 'text.bbox'), panel = panelFor(rec);
    const bold = /Bold|Blk|Extrabold/i.test(rec.font ?? '') || Boolean(rec.flags & 16);
    const italic = /(?:-It$|Italic|Oblique)/i.test(rec.font ?? '') || Boolean(rec.flags & 2);
    const family = textFamily(rec), size = finite(rec.font_size, 'text.font_size') * PT_TO_PX;
    if (size <= 0) throw new Error('text.font_size must be positive.');
    const text = String(rec.text ?? '');
    ctx.font = `${italic ? 'italic ' : ''}${bold ? 'bold ' : ''}${size}px ${JSON.stringify(family)}`;
    const measured = ctx.measureText(text).width, count = Array.from(text).length;
    const target = finite(rec.width, 'text.width', b[2] - b[0]) * PT_TO_PX;
    const spacing = count > 1 ? Math.round((target - measured) / count * 0.75 * 100) : 0;
    const w = Math.max(target + size * 0.2, size * 0.15), h = size * 1.45;
    const direction = rec.dir ?? [1, 0];
    const angle = Math.atan2(finite(direction[1], 'text.dir[1]'), finite(direction[0], 'text.dir[0]'));
    const c = Math.cos(angle), s = Math.sin(angle), baseline = size * baselineFactor;
    const ux = -w / 2, uy = baseline - h / 2;
    const delta = {dx: 0, dy: 0};
    for (const adjust of adjustments) {
      if ((adjust.panel == null || String(adjust.panel) === panel) && matches(adjust.matcher, text)) {
        delta.dx += adjust.dx; delta.dy += adjust.dy;
      }
    }
    const cx = (finite(rec.origin?.[0], 'text.origin[0]') + delta.dx) * PT_TO_PX - (ux * c - uy * s);
    const cy = (finite(rec.origin?.[1], 'text.origin[1]') + delta.dy) * PT_TO_PX - (ux * s + uy * c);
    const name = rec.name ?? `${panel}__text_${String(++textIndex).padStart(3, '0')}_${text.slice(0, 42)}`;
    const shape = slide.shapes.add({name, geometry: 'textbox', position: {left: cx - w / 2, top: cy - h / 2, width: w, height: h, rotation: angle * 180 / Math.PI}, fill: 'none', line: NONE});
    shape.text = [[{run: text, style: {typeface: family, fontSize: `${size}px`, bold, italic, characterSpacing: spacing, color: rec.color ?? '#000000'}}]];
    shape.text.style = {typeface: family, fontSize: size, bold, italic, color: rec.color ?? '#000000', wrap: 'none', autoFit: 'none', insets: 0, alignment: 'left', verticalAlignment: 'top', lineSpacing: 1};
    index.push({id: shape.id, name, panel, kind: 'text', text, bbox: b, baseline: rec.origin, rotation: angle * 180 / Math.PI, size_pt: rec.font_size, font: family, source_font: rec.font, text_adjustment_pt: delta, text_opacity: opacity(rec.opacity, `text ${name} opacity`), seqno: rec.seqno, draw_suborder: rec.draw_suborder, z_index: index.length});
  }
  async function addGraphic(rec) {
    const b = box(rec.bbox, 'graphic.bbox'), panel = panelFor(rec);
    const kind = rec.kind === 'image' ? 'image' : 'path';
    const name = kind === 'image' ? `${panel}__image_${String(++drawIndex).padStart(4, '0')}` : (rec.name ?? `${panel}__${kind}_${String(++drawIndex).padStart(4, '0')}`);
    if (kind === 'image') {
      if (b[2] <= b[0] || b[3] <= b[1]) throw new Error(`Image ${rec.name ?? name} has no area.`);
      if (!rec.path) throw new Error(`Image ${rec.name ?? name} has no path.`);
      const bytes = await fs.readFile(path.resolve(workDir, rec.path));
      const description = String(rec.name ?? name);
      const image = slide.images.add({blob: bytes, contentType: rec.content_type ?? 'image/png', alt: description, fit: 'contain', position: {left: b[0] * PT_TO_PX, top: b[1] * PT_TO_PX, width: (b[2] - b[0]) * PT_TO_PX, height: (b[3] - b[1]) * PT_TO_PX}});
      index.push({id: image.id, name, pptx_name: `${name}_${description}`, panel, kind: 'image', description, bbox: b, placement_opacity: opacity(rec.placement_opacity, `image ${description} placement_opacity`), seqno: rec.seqno, draw_suborder: rec.draw_suborder, z_index: index.length});
    } else {
      const w = Math.max((b[2] - b[0]) * PT_TO_PX, 0.001), h = Math.max((b[3] - b[1]) * PT_TO_PX, 0.001);
      const shape = slide.shapes.add({name, geometry: 'custom', position: {left: b[0] * PT_TO_PX, top: b[1] * PT_TO_PX, width: w, height: h}, fill: solid(rec.fill, opacity(rec.fill_opacity, 'fill_opacity')), line: rec.stroke ? {style: dashStyle(rec.dash), fill: solid(rec.stroke, opacity(rec.stroke_opacity, 'stroke_opacity')), width: finite(rec.line_width, 'line_width', 0) * PT_TO_PX} : NONE, customPaths: [{width: w, height: h, commands: pathCommands(rec, b)}]});
      index.push({id: shape.id, name, panel, kind: 'vector', bbox: b, line_cap: rec.line_cap, line_join: rec.line_join, line_width: rec.line_width, dash: rec.dash, even_odd: rec.even_odd, blendmode: rec.blendmode, seqno: rec.seqno, draw_suborder: rec.draw_suborder, z_index: index.length});
    }
  }
  const regions = data.raster_regions ?? (data.acquisition ? [data.acquisition] : []);
  if (!Array.isArray(regions)) throw new Error('raster_regions must be an array.');
  const queue = [...data.paths, ...images.map(rec => ({...rec, kind: 'image'})), ...regions.map(rec => ({...rec, kind: 'image'}))];
  const layering = config.text?.layering ?? 'above_artwork';
  if (!['above_artwork', 'source'].includes(layering)) throw new Error('text.layering must be above_artwork or source.');
  if (layering === 'source') {
    if (data.texts.some(rec => !Number.isFinite(rec.seqno))) throw new Error('text.layering=source requires seqno on every text span.');
    queue.push(...data.texts.map(rec => ({...rec, kind: 'text'})));
  }
  queue.sort((a, b) => finite(a.seqno, 'seqno', 0) - finite(b.seqno, 'seqno', 0) || finite(a.draw_suborder, 'draw_suborder', 0) - finite(b.draw_suborder, 'draw_suborder', 0));
  for (const rec of queue) rec.kind === 'text' ? addText(rec) : await addGraphic(rec);
  if (layering === 'above_artwork') for (const rec of data.texts) addText(rec);
  const names = index.map(rec => rec.name);
  if (new Set(names).size !== names.length) throw new Error('Object names are not unique. Supply unique source object names.');
  const sourceHash = data.page.source_sha256 ?? config.source?.sha256;
  slide.speakerNotes.text = `Reconstructed from PDF page ${data.page.page_number ?? 1}.${sourceHash ? ` Source SHA-256: ${sourceHash}.` : ''} ` +
    'Text and supported paths remain editable; embedded images remain raster images. Candidate requires rendered visual review.';
  const dimensions = {width, height, width_pt: widthPt, height_pt: heightPt, expectedEmu: `${Math.round(widthPt * PT_TO_EMU)},${Math.round(heightPt * PT_TO_EMU)}`};
  const counts = Object.fromEntries(['text', 'vector', 'image'].map(kind => [kind, index.filter(rec => rec.kind === kind).length]));
  const report = {slides: 1, width, height, objects: index.length, ...counts, text_layering: layering, fonts: fontReport, warnings,
    pending_ooxml_postprocessing: ['image names and alternate text', 'image placement opacity', 'text opacity', 'stroke caps and joins', 'safe panel grouping']};
  const temporary = path.join(workDir, 'candidate_raw.tmp.pptx');
  await (await PresentationFile.exportPptx(presentation)).save(temporary);
  await fs.rename(temporary, path.join(workDir, 'candidate_raw.pptx'));
  await fs.writeFile(path.join(workDir, 'object_index.json'), JSON.stringify(index, null, 2) + '\n');
  await fs.writeFile(path.join(workDir, 'deck_dimensions.json'), JSON.stringify(dimensions, null, 2) + '\n');
  await fs.writeFile(path.join(workDir, 'build_report.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({slides: 1, width, height, objects: index.length, ...counts, text_layering: layering, warnings}));
}

async function main() {
  const args = argumentsFor(process.argv.slice(2));
  if (args.help) { console.log('Usage: node build_pptx.mjs --work-dir DIR [--config JSON]'); return; }
  await build(path.resolve(args['work-dir']), args.config ? path.resolve(args.config) : undefined);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(`build_pptx: ${error.message}`); process.exitCode = 1; });
}
