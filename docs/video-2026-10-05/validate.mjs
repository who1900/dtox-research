import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {pitchNarration} from './pitch-narration.mjs';
import {demo} from './story.mjs';
const run=promisify(execFile),src=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(src,'../..'),out=path.join(root,'presentation/video-2026-10-05');
const sha=b=>createHash('sha256').update(b).digest('hex');
const stamp=s=>{const [h,m,z]=s.replace(',','.').split(':').map(Number);return h*3600+m*60+z;};
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
const tidy=s=>s.replace(/\s+/g,' ').trim();
const reports=JSON.parse(await fs.readFile(path.join(out,'qa-report.json'),'utf8')),results=[];
for(const name of ['presentation','demo']){
  const manifest=JSON.parse(await fs.readFile(path.join(out,name,'manifest.json'),'utf8'));
  const source=name==='presentation'?pitchNarration:demo.map(s=>s.voice);
  assert(JSON.stringify(source)===JSON.stringify(manifest.scenes.map(s=>s.voice)),name+': source/manifest speech mismatch');
  assert(tidy(await fs.readFile(path.join(out,name+'-narration.txt'),'utf8'))===tidy(source.join(' ')),name+': narration TXT mismatch');
  const raw=await fs.readFile(path.join(out,name+'.srt'),'utf8');
  const cues=raw.trim().split(/\r?\n\r?\n/).map(block=>{const [id,time,...text]=block.split(/\r?\n/),[start,end]=time.split(' --> ');return {id:Number(id),start:stamp(start),end:stamp(end),text:text.join(' '),lines:text};});
  const phrases=manifest.scenes.flatMap(s=>s.sentences);
  assert(cues.length===phrases.length,name+': caption count mismatch');
  for(const [i,cue] of cues.entries()){
    const phrase=phrases[i];assert(cue.id===i+1,name+': caption ID');
    assert(tidy(cue.text)===tidy(phrase.text),name+': caption text differs '+i);
    assert(Math.abs(cue.start-phrase.start)<=.00051&&Math.abs(cue.end-phrase.end)<=.00051,name+': phrase timing differs '+i);
    assert(cue.end>cue.start&&cue.start>=(cues[i-1]?.end||0),name+': overlap or invalid span');
    assert(cue.lines.length<=2&&cue.lines.every(line=>line.length<=72),name+': caption line readability');
  }
  const report=reports.find(r=>r.name===name),file=path.join(out,name+'-preview.mp4');
  const {stdout}=await run(process.env.FFPROBE||'C:/ffmpeg/bin/ffprobe.exe',['-v','error','-show_format','-show_streams','-of','json',file],{windowsHide:true});
  const p=JSON.parse(stdout),v=p.streams.find(s=>s.codec_type==='video'),a=p.streams.find(s=>s.codec_type==='audio'),sub=p.streams.find(s=>s.codec_type==='subtitle');
  assert(v.width===1920&&v.height===1080&&v.avg_frame_rate==='24/1'&&v.codec_name==='h264',name+': video contract');
  assert(a.codec_name==='aac'&&a.sample_rate==='48000'&&sub.codec_name==='mov_text',name+': audio/caption streams');
  assert(Number(p.format.duration)<=180&&(name!=='presentation'||Number(p.format.duration)>=120),name+': duration bounds');
  assert(Math.abs(Number(v.duration)-manifest.duration)<1/24+.001,name+': manifest/video timing drift');
  assert(Math.abs(Number(a.duration)-Number(v.duration))<1/24+.001,name+': audio/video drift');
  assert(Math.abs(Number(v.start_time||0)-Number(a.start_time||0))<1/24+.001,name+': audio/video start drift');
  assert(cues.at(-1).end<Number(v.duration),name+': captions overrun');
  assert(report.decode_errors==='',name+': decode errors');
  assert(Number.parseFloat(report.audio.max_volume)<-.1&&Number.parseFloat(report.audio.mean_volume)>-40,name+': clipping or silent track');
  for(const image of report.source_images){const scene=manifest.scenes.find(s=>s.id===image.id);assert(sha(await fs.readFile(scene.png))===image.sha256,name+': source image changed');}
  const critical=name==='demo'?manifest.scenes.filter(s=>['04-assumptions','06-eip-request','07-eip-text','09-devnet'].includes(s.id)).map(s=>({scene:s.id,start:s.start,end:s.end,first_caption:s.sentences[0].start,last_caption:s.sentences.at(-1).end})):[];
  results.push({name,status:'PASS',duration:Number(p.format.duration),video_duration:Number(v.duration),audio_duration:Number(a.duration),start_delta:Number(v.start_time||0)-Number(a.start_time||0),captions:cues.length,last_caption:cues.at(-1).end,phrases_exact_to_ms:true,source_matches_manifest:true,critical_scene_caption_spans:critical,mp4_sha256:sha(await fs.readFile(file)),srt_sha256:sha(Buffer.from(raw))});
}
await fs.writeFile(path.join(out,'validation-report.json'),JSON.stringify({validated_at:new Date().toISOString(),results},null,2));
console.log(JSON.stringify(results,null,2));