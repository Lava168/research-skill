import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {pathToFileURL} from 'node:url';

function argsOf(argv) {
  const out={};
  for(let i=0;i<argv.length;i++) {
    if(argv[i]==='--help') { console.log('node finalize_pptx.mjs --work-dir DIR --output FILE [--presentations-skill-root DIR]');process.exit(0); }
    if(!argv[i].startsWith('--')||i+1>=argv.length) throw new Error(`Invalid argument: ${argv[i]}`);
    out[argv[i].slice(2)]=argv[++i];
  }
  return out;
}
function commonParent(a,b) {
  let current=path.resolve(a);
  const target=path.resolve(b);
  while(target!==current&&!target.startsWith(current+path.sep)) {
    const parent=path.dirname(current);
    if(parent===current) return current;
    current=parent;
  }
  return current;
}
const argv=argsOf(process.argv.slice(2));
if(!argv['work-dir']||!argv.output) throw new Error('--work-dir and --output are required');
const work=path.resolve(argv['work-dir']),finalPath=path.resolve(argv.output);
const skill=argv['presentations-skill-root']??process.env.PDF_PPT_PRESENTATIONS_SKILL_ROOT??path.join(process.env.CODEX_HOME??path.join(os.homedir(),'.codex'),'skills','builtins','presentations');
const utils=path.join(skill,'container_tools','artifact_tool_utils.mjs');
try { await fs.access(utils); } catch { throw new Error('Presentations finalizer was not found. Set PDF_PPT_PRESENTATIONS_SKILL_ROOT to an installed Presentations skill root. The package does not bundle runtime-owned tools.'); }
const {finalizePresentation}=await import(pathToFileURL(utils).href);
const dim=JSON.parse(await fs.readFile(path.join(work,'deck_dimensions.json'),'utf8'));
const index=JSON.parse(await fs.readFile(path.join(work,'object_index.json'),'utf8'));
const families=[...new Set(index.filter(x=>x.kind==='text').map(x=>x.font).filter(Boolean))];
if(!families.length) families.push('Arial');
const result=await finalizePresentation({
  workspaceDir:commonParent(work,path.dirname(finalPath)),
  candidatePath:path.join(work,'candidate_grouped.pptx'),finalPath,
  pythonExecutable:process.env.CODEX_PRIMARY_RUNTIME_PYTHON??process.env.PDF_PPT_PYTHON??'python3',
  integrityValidatorPath:path.join(skill,'container_tools','inspect_presentation_package_integrity.py'),
  layoutValidatorPath:path.join(skill,'container_tools','inspect_presentation_layout_geometry.py'),
  layoutArgs:['--expected-slide-size-emu',dim.expectedEmu,'--validate-heading-fit','--approved-dense-slide','1','--approved-icon-slide','1'],
  explicitTotalSlideCount:1,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],
  fontPolicy:{basis:'design',families},verifyArtifactToolImport:true,
  receiptPath:path.join(work,'finalization.json'),
});
console.log(JSON.stringify({finalPath:result.finalPath,byteCount:result.byteCount,sha256:result.finalSha256,structuralStatus:result.packageIntegrity.status,artifactImportPassed:result.firstPartyImport.passed}));
