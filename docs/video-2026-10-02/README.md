# dtox research: local video previews

Date: 2026-10-02. Two English review drafts, not publication or submission assets approved by the owner.

Completed local previews: presentation 160.104667 seconds (2:40), demo 168.896333 seconds (2:49). Both are 1920x1080, 24 fps, H.264 with 48 kHz mono AAC, English Microsoft Zira narration, and external SRT. Full FFmpeg decode checks returned no errors. Contact sheets were extracted from the actual final MP4s and inspected. Inter and Roboto Mono were embedded before final frame generation and encoding, not patched after finished videos. Narration contains 317 and 341 words respectively. Main independently checked both output durations. No additional renders were performed after completion.

## Outputs

Generated files live only in `presentation/video-2026-10-02/`:

- `presentation-preview.mp4`: 1920x1080, 16:9, 24 fps, H.264, AAC narration.
- `demo-preview.mp4`: the separate product research walkthrough, same format.
- `presentation.srt` and `demo.srt`: external English subtitles.
- Per-video `*-thumbnail.png`, `*-contact-sheet.png`, and `*-narration.wav`.
- `qa-report.json`: actual durations, streams, decoded integrity, evidence hash.
- `evidence-selected.json`: exact selected fields and canonical source text used on screen.
- Per-video scene PNGs, WAVs, MP4 segments and render manifests.

## Rerender locally

No install, API key, paid service, telemetry, upload, deploy, signing, or git commit is required.

Run from the repository in a normal local PowerShell terminal:

```powershell
& 'C:\Program Files\nodejs\node.exe' --preserve-symlinks --preserve-symlinks-main docs/video-2026-10-02/render.mjs
```

The Node flags work around restricted Windows path resolution. Chrome is launched headless in a fresh temporary profile, not the user's authenticated browser. Background services and extensions are disabled. All page requests are aborted; fonts are embedded data assets and panels are rendered from strings. No live API call occurs during rendering.

Stages can be run separately:

```powershell
& 'C:\Program Files\nodejs\node.exe' --preserve-symlinks --preserve-symlinks-main docs/video-2026-10-02/render.mjs --frames-only
foreach ($videoName in @('presentation','demo')) {
  & 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -NoProfile -ExecutionPolicy Bypass -File docs/video-2026-10-02/voice.ps1 -ManifestPath (Join-Path $PWD ('presentation/video-2026-10-02/' + $videoName + '/manifest.json'))
}
& 'C:\Program Files\nodejs\node.exe' --preserve-symlinks --preserve-symlinks-main docs/video-2026-10-02/render.mjs --measure-only
& 'C:\Program Files\nodejs\node.exe' --preserve-symlinks --preserve-symlinks-main docs/video-2026-10-02/render.mjs --render-only
```

System.Speech voice activation is blocked by the current agent sandbox. Only the local narration commands need a reviewed outside-sandbox run there. The ordinary user's terminal can access Microsoft Zira Desktop. Rate is 1. No cloud speech or voice cloning is used.

Local dependencies: FFmpeg/ffprobe at `C:/ffmpeg/bin/`; bundled Playwright at `C:/Users/User/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright`; Sharp from the existing `.deck-build/node_modules/sharp`; installed Google Chrome. No package download is used.

FFmpeg encoding and filters use two threads. The animation vocabulary is a 0.9 percent slow zoom and a 0.3-second paper-color fade. No music, stock video, avatars or sound effects are added.

## Brand and composition

Shared with the existing deck: technical editorial Swiss composition, asymmetric whitespace, square corners, paper `#fafaf3`, ink `#1a1a1a`, restrained orange `#FF4D00`. Background shifts separate panels; no 1px layout borders. Headline size is 80px; metadata is at least 28px. Only ambient 4 percent / 24px shadows are used.

Inter and Roboto Mono are embedded locally. The two variable TTF files and their OFL licenses are in `fonts/`, downloaded once from the official [Google Fonts Inter directory](https://github.com/google/fonts/tree/main/ofl/inter) and [Roboto Mono directory](https://github.com/google/fonts/tree/main/ofl/robotomono). No global font installation. If font assets are absent, the script explicitly falls back to local Arial and Consolas and records that fallback in QA.

The video-craft and marketing-video skills informed the focal-point, negative-space, source-object framing and pacing decisions. Their telemetry and paid/installation workflows are not used, following the user's stricter scope.

## Evidence and honesty

The primary capture is `docs/data/2026-10-02-web3-demo.json`, containing three exact successful MCP responses. That file is read-only in this task. Its SHA-256 and capture time are preserved in the output. The optional `solana-response.json` in this source directory preserves the additional exact response provided by the coordinating agent from a read-only MCP call; its query arguments and date are documented separately in `evidence-selected.json`. No precise capture timestamp is invented for that extra response. All selected source fields and text remain unchanged; only this provenance description was clarified after rendering.

The MCP Response Viewer is an **editorial view of recorded responses**, not an existing product UI, an agent thought transcript, a continuous recording or newly executed calls. Raw selected source text is escaped for display, never rewritten. The large equation is a reviewed readable transcription; unchanged original LaTeX remains beneath it. The script checks that the canonical LaTeX matches before rendering. The six-equation/one-algorithm counts are validated against the capture, and the replay algorithm is not shown or implemented.

The Solana result contains `acquisition_type=latex`, `extraction_type=markdown_source`, and `exact_latex=false`. The captured field is retained unchanged; its acquisition tag is not an editorial claim that Markdown is original LaTeX.

Corpus counters use the verified snapshot in SUBMISSION.md, `2026-10-02T14:00Z`. They are dated research-index counters, not users, market share, traction or revenue. The local `site/stats.json` placeholder is not used as evidence.

Historical on-chain references come from SUBMISSION.md: September 29, team-controlled, Solana devnet, on different claims. No fresh transaction or MEV verdict is created. A signature proves authorship, not truth or independence. Wallet-only agreement remains pending; trusted-model diversity is required for registry agreement. Base, Ethereum and Arbitrum remain configured testnets only.

No release, mainnet readiness, benchmark, external feedback, graph candidate result or scientific-consensus claim is made. The two verified P1 checks plus health tests (47/47) and API tests (19) were reported by main; the videos do not turn these local results into runtime release approval. Repeated spoken caveats are consolidated into the closing boundary scene. Historical and provenance labels remain visible on the relevant shots.

SRT cues are aligned to each measured scene WAV and proportionally timed by sentence word count. They are preview subtitles, not phoneme-aligned transcription. Listen and adjust individual cue boundaries before public use.

## Manual owner gate

- Review both actual MP4 files, narration pronunciation, pacing and SRT alignment.
- Supply team, contact, background, eligibility and prior-work/funding disclosure. See `application-preview.md` for the scoped application supplement.
- Confirm creative approval and the deployment status separately with main.
- Upload manually only after approval, then insert the two actual public URLs into the owner-controlled application. This task performs neither upload nor submission.
- Old PPTX files, existing deck-build assets and root application docs were not overwritten.
