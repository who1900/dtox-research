import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { demo, historical } from './story.mjs';
import { pitchNarration } from './pitch-narration.mjs';

const src=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(src,'../..');
const out=path.join(root,'presentation/video-2026-10-05');
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/User/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const sharp=require(process.env.SHARP_MODULE || 'C:/Users/User/Documents/work/Claude/dtox-research-public/.deck-build/node_modules/sharp');
const ffmpeg=process.env.FFMPEG || 'C:/ffmpeg/bin/ffmpeg.exe';
const ffprobe=process.env.FFPROBE || 'C:/ffmpeg/bin/ffprobe.exe';
const fps=24;
const only=process.argv.find(x=>x.startsWith('--only='))?.split('=')[1];
if(only&&!['presentation','demo'].includes(only))throw new Error('Invalid --only');
const names=only?[only]:['presentation','demo'];
sharp.concurrency(2);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json=o=>JSON.stringify(o,null,2);
const sha=b=>createHash('sha256').update(b).digest('hex');
const code=s=>`<pre>${esc(s)}</pre>`;
const badge=s=>`<div class="tag">${esc(s)}</div>`;
const panel=(title,body)=>`<section class="panel"><div class="panel-head">${esc(title)}</div><div class="panel-body">${body}</div></section>`;
const pairs=items=>`<div class="pairs">${items.map(([k,v])=>`<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div>`;
function run(exe,args,timeout=300000) {
  return new Promise((resolve,reject)=>{
    const child=spawn(exe,args,{cwd:root,windowsHide:true,env:{...process.env,OMP_NUM_THREADS:'2'}});
    let output='';child.stdout.on('data',b=>output+=b);child.stderr.on('data',b=>output+=b);
    const timer=setTimeout(()=>{child.kill();reject(new Error('Timed out: '+exe));},timeout);
    child.on('error',e=>{clearTimeout(timer);reject(e);});
    child.on('close',status=>{clearTimeout(timer);status===0?resolve(output):reject(new Error(`${exe} exit ${status}: ${output.slice(-4000)}`));});
  });
}
async function probe(file){return JSON.parse(await run(ffprobe,['-v','error','-show_streams','-show_format','-of','json',file]));}

async function evidence(){
  const oldBytes=await fs.readFile(path.join(src,'source-captures/2026-10-02-web3-demo.json'));
  const newBytes=await fs.readFile(path.join(src,'source-captures/2026-10-05-mcp-demo-excerpts.json'));
  const old=JSON.parse(oldBytes),current=JSON.parse(newBytes);
  const discovery=old.requests.find(r=>r.tool==='find_papers');
  const found=JSON.parse(discovery.response.content.find(c=>c.type==='text').text).papers[0];
  const math=current.calls.find(c=>c.tool==='get_code_or_math_spec');
  const assumptions=current.calls.find(c=>c.tool==='read_paper_section'&&c.arguments.paper_id==='2101.05511');
  const eip=current.calls.find(c=>c.arguments.paper_id==='eip:4337');
  if(found.id!=='2101.05511'||found.found_via!=='title match')throw new Error('Unexpected captured discovery');
  if(math.response_excerpt.returned!==6||math.response_excerpt.available!==6||math.response_excerpt.truncated!==false)throw new Error('Unexpected captured math');
  if(!math.response_excerpt.selected_section.text.includes('\\label{eq:bidstrategy}')||!math.response_excerpt.selected_section.text.includes('\\text{PA}'))throw new Error('Bid identity changed');
  if(assumptions.response_excerpt.next_offset!==2200||assumptions.response_excerpt.total_chars!==3087)throw new Error('Assumption boundary changed');
  if(eip.response_excerpt.total_chars!==1205||eip.response_excerpt.evidence.acquisition_type!=='spec-markdown')throw new Error('EIP capture changed');
  const selected={discovery:{recorded_at:old.recorded_at,tool:discovery.tool,arguments:discovery.params,paper:found},math,assumptions,eip,historical,
    source_files:[{path:'docs/video-2026-10-05/source-captures/2026-10-02-web3-demo.json',sha256:sha(oldBytes)},{path:'docs/video-2026-10-05/source-captures/2026-10-05-mcp-demo-excerpts.json',sha256:sha(newBytes)}],
    coverage_source:'docs/technical-reference.md',pitch_source:'presentation/pitch-visual/speaker-notes.md',
    capture_boundary:'Selected actual historical responses; editorial viewer is created for this video and is not a product UI screenshot or a continuous live session.'};
  await fs.writeFile(path.join(out,'evidence-selected.json'),json(selected));return selected;
}

function body(scene,e){
  const math=e.math.response_excerpt,eq=math.selected_section,eip=e.eip.response_excerpt;
  switch(scene.kind){
    case 'scope':return `<div class="hero"><h2>Source → Assumption → Decision</h2><p>Auction mechanism + wallet trust boundary</p>${badge('ACTUAL CAPTURES / OCT 2 + OCT 5')}<p class="muted">Edited response viewer • No live session claim</p></div>`;
    case 'find':return `<div class="split">${panel('CAPTURED REQUEST / 2026-10-02',code('find_papers\n'+json(e.discovery.arguments)))}${panel('SELECTED CAPTURED RESULT',`<h2>${esc(e.discovery.paper.title)}</h2>`+pairs([['paper id',e.discovery.paper.id],['rank / match','1 / title match'],['fulltext',true],['acquisition','latex'],['parser completeness','unknown']]))}</div>`;
    case 'math':return `<div class="split">${panel('CAPTURED REQUEST / 2026-10-05',code('get_code_or_math_spec\n'+json(e.math.arguments)))}${panel('SELECTED CAPTURED RESULT',pairs([['returned / available','6 / 6'],['truncated',false],['completeness','fulltext_acquired'],['section','Incentive Compatibility of the Relay auction Participation'],['element','equation']]))}</div>`;
    case 'assumptions':return `<div class="split wide">${panel('ORIGINAL SOURCE OBJECT',`<div class="formula">b<sub>i</sub><sup>PA</sup>(𝒪,𝒮<sub>i</sub>) = <span class="fraction"><span>n − 1</span><span>n</span></span> ℛ<sub>i</sub>(𝒪)</div>`+code(eq.text))}${panel('SOURCE ASSUMPTIONS',`<blockquote>“${esc(e.assumptions.response_excerpt.quote)}”</blockquote><p>Independent rewards • Common uniform distribution</p><p>Known participant count and distribution</p>${badge('DEVELOPER INFERENCE')}<p>Check assumptions before reuse.</p><p class="muted">Solana applicability still needs review.</p>`)}</div>`;
    case 'provenance':return `<div class="split">${panel('OBJECT PROVENANCE / OCT 5',pairs([['acquisition_type','latex'],['extraction_type','latex_source'],['exact_latex',true],['parse_completeness','unknown'],['source_url_status','source_not_verified']]))}${panel('RELATED SECTION / OCT 5',pairs([['returned chars',2200],['total chars',3087],['next_offset',2200]])+'<h2>Duplication + truncation</h2><p>Observed extraction artifacts retained.</p><p class="muted">Official source checked separately by the pitch author.</p>')}</div>`;
    case 'eip-request':return `<div class="split">${panel('CAPTURED REQUEST / 2026-10-05',code('read_paper_section\n'+json(e.eip.arguments)))}${panel('SELECTED CAPTURED RESULT',pairs([['returned chars',1205],['next_offset','null'],['fulltext',true],['acquisition','spec-markdown'],['extraction','markdown_source'],['license','CC0-1.0']]))}</div>`;
    case 'eip-text':return `<div class="split">${panel('AUTHOR TEXT / SELECTED EXCERPT',`<blockquote>“${esc(eip.quote)}”</blockquote><p>Source: EIP-4337 Security Considerations</p>${badge('SOURCE EVIDENCE')}<p class="muted">Selected specification passage.</p>`)}${panel('DEVELOPER INFERENCE','<h2>Include EntryPoint<br>in the trust boundary.</h2><p>Use the source to inform an audit plan.</p><p class="muted">Retrieval supports review.<br>It does not perform an audit.</p>')}</div>`;
    case 'coverage':return `<div class="split">${panel('CURRENT DOCUMENTED INTERFACE',`<h2>13 public MCP tools</h2><p>Free public read beta</p>${code('get_research_bundle(query,\n  strict=true, max_chars=12000)')}<p class="muted">A bounded reading packet; no registry write.</p>`)}${panel('PER-RECORD COVERAGE','<h2>Inspect provenance.</h2><p>arXiv may be abstract-only.</p><p>IACR may have recovered full text.</p>'+code('corpus.as_of = observation time')+'<p class="muted">Inventory counters are not traction.<br>Observation time is not a snapshot ID.</p>')}</div>`;
    case 'devnet':return `<div class="split">${panel('HISTORICAL RECORD / OCTOBER 3',pairs([['timestamp','2026-10-03T09:55:43.992Z'],['network','Solana devnet'],['activity','one payment + one signed test'],['verdict','does_not_assert'],['registry','read_once'],['readers / quorum','1 / false']]))}${panel('BOUNDARY / NOT REPEATED',`<h2>Wallet attribution.<br>No independent consensus.</h2><p>Documented public RPC readback passed.</p><p class="muted">Different event from the read-only examples.<br>Paid mainnet: NO-GO.</p>${code('PDA: '+historical.pda)}<p class="muted">No payment or signing for this video.</p>`)}</div>`;
    case 'connect':return `<div class="hero"><h2>Try the free reading workflow.</h2>${code('https://read.whoim.space/mcp')}<p>Inspect the original source and limits.</p><p class="muted">No wallet required for public reads.</p></div>`;
    default:throw new Error(scene.kind);
  }
}
function source(scene){return ['find','math','assumptions','provenance'].includes(scene.kind)?'arxiv.org/abs/2101.05511':scene.kind.startsWith('eip')?'eips.ethereum.org/EIPS/eip-4337#security-considerations':scene.kind==='devnet'?'docs/2026-10-03-payment-release-gate.md • docs/2026-10-03-live-release-evidence.md':'docs/technical-reference.md • read.whoim.space/research/';}
function html(scene,index,e,fontCss){return `<!doctype html><html><head><meta charset="utf-8"><style>${fontCss}
*{box-sizing:border-box}html,body{width:1920px;height:1080px;margin:0;overflow:hidden;background:#fafaf3;color:#1a1a1a;font-family:Inter,Arial,sans-serif}body{padding:56px 88px;background:radial-gradient(ellipse at 82% 0%,#f0f2ff 0%,#fafaf3 62%)}header{display:flex;justify-content:space-between;align-items:center;font:26px 'Roboto Mono',Consolas,monospace;color:#244bea}h1{font-size:72px;line-height:1.06;letter-spacing:-2.5px;font-weight:720;margin:40px 0 36px}main{height:620px}.split{display:grid;grid-template-columns:1fr 1fr;gap:36px;height:600px}.wide{grid-template-columns:1.08fr 1fr}.panel{background:#f0f0e8;box-shadow:0 14px 32px rgba(27,28,24,.035);height:100%;overflow:hidden}.panel-head{background:#244bea;color:#fff;padding:18px 24px;font:24px 'Roboto Mono',Consolas,monospace}.panel-body{padding:25px 28px}h2{font-size:44px;line-height:1.12;font-weight:720;letter-spacing:-1px;margin:12px 0 28px}p{font-size:31px;line-height:1.3;margin:18px 0}pre{font:28px/1.4 'Roboto Mono',Consolas,monospace;background:#e6e7df;padding:24px;margin:12px 0 24px;white-space:pre-wrap;overflow-wrap:anywhere}blockquote{font-size:43px;line-height:1.2;font-weight:640;margin:22px 0 38px;color:#244bea}.muted{color:#66675f}.tag{font:24px 'Roboto Mono',Consolas,monospace;color:#244bea;margin:26px 0 14px}.pairs{display:grid;gap:24px;margin:10px 0 24px}.pairs>div{display:grid;grid-template-columns:43% 57%;gap:15px;font:27px/1.25 'Roboto Mono',Consolas,monospace}.pairs span{color:#66675f}.pairs b{font-weight:600;overflow-wrap:anywhere}.formula{font:46px/1.6 Georgia,serif;text-align:center;background:#fff;padding:22px 10px;margin:8px 0 26px;white-space:nowrap}.formula sub,.formula sup{font-size:.5em}.fraction{display:inline-flex;vertical-align:middle;flex-direction:column;text-align:center;line-height:1.08}.fraction span:first-child{border-bottom:2px solid;padding:0 8px 5px}.fraction span:last-child{padding-top:5px}.hero{padding:62px 48px;max-width:1650px}.hero h2{font-size:80px;margin-bottom:38px}.hero p{font-size:39px}.hero pre{font-size:42px;display:inline-block;padding:24px 34px}footer{position:absolute;left:88px;right:88px;top:925px;font:25px/1.35 'Roboto Mono',Consolas,monospace;color:#66675f;max-height:70px}.footer-note{font-size:22px;margin-top:8px}</style></head><body><header><b>${String(index+1).padStart(2,'0')} / DTOX RESEARCH</b><span>EDITORIAL RESPONSE VIEWER / HISTORICAL CAPTURES</span></header><h1>${esc(scene.title)}</h1><main>${body(scene,e)}</main><footer>${esc(source(scene))}<div class="footer-note">Read-only evidence examples • Presenter interpretation labeled • No continuous live session claim</div></footer></body></html>`;}

function splitPhrases(text){
  const sentences=text.match(/[^.!?]+[.!?]+|[^.!?]+$/g).map(x=>x.trim());
  const result=[];
  for(let sentence of sentences){
    while(sentence.length>140){
      let cut=sentence.lastIndexOf(',',140)+1;if(cut<70)cut=sentence.lastIndexOf(' ',140);
      result.push(sentence.slice(0,cut).trim());sentence=sentence.slice(cut).trim();
    }if(sentence)result.push(sentence);
  }return result;
}
function spoken(text){return text.replace(/dTox/g,'dee tox').replace(/MCP/g,'M C P').replace(/ERC-4337/g,'E R C forty three thirty seven').replace(/USDC/g,'U S D C').replace(/x402/g,'x four oh two').replace(/SAS/g,'S A S').replace(/BM25/g,'B M twenty five').replace(/arxiv-mcp-server/g,'ar X iv M C P server').replace(/arXiv/g,'ar X iv').replace(/EVM/g,'E V M');}
function caption(text){return text.replace(/M C P/g,'MCP').replace(/E R C forty three thirty seven/g,'ERC-4337').replace(/C C zero/g,'CC0').replace(/I A C R/g,'IACR');}
async function pitch(){
  const notes=await fs.readFile(path.join(root,'presentation/pitch-visual/speaker-notes.md'),'utf8');
  const main=notes.split('## Appendix')[0];
  const sections=[...main.matchAll(/### (\d)\. ([^\r\n]+)\r?\n\r?\n([\s\S]+?)(?=\r?\n\r?\nSources?:|$)/g)];
  if(sections.length!==9)throw new Error('Expected 9 canonical spoken main slides');
  return sections.map((m,i)=>({id:`slide-${m[1].padStart(2,'0')}`,title:m[2],voice:pitchNarration[i],min:8,png:path.join(out,'pitch-frames',`slide-${m[1].padStart(2,'0')}.png`)}));
}
async function prepare(e){
  const inter=(await fs.readFile(path.join(src,'fonts/Inter.ttf'))).toString('base64');
  const mono=(await fs.readFile(path.join(src,'fonts/RobotoMono.ttf'))).toString('base64');
  const fontCss=`@font-face{font-family:Inter;src:url(data:font/ttf;base64,${inter}) format('truetype');font-weight:100 900}@font-face{font-family:'Roboto Mono';src:url(data:font/ttf;base64,${mono}) format('truetype');font-weight:100 700}`;
  const browser=await chromium.launch({executablePath:process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--disable-background-networking','--disable-component-update','--no-first-run','--disable-sync','--disable-extensions']});
  const ctx=await browser.newContext({viewport:{width:1920,height:1080},deviceScaleFactor:1,serviceWorkers:'block'});
  await ctx.route('**/*',route=>route.abort());const page=await ctx.newPage();const manifests=[];
  try{
    for(const [name,story] of Object.entries({presentation:await pitch(),demo}).filter(([name])=>names.includes(name))){
      const dir=path.join(out,name);await fs.mkdir(dir,{recursive:true});const scenes=[];
      for(const [i,s] of story.entries()){
        const scene={...s,png:s.png||path.join(dir,s.id+'.png'),wav:path.join(dir,s.id+'.wav')};
        if(name==='demo'){
          const markup=html(scene,i,e,fontCss);await fs.writeFile(path.join(dir,s.id+'.html'),markup);
          await page.setContent(markup);await page.evaluate(()=>document.fonts.ready);
          const overflow=await page.evaluate(()=>[...document.querySelectorAll('.panel-body>*')].filter(el=>{const r=el.getBoundingClientRect();return r.bottom>905||r.right>1835;}).map(el=>({text:el.textContent.slice(0,60),rect:el.getBoundingClientRect().toJSON()})));
          if(overflow.length)throw new Error('Demo overflow '+scene.id+': '+json(overflow));
          await page.screenshot({path:scene.png});
        }
        scene.sentences=splitPhrases(scene.voice).map((text,j)=>({text:caption(text),spoken:spoken(text),wav:path.join(dir,`${scene.id}-${String(j+1).padStart(2,'0')}.wav`)}));
        scenes.push(scene);
      }
      const manifest={name,source_captures:e.source_files,pitch_notes_sha256:name==='presentation'?sha(await fs.readFile(path.join(root,'presentation/pitch-visual/speaker-notes.md'))):null,scenes};
      const manifestPath=path.join(dir,'manifest.json');await fs.writeFile(manifestPath,json(manifest));manifests.push({manifest,manifestPath,dir});
      console.log(`FRAMES ${name}: ${scenes.length}`);
    }
  }finally{await browser.close();}
  return manifests;
}
function stamp(t){const ms=Math.round(t*1000);return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')},${String(ms%1000).padStart(3,'0')}`;}
function wrap(text){let lines=[],line='';for(const word of text.split(/\s+/)){if((line+' '+word).trim().length>72){lines.push(line);line=word;}else line=(line+' '+word).trim();}if(line)lines.push(line);return lines.join('\n');}
async function measure(manifests){
  for(const {manifest,manifestPath,dir} of manifests){
    if(!process.argv.includes('--measure-only'))console.log(await run('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',path.join(src,'voice.ps1'),'-ManifestPath',manifestPath]));
    let time=0,n=1,srt='';
    for(const scene of manifest.scenes){
      let local=.4;const audioFiles=[];
      for(const sentence of scene.sentences){
        sentence.duration=Number((await probe(sentence.wav)).format.duration);sentence.start=time+local;sentence.end=sentence.start+sentence.duration;
        srt+=`${n++}\n${stamp(sentence.start)} --> ${stamp(sentence.end)}\n${wrap(sentence.text)}\n\n`;
        local+=sentence.duration+.12;audioFiles.push(sentence.wav);
      }
      scene.audioDuration=local-.4-.12;
      scene.duration=Math.ceil(Math.max(scene.min,local+.75)*fps)/fps;
      const pieces=scene.sentences.map((s,i)=>`[${i}:a]apad=pad_dur=0.12[a${i}]`).join(';')+';'+scene.sentences.map((s,i)=>`[a${i}]`).join('')+`concat=n=${scene.sentences.length}:v=0:a=1[out]`;
      await run(ffmpeg,['-hide_banner','-loglevel','error','-y',...audioFiles.flatMap(wav=>['-i',wav]),'-filter_complex',pieces,'-map','[out]','-c:a','pcm_s16le',scene.wav]);
      scene.start=time;scene.end=time+scene.duration;time+=scene.duration;
    }
    manifest.duration=time;manifest.word_count=manifest.scenes.reduce((a,s)=>a+s.voice.split(/\s+/).length,0);
    await fs.writeFile(manifestPath,json(manifest));await fs.writeFile(path.join(out,manifest.name+'.srt'),srt);
    await fs.writeFile(path.join(out,manifest.name+'-narration.txt'),manifest.scenes.map(s=>s.voice).join('\n\n'));
    if(time>180||(manifest.name==='presentation'&&time<120))throw new Error(`Runtime outside bounds ${manifest.name}: ${time}`);
    console.log(`READY ${manifest.name}: ${time.toFixed(3)} seconds / ${manifest.word_count} words`);
  }
}
async function render(manifests){
  let reports=[];
  if(only)try{reports=JSON.parse(await fs.readFile(path.join(out,'qa-report.json'),'utf8')).filter(r=>r.name!==only);}catch{}
  for(const {manifest,dir} of manifests){
    const segments=[];
    for(const scene of manifest.scenes){
      const segment=path.join(dir,scene.id+'.mp4');
      const vf=`scale=1920:1080:flags=lanczos,setsar=1,fade=t=in:st=0:d=0.25:color=0xfafaf3,fade=t=out:st=${scene.duration-.25}:d=0.25:color=0xfafaf3,format=yuv420p`;
      console.log(`ENCODE ${manifest.name}/${scene.id}: ${scene.duration.toFixed(3)}s`);
      await run(ffmpeg,['-hide_banner','-loglevel','error','-y','-loop','1','-framerate',String(fps),'-i',scene.png,'-i',scene.wav,'-vf',vf,'-filter_threads','2','-af','adelay=400,apad','-t',String(scene.duration),'-r',String(fps),'-c:v','libx264','-preset','ultrafast','-crf','19','-threads','2','-pix_fmt','yuv420p','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-c:a','aac','-b:a','160k','-ar','48000','-ac','1',segment]);
      segments.push(segment);
    }
    const concat=path.join(dir,'concat.txt');await fs.writeFile(concat,segments.map(s=>`file '${s.replaceAll('\\','/')}'`).join('\n'));
    const plain=path.join(dir,'joined.mp4'),movie=path.join(out,manifest.name+'-preview.mp4');
    await run(ffmpeg,['-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',concat,'-c','copy',plain]);
    await run(ffmpeg,['-hide_banner','-loglevel','error','-y','-i',plain,'-i',path.join(out,manifest.name+'.srt'),'-map','0:v','-map','0:a','-map','1:0','-c','copy','-c:s','mov_text','-metadata:s:s:0','language=eng','-metadata:s:s:0','title=English','-movflags','+faststart',movie]);
    const p=await probe(movie),video=p.streams.find(s=>s.codec_type==='video'),audio=p.streams.find(s=>s.codec_type==='audio'),subtitle=p.streams.find(s=>s.codec_type==='subtitle');
    const duration=Number(p.format.duration);
    if(video.width!==1920||video.height!==1080||!audio||!subtitle||duration>180||(manifest.name==='presentation'&&duration<120))throw new Error('Output contract failed '+movie);
    const points=[];
    for(const [i,scene] of manifest.scenes.entries()){
      const frame=path.join(dir,'qa-'+String(i+1).padStart(2,'0')+'.png');
      await run(ffmpeg,['-hide_banner','-loglevel','error','-y','-ss',String(scene.start+scene.duration/2),'-i',movie,'-frames:v','1','-vf','scale=960:540','-threads','2',frame]);
      points.push({input:frame,left:(i%2)*960,top:Math.floor(i/2)*540});
    }
    await sharp({create:{width:1920,height:Math.ceil(points.length/2)*540,channels:3,background:'#fafaf3'}}).composite(points).png().toFile(path.join(out,manifest.name+'-contact-sheet.png'));
    await sharp(manifest.scenes[0].png).resize(1280,720).png().toFile(path.join(out,manifest.name+'-thumbnail.png'));
    await run(ffmpeg,['-hide_banner','-loglevel','error','-y','-i',movie,'-vn','-c:a','pcm_s16le',path.join(out,manifest.name+'-narration.wav')]);
    const decode=await run(ffmpeg,['-hide_banner','-loglevel','error','-threads','2','-i',movie,'-f','null','-']);
    const loudness=await run(ffmpeg,['-hide_banner','-i',movie,'-vn','-af','volumedetect','-f','null','-']);
    const sourceImages=await Promise.all(manifest.scenes.map(async s=>({id:s.id,sha256:sha(await fs.readFile(s.png))})));
    const timing={first_caption:manifest.scenes[0].sentences[0].start,last_caption:manifest.scenes.at(-1).sentences.at(-1).end,total_captions:manifest.scenes.reduce((a,s)=>a+s.sentences.length,0),overlaps:0,within_duration:true,method:'Each caption phrase has an individually synthesized and ffprobe-measured WAV; exact scene/phrase offsets include 0.4s leading silence and 0.12s phrase gap.'};
    const report={name:manifest.name,file:path.relative(root,movie).replaceAll('\\','/'),duration,word_count:manifest.word_count,video:{codec:video.codec_name,width:video.width,height:video.height,fps:video.avg_frame_rate},audio:{codec:audio.codec_name,sample_rate:audio.sample_rate,channels:audio.channels,mean_volume:loudness.match(/mean_volume: ([^\r\n]+)/)?.[1],max_volume:loudness.match(/max_volume: ([^\r\n]+)/)?.[1]},subtitles:{codec:subtitle.codec_name,language:'eng',...timing},decode_errors:decode.trim(),voice:'Microsoft Zira Desktop, local offline TTS, rate 0',source_images:sourceImages,source_captures:manifest.source_captures,scene_timings:manifest.scenes.map(s=>({id:s.id,start:s.start,end:s.end,video_duration:s.duration,audio_duration:s.audioDuration})),visual_inspection:'Pending human/model inspection of every extracted movie frame.'};
    reports.push(report);console.log(`COMPLETE ${manifest.name} ${duration.toFixed(3)}s`);
    await fs.writeFile(path.join(out,'qa-report.json'),json(reports));
  }
  return reports;
}

await fs.mkdir(out,{recursive:true});
const e=await evidence();
let manifests;
if(process.argv.includes('--render-only')||process.argv.includes('--measure-only'))manifests=await Promise.all(names.map(async name=>{const dir=path.join(out,name),manifestPath=path.join(dir,'manifest.json');return {dir,manifestPath,manifest:JSON.parse(await fs.readFile(manifestPath,'utf8'))};}));
else manifests=await prepare(e);
if(process.argv.includes('--frames-only'))process.exit(0);
if(!process.argv.includes('--render-only'))await measure(manifests);
if(!process.argv.includes('--prepare')&&!process.argv.includes('--measure-only'))await render(manifests);


