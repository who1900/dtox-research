import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { presentation, demo, corpus } from './story.mjs';

const src = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(src, '../..');
const out = path.join(root, 'presentation/video-2026-10-02');
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/User/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const sharp = require(path.join(root, '.deck-build/node_modules/sharp'));
sharp.concurrency(2);
const ffmpeg = 'C:/ffmpeg/bin/ffmpeg.exe';
const ffprobe = 'C:/ffmpeg/bin/ffprobe.exe';
const fps = 24;
let fontCss = '';
let brandFonts = false;
const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sha = s => createHash('sha256').update(s).digest('hex');
const json = o => JSON.stringify(o, null, 2);
const mono = t => `<pre>${escape(t)}</pre>`;
const rows = pairs => `<div class="rows">${pairs.map(([k,v]) => `<div><span>${escape(k)}</span><strong>${escape(v)}</strong></div>`).join('')}</div>`;
const viewer = (body, title = 'MCP RESPONSE VIEWER / SELECTED DATA') => `<section class="viewer"><div class="viewer-head">${escape(title)}</div><div class="viewer-body">${body}</div></section>`;

function run(exe, args, timeout = 120000) {
  return new Promise((resolve, reject) => {
    const child = spawn(exe, args, { cwd: root, windowsHide: true, env: {...process.env, OMP_NUM_THREADS:'2', OPENBLAS_NUM_THREADS:'2'} });
    let output = ''; child.stdout.on('data', b => { output += b; }); child.stderr.on('data', b => { output += b; });
    const timer = setTimeout(() => { child.kill(); reject(new Error(`Timed out: ${exe}`)); }, timeout);
    child.on('error', e => { clearTimeout(timer); reject(e); });
    child.on('close', code => { clearTimeout(timer); code === 0 ? resolve(output) : reject(new Error(`${path.basename(exe)} exited ${code}: ${output.slice(-6000)}`)); });
  });
}
async function probe(file) { return JSON.parse(await run(ffprobe, ['-v','error','-show_format','-show_streams','-of','json',file])); }

async function evidence() {
  const bytes = await fs.readFile(path.join(root, 'docs/data/2026-10-02-web3-demo.json'));
  const capture = JSON.parse(bytes);
  const decoded = name => {
    const request = capture.requests.find(r => r.tool === name);
    if (!request || request.response.isError) throw new Error(`Missing successful capture: ${name}`);
    return { tool: request.tool, params: request.params, result: JSON.parse(request.response.content.find(c => c.type === 'text').text) };
  };
  const find = decoded('find_papers'), math = decoded('get_code_or_math_spec'), warning = decoded('read_paper_section');
  const solanaResponse = JSON.parse(await fs.readFile(path.join(src,'solana-response.json'),'utf8'));
  const solana = JSON.parse(solanaResponse.content[0].text).papers[0];
  const paper = find.result.papers[0];
  const equation = math.result.sections.find(s => s.text.includes('eq:p2p-payoff'));
  if (paper.id !== '2101.05511' || paper.relevance_rank !== 1 || paper.found_via !== 'title match' || !paper.fulltext || !equation) throw new Error('Discovery or equation changed; review storyboard.');
  if (math.result.returned !== 7 || math.result.available !== 7 || math.result.chars !== 2211 || math.result.truncated) throw new Error('Math response changed; review storyboard.');
  if (math.result.sections.filter(s => s.element_type === 'equation').length !== 6 || math.result.sections.filter(s => s.element_type === 'algorithm').length !== 1) throw new Error('Object types changed.');
  if (warning.params.max_chars !== 3500 || warning.result.total_chars !== 1205 || warning.result.evidence.exact_latex !== false || warning.result.license.spdx !== 'CC0-1.0') throw new Error('Specification response changed.');
  const canonical = '\\begin{equation}\\label{eq:p2p-payoff}\n    \\mathbb{E}\\left[u_i^{\\text{PA}}\\mid\\alpha\\right]=(1-\\alpha)\\operatorname{Pr}^\\text{PA}(\\mathcal{O}, \\mathcal{S}_i)\\mathcal{R}_i(\\mathcal{O})-b_i^{\\text{PA}}(\\mathcal{O}, \\mathcal{S}_i)\n\\end{equation}';
  if (equation.text !== canonical) throw new Error('Pretty equation requires a reviewed canonical source match.');
  const submission = await fs.readFile(path.join(root, 'SUBMISSION.md'), 'utf8');
  const historical = [...submission.matchAll(/https:\/\/explorer\.solana\.com\/(?:address|tx)\/[^)\s]+/g)].map(m => m[0]);
  if (historical.length < 3) throw new Error('Historical links missing.');
  const firstSentence = warning.result.text.slice(0, warning.result.text.indexOf('. In total') + 1);
  const riskStart = warning.result.text.indexOf('concentrating security risk');
  const risk = warning.result.text.slice(riskStart, warning.result.text.indexOf('\n\n', riskStart));
  const safety = [...warning.result.text.matchAll(/\* \*\*([^*]+)\*\*/g)].map(m => m[1]);
  const selected = { captured_at: capture.recorded_at, capture_sha256: sha(bytes),
    discovery: { tool: find.tool, params: find.params, paper },
    math: { tool: math.tool, params: math.params, returned: math.result.returned, available: math.result.available, chars: math.result.chars, truncated: math.result.truncated, object_counts: {equations:6,algorithms:1}, equation },
    specification: { tool: warning.tool, params: warning.params, result: warning.result },
    solana: { tool:'find_papers', params:{query:'durable nonce',layer:'web3',limit:3}, date:'2026-10-02', provided_by:'coordinating agent from read-only MCP call', paper:solana },
    historical: { date:'2026-09-29', control:'team-controlled', network:'Solana devnet', source:'SUBMISSION.md', links:historical.slice(0,3) }, corpus };
  await fs.writeFile(path.join(out, 'evidence-selected.json'), json(selected));
  return { find, math, warning, paper, equation, firstSentence, risk, safety, historical, selected, solana };
}

