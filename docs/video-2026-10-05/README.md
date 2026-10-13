# Local video build — 2026-10-05

This build creates two separate English videos for the Crypto World's Fair review package, using local Chrome, Windows speech synthesis and ffmpeg. No external paid service is required.

- `presentation/video-2026-10-05/presentation-preview.mp4`: nine final jury main slides, in their original order; appendix slides are excluded.
- `presentation/video-2026-10-05/demo-preview.mp4`: ten-scene editorial response viewer of actual read-only MCP captures dated October 2 and October 5, plus a separately labeled historical October 3 devnet record. It is not a continuous live recording or a screenshot of the product UI.
- Matching `presentation.srt` and `demo.srt`, English subtitle tracks embedded in the MP4s, narration WAV/TXT, manifests, thumbnails, contact sheets and `qa-report.json` are retained beside the movies. The local review page and browser VTT tracks are supplied by the coordinating task.

The pitch video uses the separate, condensed [pitch-narration.mjs](pitch-narration.mjs): 292 words. It follows all nine main-slide notes while retaining assumptions, comparison limitations, free beta status, inventory-versus-traction distinction, wallet attribution boundaries and the unconfirmed pilot proposal. The original deck and speaker notes are unchanged. The voice is explicitly local synthetic Microsoft Zira Desktop at its normal rate (`Rate = 0`), not a founder recording or a cloned voice.

The demo [story.mjs](story.mjs) contains 322 spoken words. Its bid-strategy equation uses `PA` exactly as captured on October 5, distinct from the October 2 expected-utility example. Parser completeness remains unknown; the backend source URL remains unverified; official-source checking is separately attributed; duplication and truncation in the associated section are disclosed. The ERC-4337 excerpt is source retrieval, not an audit. Full-text availability is per-record provenance; `corpus.as_of` is observation time. The October 3 proof is one `does_not_assert` reading with `read_once`, one reader and no quorum.

Caption timing uses each phrase's separately synthesized and ffprobe-measured WAV, then adds the actual leading silence and phrase gaps. It is not a proportional estimate over an entire scene. Each manifest records the original spoken text, pronunciation text, measured phrase start/end, scene duration and source hashes. The full MP4 decode and audio-level checks are recorded in the final QA report; every scene is inspected in an extracted movie frame.

Run from the repository root:

```powershell
node docs/video-2026-10-05/render.mjs --prepare
node docs/video-2026-10-05/render.mjs --render-only
```

`--frames-only` prepares frames/manifests, `--measure-only` measures existing phrase WAVs, and the default invocation prepares and renders both movies. The renderer aborts Chrome network requests and makes no MCP/RPC requests. It never pays, signs, mutates a registry, publishes or submits anything. Input captures are retained in `source-captures/`; the historical devnet record is sourced from the two dated release-evidence documents rather than repeated.

The defaults reference available runtime dependencies on this computer. Override `PLAYWRIGHT_MODULE`, `SHARP_MODULE`, `CHROME`, `FFMPEG` and `FFPROBE` when rebuilding elsewhere. Locally embedded Inter and Roboto Mono fonts retain their OFL licenses. Historical October 2 videos are preserved in their original folder and are not overwritten by this build.

Final measured MP4 durations after the UTF-8 voice correction: pitch 168.479667 seconds; demo 171.229667 seconds. Independent validate.mjs passed both movies, including exact source/manifest/TXT speech, measured caption anchors, source-image hashes, audio/video drift and media bounds. See validation-report.json and the coordinating task's video-work-report.md. Use voice.ps1's explicit UTF-8 input; old pitch bytes measured 170.021333 seconds are superseded.