function content(scene, e) {
  const p = e.paper, w = e.warning.result, eq = e.equation;
  const call = req => `<div class="eyebrow">${escape(req.tool)}</div>${mono(json(req.params))}`;
  switch (scene.kind) {
    case 'hero': return `<div class="hero-mark">dtox<br>research</div><div class="hero-copy">${scene.lines.map(l=>`<p>${escape(l)}</p>`).join('')}</div>`;
    case 'question': return viewer(`<div class="eyebrow">RESEARCH BRIEF / NOT AGENT THOUGHTS</div><div class="question">Research a cross-chain execution agent.</div><div class="split"><p>Find the MEV auction formula.</p><p>Read ERC-4337 security constraints.</p></div><p class="muted">Recorded read-only requests. No execution or signing.</p>`);
    case 'find-call': return viewer(`<div class="find-layout"><div>${call(e.find)}</div><div><div class="paper-title">${escape(p.title)}</div><p class="muted">${p.year} / ${escape(p.venue)}</p>${rows([['id',p.id],['rank / found_via',`${p.relevance_rank} / ${p.found_via}`],['fulltext / exact_latex',`${p.fulltext} / ${p.evidence.exact_latex}`]])}<p class="muted">${escape(p.evidence.acquisition_type)} / ${escape(p.evidence.extraction_type)}</p></div></div>`);
    case 'source-pair': return viewer(`<div class="source-pair"><div><div class="eyebrow">SOLANA / find_papers</div>${mono(json(e.selected.solana.params))}<h2>${escape(e.solana.title)}</h2><p>rank=${e.solana.relevance_rank} / fulltext=${e.solana.fulltext}</p><code>${escape(e.solana.url)}</code></div><div><div class="eyebrow">ETHEREUM / read_paper_section</div><h2>${escape(w.title)}</h2><p>fulltext=${w.fulltext} / ${escape(w.license.spdx)}</p><code>${escape(w.url)}</code><p class="muted">Both: markdown_source / exact_latex=false</p></div></div>`);
    case 'find-result': return viewer(`<div class="paper-title">${escape(p.title)}</div>${rows([['id / year',`${p.id} / ${p.year}`],['venue',p.venue],['citation_count / rank',`${p.citation_count} / ${p.relevance_rank}`],['fulltext / exact_latex',`${p.fulltext} / ${p.evidence.exact_latex}`],['acquisition / extraction',`${p.evidence.acquisition_type} / ${p.evidence.extraction_type}`]])}`);
    case 'math-call': return viewer(`${call(e.math)}<div class="metrics inline"><div><b>7 / 7</b><span>returned / available</span></div><div><b>2,211</b><span>chars</span></div><div><b>false</b><span>truncated</span></div></div><p class="muted">6 equations + 1 algorithm</p>`);
    case 'math': return viewer(`<div class="eyebrow">${escape(eq.section_title)} / eq:p2p-payoff</div><div class="equation">E[u<sub>i</sub><sup>PA</sup> | &alpha;] = (1 - &alpha;) Pr<sup>PA</sup>(O, S<sub>i</sub>) R<sub>i</sub>(O) - b<sub>i</sub><sup>PA</sup>(O, S<sub>i</sub>)</div><div class="eyebrow">ORIGINAL RETURNED LATEX / UNCHANGED TEXT</div>${mono(eq.text)}<div class="provenance">${escape(eq.evidence.acquisition_type)} / ${escape(eq.evidence.extraction_type)} / exact_latex=${eq.evidence.exact_latex}</div>`);
    case 'warning-call': return viewer(`${call(e.warning)}${rows([['total_chars',w.total_chars],['license.spdx',w.license.spdx],['acquisition / extraction',`${w.evidence.acquisition_type} / ${w.evidence.extraction_type}`],['exact_latex',w.evidence.exact_latex]])}`);
    case 'warning': return viewer(`<div class="eyebrow">ERC-4337 / EXACT SELECTED SOURCE FRAGMENTS</div><blockquote>${escape(e.firstSentence)}</blockquote><blockquote class="risk">${escape(e.risk)}</blockquote><div class="split safety">${e.safety.map(s=>`<p>${escape(s)}</p>`).join('')}</div><div class="provenance">CC0-1.0 / markdown_source / exact_latex=false</div>`);
    case 'workflow': return `<div class="flow"><div><b>01</b><h2>Discover</h2><p>find_papers</p></div><div><b>02</b><h2>Read</h2><p>read_paper_section</p></div><div><b>03</b><h2>Inspect</h2><p>get_code_or_math_spec</p></div></div><div class="wide-note">13 public MCP tools / bounded research bundles</div>`;
    case 'corpus': return `<div class="metrics corpus"><div><b>180,101</b><span>documents</span></div><div><b class="orange">17,243</b><span>Web3 documents</span></div><div><b>10,735,072</b><span>structural chunks</span></div><div><b>3,377,328</b><span>citation edges</span></div></div><div class="wide-note">Corpus snapshot / 2026-10-02T14:00Z</div>`;
    case 'history': return viewer(`<div class="eyebrow">HISTORICAL / 2026-09-29 / TEAM-CONTROLLED / SOLANA DEVNET</div><div class="history-grid"><div><h2>SAS attestation</h2><p>paper 2402.03300 / GRPO baseline claim</p><code>${escape(e.historical[0])}</code></div><div><h2>SAS attestation</h2><p>paper 2501.12948 / R1-Zero SFT claim</p><code>${escape(e.historical[1])}</code></div><div><h2>x402 settlement</h2><p>Separate historical payment evidence</p><code>${escape(e.historical[2])}</code></div></div>`, 'HISTORICAL PROOF REFERENCES / NOT A NEW TRANSACTION');
    case 'business': return `<div class="business"><div><div class="eyebrow">AVAILABLE NOW</div><h2>Public beta<br>FREE</h2><p>No key. No signup.</p></div><div><div class="eyebrow">BUSINESS DIRECTION / PROPOSED</div><h2>Pay per research.</h2><p>Optional usage. No subscription.</p><p>Team integrations. Deeper workflows.</p></div></div>`;
    case 'prior': return `<div class="business prior"><div><div class="eyebrow">BEFORE SEPTEMBER 14, 2026</div><h2>Research foundation.</h2><p>Ingestion and quality pipeline</p><p>REST API and MCP read tools</p><p>API-identity registry</p></div><div><div class="eyebrow">DURING THE CONTEST</div><h2>Evidence workflows.</h2><p>Retrieval and structured-source improvements</p><p>Signed readings / SAS / devnet x402</p><p>Source-verified Web3 demo</p></div></div>`;
    case 'boundary': return `<div class="boundary"><div><b>01</b><h2>Signature = authorship.</h2><p>Not truth. Not independence.</p></div><div><b>02</b><h2>Wallet-only agreement stays pending.</h2><p>Registry agreement needs trusted model diversity.</p></div><div><b>03</b><h2>Historical Solana devnet evidence.</h2><p>Base / Ethereum / Arbitrum: configured testnets only.</p></div></div>`;
    case 'end': return `<div class="end"><div class="eyebrow">TRY THE FREE PUBLIC BETA</div><h2>read.whoim.space/mcp</h2><pre>claude mcp add --transport http dtox<br>https://read.whoim.space/mcp</pre><div class="end-code">github.com/who1900/dtox-research</div></div>`;
    default: throw new Error(`Unknown scene ${scene.kind}`);
  }
}
function html(scene, index, name, e) {
  const source = scene.kind.startsWith('find') || scene.kind.startsWith('math') ? e.paper.url : scene.kind.startsWith('warning') ? e.warning.result.url : scene.kind === 'corpus' ? corpus.source : '';
  const recorded = ['question','find-call','find-result','source-pair','math-call','math','warning-call','warning'].includes(scene.kind);
  const footer = recorded ? `EDITORIAL VIEW OF RECORDED RESPONSE / ${e.selected.captured_at.slice(0,10)}` : 'LOCAL PREVIEW / NOT PUBLISHED';
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
  *{box-sizing:border-box}html,body{margin:0;width:1920px;height:1080px;overflow:hidden;background:#fafaf3;color:#1a1a1a;font-family:Arial,sans-serif}body{position:relative}header{position:absolute;left:88px;right:88px;top:42px;display:flex;justify-content:space-between;font:28px Consolas,monospace;color:#72726a}h1{position:absolute;left:88px;top:106px;margin:0;font-size:80px;line-height:.99;letter-spacing:-3.4px;font-weight:700;white-space:pre-line}main{position:absolute;left:88px;right:88px;top:292px;bottom:176px}footer{position:absolute;left:88px;right:88px;bottom:40px;font:28px Consolas,monospace;color:#72726a}footer .url{font-size:32px;color:#1a1a1a;margin-bottom:14px}.viewer{height:100%;background:#edede4;box-shadow:0 6px 24px rgba(27,28,24,.04)}.viewer-head{padding:20px 34px;background:#1a1a1a;color:#fafaf3;font:28px Consolas,monospace}.viewer-body{padding:26px 36px}.eyebrow{font:28px Consolas,monospace;color:#b53600;margin-bottom:20px}pre{font:32px/1.25 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 24px}.viewer pre{background:#fafaf3;padding:22px 26px}h2{font-size:48px;line-height:1.05;letter-spacing:-1.5px;margin:12px 0 22px}p{font-size:36px;line-height:1.25;margin:14px 0}.muted{color:#72726a;font-size:30px}.rows>div{display:flex;gap:32px;margin:12px 0;font:30px/1.25 Consolas,monospace}.rows span{width:410px;flex-shrink:0;color:#72726a}.rows strong{font-weight:400}.paper-title{font-size:48px;line-height:1.12;letter-spacing:-1.5px;max-width:1520px;margin:0 0 28px}.equation{font-size:52px;white-space:nowrap;letter-spacing:-1px;margin:28px 0 38px;font-family:Arial,sans-serif}.equation sup,.equation sub{font-size:55%}.provenance{font:28px Consolas,monospace;color:#72726a;margin-top:18px}blockquote{font:36px/1.24 Consolas,monospace;margin:0 0 26px}.risk{font-size:30px;color:#5e5e56}.split{display:flex;gap:56px}.split>*{flex:1}.safety p{font-size:34px;font-weight:700}.metrics{display:grid;gap:46px}.inline{grid-template-columns:1fr 1fr 1fr;margin-top:28px}.metrics b{display:block;font:700 76px/.95 Consolas,monospace;letter-spacing:-3px}.metrics span{display:block;font:30px Consolas,monospace;margin-top:18px;color:#72726a}.corpus{grid-template-columns:1fr 1fr;gap:76px;padding-top:20px}.corpus b{font-size:78px}.orange{color:#FF4D00}.wide-note{position:absolute;bottom:8px;left:0;font:30px Consolas,monospace;color:#72726a}.hero-mark{position:absolute;left:0;top:0;font-size:130px;line-height:.94;letter-spacing:-6px;padding-left:38px}.hero-mark:before{content:'';position:absolute;left:0;top:0;bottom:-72px;width:10px;background:#FF4D00}.hero-copy{position:absolute;left:930px;top:80px}.hero-copy p{font-size:44px;line-height:1.17;margin:24px 0}.flow{display:flex;gap:34px;padding-top:34px}.flow>div{flex:1;background:#edede4;min-height:330px;padding:32px}.flow b,.boundary b{font:32px Consolas,monospace;color:#FF4D00}.flow h2{font-size:60px;margin-top:42px}.flow p{font:28px Consolas,monospace}.business{display:grid;grid-template-columns:38% 1fr;gap:80px;padding-top:28px}.business>div:first-child{background:#edede4;padding:36px}.business>div:nth-child(2){padding-top:36px}.business h2{font-size:70px}.business p{font-size:34px;margin:25px 0}.business .eyebrow{font-size:28px}.prior{grid-template-columns:46% 1fr;gap:64px}.prior h2{font-size:54px}.prior p{font-size:32px}.boundary>div{display:grid;grid-template-columns:72px 1fr;align-items:start;margin-bottom:26px}.boundary h2{font-size:46px;margin:0 0 8px}.boundary p{grid-column:2;font-size:32px;margin:0;color:#72726a}.history-grid{display:grid;gap:20px}.history-grid>div{display:grid;grid-template-columns:340px 1fr;column-gap:28px}.history-grid h2{font-size:40px;grid-column:1;margin:4px 0}.history-grid p{grid-column:2;font-size:30px;margin:4px 0 12px}.history-grid code{grid-column:2;font:28px/1.2 Consolas,monospace;overflow-wrap:anywhere;max-width:1220px}.end{padding:22px 0}.end h2{font-size:84px;letter-spacing:-3px;margin:42px 0}.end pre{font-size:38px;line-height:1.45;padding:30px;background:#edede4;max-width:1580px}.end-code{font:32px Consolas,monospace;color:#72726a;margin-top:28px}
  .equation{margin:18px 0 22px}.viewer pre{font-size:30px;line-height:1.2}.find-layout{display:grid;grid-template-columns:43% 1fr;gap:44px}.find-layout .rows{margin-top:24px}.find-layout .rows span{width:325px}.find-layout .paper-title{font-size:44px}.source-pair{display:grid;grid-template-columns:1fr 1fr;gap:66px}.source-pair h2{font-size:48px}.source-pair p{font-size:30px}.source-pair code{font:28px/1.25 Consolas,monospace;overflow-wrap:anywhere}.source-pair pre{font-size:28px;margin-bottom:20px}.source-pair>div:nth-child(2){padding-top:0}${fontCss}
  .viewer-body{padding:20px 30px}.viewer pre{font-size:28px;line-height:1.13;padding:16px 22px;margin-bottom:16px}.source-pair pre{padding:12px 16px;line-height:1.1;margin-bottom:12px}.source-pair p{margin:10px 0}
  </style></head><body><header><span>${String(index+1).padStart(2,'0')} / DTOX RESEARCH</span><span>${name.toUpperCase()} / ENGLISH PREVIEW</span></header><h1>${escape(scene.title)}</h1><main>${content(scene,e)}</main><footer>${source ? `<div class="url">${escape(source)}</div>`:''}<div>${escape(footer)}</div></footer></body></html>`;
}

function stamp(t) {
  const ms = Math.round(t*1000); return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')},${String(ms%1000).padStart(3,'0')}`;
}
function captions(scenes) {
  let cursor=0, n=1, text='';
  for (const scene of scenes) {
    const chunks=scene.voice.match(/[^.!?]+[.!?]+|[^.!?]+$/g).map(s=>s.trim());
    const tokens=chunks.map(s=>s.split(/\s+/).length), count=tokens.reduce((a,b)=>a+b,0);
    let local=.35;
    chunks.forEach((s,i)=>{const end=local+scene.audioDuration*tokens[i]/count;
      const words=s.split(/\s+/); let line='', lines=[]; for(const word of words){if((line+' '+word).trim().length>72){lines.push(line);line=word;}else line=(line+' '+word).trim();}if(line)lines.push(line);
      text+=`${n++}\n${stamp(cursor+local)} --> ${stamp(cursor+end)}\n${lines.join('\n')}\n\n`; local=end;
    }); cursor+=scene.duration;
  } return text;
}

async function prepare(e) {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--disable-background-networking','--disable-component-update','--disable-default-apps','--no-first-run','--disable-sync','--disable-extensions','--disable-features=MediaRouter,OptimizationHints']});
  const context=await browser.newContext({viewport:{width:1920,height:1080},deviceScaleFactor:1,serviceWorkers:'block'});
  await context.route('**/*',route=>route.abort());
  const page=await context.newPage(); const manifests=[];
  try {
    for(const [name,story] of Object.entries({presentation,demo})) {
      const dir=path.join(out,name);await fs.mkdir(dir,{recursive:true});const scenes=[];
      for(const [i,scene] of story.entries()) {
        const png=path.join(dir,`${scene.id}.png`), wav=path.join(dir,`${scene.id}.wav`);
        await page.setContent(html(scene,i,name,e));await page.evaluate(()=>document.fonts.ready);
        const issues=await page.evaluate(()=>[...document.querySelectorAll('main *')].filter(el=>{const r=el.getBoundingClientRect();return r.bottom>933||r.right>1834}).map(el=>({tag:el.tagName,text:el.textContent.slice(0,90),rect:el.getBoundingClientRect().toJSON()})));
        if(issues.length) throw new Error(`Layout overflow ${name}/${scene.id}: ${json(issues)}`);
        await page.screenshot({path:png});scenes.push({...scene,png,wav});console.log(`FRAME ${name}/${scene.id}`);
      }
      const manifest={name,scenes,capture_sha256:e.selected.capture_sha256};const manifestPath=path.join(dir,'manifest.json');await fs.writeFile(manifestPath,json(manifest));
      manifests.push({manifest,manifestPath,dir});
      await sharp(scenes[0].png).resize(1280,720).png().toFile(path.join(out,`${name}-thumbnail.png`));
    }
  } finally {await browser.close();}
  if(process.argv.includes('--frames-only')) return manifests;
  return await measure(manifests);
}
async function measure(manifests) {
  for(const {manifest,manifestPath,dir} of manifests) {
    if(!process.argv.includes('--measure-only')) {
      console.log(`NARRATION ${manifest.name}`);
      await run('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',path.join(src,'voice.ps1'),'-ManifestPath',manifestPath]);
    }
    for(const scene of manifest.scenes) {
      scene.audioDuration=Number((await probe(scene.wav)).format.duration);
      scene.duration=Math.ceil(Math.max(scene.min,scene.audioDuration+1.5)*fps)/fps;
    }
    manifest.duration=manifest.scenes.reduce((a,s)=>a+s.duration,0);
    manifest.word_count=manifest.scenes.reduce((a,s)=>a+s.voice.split(/\s+/).length,0);
    if(manifest.duration>180 || (manifest.name==='presentation'&&manifest.duration<120)) throw new Error(`Runtime outside required bounds: ${manifest.name} ${manifest.duration}`);
    await fs.writeFile(manifestPath,json(manifest));await fs.writeFile(path.join(out,`${manifest.name}.srt`),captions(manifest.scenes));
    await fs.writeFile(path.join(out,`${manifest.name}-narration.txt`),manifest.scenes.map(s=>s.voice).join('\n\n'));
    console.log(`READY ${manifest.name}: ${manifest.duration.toFixed(2)}s / ${manifest.word_count} words`);
  }
  return manifests;
}

async function render(manifests) {
  const reports=[];
  for(const {manifest,dir} of manifests) {
    const segments=[];
    for(const scene of manifest.scenes) {
      const segment=path.join(dir,`${scene.id}.mp4`), frames=Math.round(scene.duration*fps);
      const vf=`zoompan=z='1+0.009*on/${frames}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=${frames}:s=1920x1080:fps=${fps},fade=t=in:st=0:d=0.3:color=0xfafaf3,fade=t=out:st=${scene.duration-.3}:d=0.3:color=0xfafaf3,format=yuv420p`;
      console.log(`ENCODE ${manifest.name}/${scene.id} ${scene.duration.toFixed(2)}s`);
      await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-threads','2','-i',scene.png,'-i',scene.wav,'-vf',vf,'-filter_threads','2','-af','adelay=350|350,apad','-t',String(scene.duration),'-r',String(fps),'-c:v','libx264','-preset','ultrafast','-crf','20','-threads','2','-pix_fmt','yuv420p','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-c:a','aac','-b:a','160k','-ar','48000','-ac','1',segment]);
      segments.push(segment);
    }
    const list=path.join(dir,'concat.txt');await fs.writeFile(list,segments.map(s=>`file '${s.replaceAll('\\','/')}'`).join('\n'));
    const movie=path.join(out,`${manifest.name}-preview.mp4`);
    await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-f','concat','-safe','0','-i',list,'-c','copy','-movflags','+faststart',movie]);
    const report=await probe(movie), video=report.streams.find(s=>s.codec_type==='video'), audio=report.streams.find(s=>s.codec_type==='audio');
    if(video?.width!==1920||video?.height!==1080||!audio||Number(report.format.duration)>180)throw new Error(`Output QA failed: ${movie}`);
    const points=[];let offset=0;
    for(const [i,scene] of manifest.scenes.entries()) {
      const shot=path.join(dir,`qa-${String(i+1).padStart(2,'0')}.png`);await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-threads','2','-ss',String(offset+scene.duration/2),'-i',movie,'-frames:v','1','-vf','scale=640:360','-threads','2',shot]);
      points.push({input:shot,left:(i%2)*640,top:Math.floor(i/2)*360});offset+=scene.duration;
    }
    const contact=path.join(out,`${manifest.name}-contact-sheet.png`);await sharp({create:{width:1280,height:Math.ceil(points.length/2)*360,channels:3,background:'#fafaf3'}}).composite(points).png().toFile(contact);
    await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-threads','2','-i',movie,'-vn','-c:a','pcm_s16le',path.join(out,`${manifest.name}-narration.wav`)]);
    const integrity=await run(ffmpeg,['-hide_banner','-loglevel','error','-threads','2','-i',movie,'-f','null','-'],120000);
    reports.push({name:manifest.name,file:movie,duration:Number(report.format.duration),word_count:manifest.word_count,video:{codec:video.codec_name,width:video.width,height:video.height,fps:video.avg_frame_rate},audio:{codec:audio.codec_name,sample_rate:audio.sample_rate,channels:audio.channels},capture_sha256:manifest.capture_sha256,decode_errors:integrity.trim(),fonts:brandFonts?'Inter / Roboto Mono (embedded local OFL assets)':'Arial / Consolas (local Windows fallback)',captions:'Scene-aligned proportional sentence timings; manual listening remains the publish gate.',scene_durations:manifest.scenes.map(s=>({id:s.id,audio:s.audioDuration,video:s.duration}))});
    console.log(`COMPLETE ${movie} ${report.format.duration}s`);
  }
  await fs.writeFile(path.join(out,'qa-report.json'),json(reports));
  return reports;
}

await fs.mkdir(out,{recursive:true});
try {
  const inter=(await fs.readFile(path.join(src,'fonts/Inter.ttf'))).toString('base64');
  const roboto=(await fs.readFile(path.join(src,'fonts/RobotoMono.ttf'))).toString('base64');
  fontCss=`@font-face{font-family:Inter;src:url(data:font/ttf;base64,${inter}) format('truetype');font-weight:100 900}@font-face{font-family:'Roboto Mono';src:url(data:font/ttf;base64,${roboto}) format('truetype');font-weight:100 700}html,body,h1,h2,p,.equation{font-family:Inter,Arial,sans-serif}header,footer,pre,.eyebrow,.rows>div,.provenance,.metrics b,.metrics span,.flow b,.flow p,.boundary b,.wide-note,.history-grid code,.source-pair code,.end-code{font-family:'Roboto Mono',Consolas,monospace}`;
  brandFonts=true;
} catch {console.log('FONTS Arial / Consolas fallback');}
const e=await evidence();
let manifests;
if(process.argv.includes('--render-only')||process.argv.includes('--measure-only')) manifests=await Promise.all(['presentation','demo'].map(async name=>{const dir=path.join(out,name),manifestPath=path.join(dir,'manifest.json');return {dir,manifestPath,manifest:JSON.parse(await fs.readFile(manifestPath,'utf8'))};}));
else manifests=await prepare(e);
if(process.argv.includes('--measure-only')) await measure(manifests);
else if(!process.argv.includes('--prepare')&&!process.argv.includes('--frames-only')) console.log(json(await render(manifests)));
