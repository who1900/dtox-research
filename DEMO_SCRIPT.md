# dtox research: current English video scripts

Updated 2026-10-05. Local production notes, not authorization to upload or submit. The current jury deck has 9 main slides and 2 appendix slides; appendix slides are not narrated.

The [official fall-2026 FAQ](https://colosseum.com/hackathon?year=fall2026) requires a separate 2-3 minute presentation/pitch video and a product-demo video of no more than 3 minutes. The submission cutoff is October 12 at 23:59 Pacific (PDT), equivalent to October 13 at 06:59 UTC / 11:59 Asia/Almaty. Registration and eligibility remain unchecked. See [SUBMISSION.md](SUBMISSION.md) for owner fields and development disclosures.

## Current sources and outputs

- Pitch basis: the 9 main paragraphs in [jury speaker notes](presentation/pitch-visual/speaker-notes.md), excluding headings, sources and appendices. The video uses a concise spoken adaptation at a natural pace, targeting 165-175 seconds; the final narration and timing are recorded with the current production source. Preserve the final deck's early customer, actual source response, documented-capability comparison, founder, proposed pilot and evidence boundaries.
- Demo narration and scene definitions: [October 5 story source](docs/video-2026-10-05/story.mjs). The English voice text is reproduced below for review.
- Read captures: [actual MCP excerpts](presentation/pitch-visual/mcp-demo-excerpts.json) and [pitch evidence ledger](presentation/pitch-visual/evidence.md).
- Existing chain proof: [October 3 payment release gate](docs/2026-10-03-payment-release-gate.md) and [October 3 live release evidence](docs/2026-10-03-live-release-evidence.md).
- Current local render paths: [presentation MP4](presentation/video-2026-10-05/presentation-preview.mp4), [presentation SRT](presentation/video-2026-10-05/presentation.srt), [demo MP4](presentation/video-2026-10-05/demo-preview.mp4), [demo SRT](presentation/video-2026-10-05/demo.srt). Final render metadata and timing are recorded below; the historical runtimes do not establish current runtime.

Both videos are editorial presentations of actual captured responses and dated records. They are not continuous live MCP-client recordings. The BEV question is presenter framing, not a captured end-to-end natural-language discovery query. The pitch uses September 29 historical payment/SAS examples from its existing evidence ledger; the new demo separately shows the October 3 payment and one test reading. Keep those dates and evidence classes visible.

## Read-only workflow

Public research beta is free, without a wallet, payment, key or signup. Connection examples:

```bash
codex mcp add dtox --url https://read.whoim.space/mcp
claude mcp add --transport http dtox https://read.whoim.space/mcp
```

Other MCP clients: `{"type":"http","url":"https://read.whoim.space/mcp"}`. The earlier installed Codex CLI help check accepted `NAME --url URL`; that check did not change client configuration.

The public inventory is 13 tools, including `get_research_bundle`. Twelve are read/preview paths; `record_signed_verdict` is a protected signed write. The bundle is a bounded reading packet, not a payment, registry mutation, scientific review or verdict. This production task does not call signed-write tools or create a fresh proof.

Captured calls shown in the demo:

```text
# Historical known-paper discovery: 2026-10-02
find_papers(query="Quantifying Blockchain Extractable Value", layer="web3", limit=3)

# Actual read-only source captures: 2026-10-05
get_code_or_math_spec(arxiv_id="2101.05511", target_elements="equation", max_chars=4500)
read_paper_section(paper_id="2101.05511", section_title="Incentive Compatibility of the Relay auction Participation", max_chars=2200)
read_paper_section(paper_id="eip:4337", section_type="limitations", max_chars=1800)
```

Evidence to retain on screen:

- October 2 known-paper discovery ranked `2101.05511` first. This does not establish that an arbitrary research question is solved; omit a changing citation count from the voiceover.
- October 5 math response: 6/6 equations, 1,245 characters, not truncated; selected bid strategy `b_i^{PA}(O,S_i)=((n-1)/n) R_i(O)`. Preserve `PA` and its section identity.
- Original LaTeX is exact for this object, with `parse_completeness=unknown` and `source_url_status=source_not_verified`. The official source was opened and manually checked separately; do not attribute that check to the backend.
- Assumption section: 2,200 of 3,087 characters, next offset 2,200, duplicated prose/artifact and truncation. The source model assumes independent rewards from a common uniform distribution and known participant count/distribution. Suitability for a Solana auction is an engineering question, not established by the extraction.
- ERC-4337 limitations: 1,205 characters, no next page, CC0 specification Markdown, not original LaTeX. Source text identifies risk concentrated in EntryPoint. The trust-boundary/audit-plan consequence is presenter interpretation, not a dtox audit.
- Coverage is per record/provenance. arXiv can be abstract-only; IACR can contain recovered open-access full text. Corpus counts are dated inventory, not traction. `corpus.as_of` is an observation time, not a snapshot identifier or reproducibility guarantee; not every read includes it.

## Video 1: jury presentation

Actual video wording: [concise pitch source](docs/video-2026-10-05/pitch-narration.mjs) and [final narrated text](presentation/video-2026-10-05/presentation-narration.txt). The 292-word video adaptation preserves all 9 main-slide topics; the original editable deck and speaker notes remain unchanged.

Use the [speaker notes](presentation/pitch-visual/speaker-notes.md) as the narrative basis, with the concise video adaptation in the production source. The deck's original approximately 2:40 estimate is a planning figure. The final video falls within its preferred 165-175-second target at normal voice rate.

The 9-slide order is: protocol engineer's decision; real auction equation and assumptions; ERC-4337 trust boundary; evidence workflow; alternatives on consistent capability criteria; free beta and dated inventory; historical Solana demonstrations; founder; proposed pilot. The pilot targets 3 teams over 14 days, with success defined as at least 2 teams producing a source/assumption/consequence memo and returning with a second question. No participants are confirmed.

The competitor matrix compares documented capabilities only; no measured accuracy, speed, coverage or win is claimed. Paid heavy usage/transport remains a hypothesis; the same source bundle remains free. No users, revenue, scientific consensus or mainnet readiness are inferred from corpus or chain evidence.

## Video 2: product demo narration

These are the current ten scenes from `story.mjs`. Scene numbers are editorial order, not measured timecodes. Exact SRT/timing is generated from the final audio and render.

### 1. Read before choosing a design

Screen: client-facing research question and persistent editorial capture label; keep October 2/5 dates explicit.

> We need an auction formula, its assumptions, and a wallet trust boundary. This editorial viewer uses real read only MCP captures from October second and fifth, not a continuous live recording.

### 2. Find the named primary paper

Screen: the October 2 `find_papers` arguments and known-paper result, with source identity and date.

> The October second request names Quantifying Blockchain Extractable Value and selects Web3. The paper ranks first by title match. This known paper lookup does not solve an arbitrary research question.

### 3. Request a bounded source object

Screen: October 5 math request, returned/available 6/6, 1,245 characters and selected raw LaTeX object; source link remains visible.

> The October fifth math request asks for equations within forty five hundred characters. Six of six return without truncation. The bid strategy retains original LaTeX and section identity.

### 4. A formula needs its assumptions

Screen: readable `PA` bid strategy, the short source assumption excerpt and separately labelled engineering check.

> The bid is n minus one over n, times reward. The model assumes independent rewards from a common uniform distribution and known participant count. Check assumptions before reuse: that is engineering inference. Solana applicability needs review.

### 5. Inspect the extraction boundary

Screen: provenance fields, unknown parser completeness, unverified backend source URL, manual source check and duplicated/truncated section markers.

> Exact LaTeX is true here; parser completeness is unknown. The backend source URL is unverified; the pitch author checked it separately. The related section contains duplicated prose and truncation. Those limitations remain visible.

### 6. Read the specification's limitations

Screen: actual ERC-4337 section arguments and 1,205-character Markdown response, CC0/source URL and no next offset.

> For E R C forty three thirty seven, request limitations within eighteen hundred characters. Twelve hundred five characters return from specification Markdown, under C C zero. This section reports no next page.

### 7. EntryPoint concentrates security risk

Screen: readable author-stated EntryPoint excerpt, followed by a separately labelled presenter implication.

> The authors name concentrated risk in EntryPoint. Include that dependency in the wallet trust boundary and audit plan. The text is evidence; this design check is presenter interpretation, not an audit.

### 8. Coverage follows provenance

Screen: 13-tool inventory and `get_research_bundle`, free public endpoint, per-record full-text/abstract markers and observation-time boundary.

> The free beta has thirteen public MCP tools, including a read only research bundle. arXiv can be abstract only; I A C R can have recovered full text. Corpus observation times are not reproducible snapshot identifiers.

### 9. Separate historical devnet proof

Screen: persistent `Historical | 2026-10-03T09:55:43.992Z | Solana devnet | One authorized test reading` label, dated release-document references, the existing PDA and `does_not_assert` / `read_once` / `readers=1` / `quorum=false` fields. The complete public Explorer links are retained in the proof-card notes below.

> Separately, the October third devnet run made one payment and one signed test. Its verdict: does not assert. Its status: read once, one reader, not quorum. Signatures attribute wallets, not scientific truth or independent review. Paid mainnet remains no go.

### 10. Bring the source into your decision

Screen: free connection command, source links and code repository. No signing/payment interaction.

> Connect your agent to the free beta. Inspect sources, assumptions and provenance, then decide what applies. No payment or signature is required for public reads.

## Existing October 3 proof card

Use the existing documented record; no payment, signature, registry mutation or chain write is performed by the video build.

- Claim: `Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack`; paper `eip:4337`; verdict `does_not_assert`.
- Registry: `read_once`, one reader, one `does_not_assert`; `judged_by_model=unspecified` does not supply trusted-model diversity. Never force `confirmed_prior_art`.
- [Payment transaction](https://explorer.solana.com/tx/31iL5AZXhVpBFVDF2Je42C3uZAirzk2Hg9LpLtLBXd3mw5w8inWYwbvgoYVCW3t4zxMrbTVZVsHcoxsukf8adu8J?cluster=devnet).
- [Attestation transaction](https://explorer.solana.com/tx/4wwnu49FV5gSshoXyfBteZZhEod5wcNSuQsyBx5e148ESpupQyPQQ47Upzip1tLgjoVQ1AaEyahtpXRK8qysJ3x3?cluster=devnet); PDA `G1BBNygRmArfr4Ut7s3D84SBBwsgSQPRxv6uy6dnFH6b`.
- Evidence fragment hash: `335696d9c582a3285a719928621789cf8ce38438f51d39b8905ed124b0d0978a`. This is the context-bound evidence identity used by the API, not an immutable corpus snapshot or text-only checksum.
- Independent public RPC readback verified devnet genesis, SAS owner, record fields, Ed25519 signature and finalized `err=null` transactions. Payment slot `506956512`; attestation slot `506956519`; charged amount exactly 0.01 devnet USDC.
- The public MCP response does not expose the complete attestation record. No internal token or private endpoint is used here. The run did not test paid reads, a live invalid signature, replay, batch, retry or a second payment/write.

The minimum tested devnet path passed. Paid mainnet and full production remain NO-GO; EVM settlement is unverified, and RAM headroom/full application-Qdrant restore remain production gates. These are source-specific historical observations, not a new release decision.

## Historical October 2 previews and checks

Retained with their dates; do not substitute them for October 5 media validation:

These archival media remain in the original local project tree. They were located and checked separately; they are not copied into the current October 5 output directory. The repository-relative archive paths below identify those existing objects.

- [Historical presentation MP4](presentation/video-2026-10-02/presentation-preview.mp4): 160.104667 seconds (2:40); [SRT](presentation/video-2026-10-02/presentation.srt), [contact sheet](presentation/video-2026-10-02/presentation-contact-sheet.png).
- [Historical demo MP4](presentation/video-2026-10-02/demo-preview.mp4): 168.896333 seconds (2:49); [SRT](presentation/video-2026-10-02/demo.srt), [contact sheet](presentation/video-2026-10-02/demo-contact-sheet.png).
- Main-reported metadata: 1920x1080, 24 fps, H.264, AAC 48 kHz mono. All 20 contact-sheet frames had no overflow; audio mean -21 dB, max -2.5 dB, no clipping. Owner pronunciation/SRT review remained pending.
- The earlier demo was also an editorial viewer of recorded responses. Its actual story remains [October 2 story source](docs/video-2026-10-02/story.mjs), with [historical read notes](docs/2026-10-02-web3-demo.md).
- October 2 math capture returned 7/7 equation/algorithm objects, 2,211 characters without truncation; the active October 5 math capture asks for equations only and returns 6/6, 1,245 characters. Do not merge those response counts.
- October 2 bounded hardening checks: attestor 47/47 in 2.4 seconds, targeted API 19 passes in 1.65 seconds with three pre-existing DeprecationWarnings, TypeScript check and attestor build passed. Scoped deployment/health and free bundle smoke were reported. Bundle `partial=true` coexisted with `search_partial=false`; budget clipping/absent object classes did not establish complete parsing.
- October 2 negative-offset validation returned an error payload for HTTP 422 while the MCP envelope had `isError=false`. It was a validation rejection, not a successful read. Four pre-edit source hashes matched HEAD. Details remain in [the hardening follow-up](docs/2026-10-02-registry-hardening.html).
- October 3 gateway 38/38 and attestor 47/47 plus TypeScript checks are a separate dated local/mock validation set; helper 3/3 overlaps gateway 38 and is not additive. No current full-suite or independent scientific quorum is claimed.

## Local completion and publish gate

Final October 5 media: pitch **168.479667 seconds** (9 scenes, 292 spoken words); demo **171.229667 seconds** (10 scenes, 322 spoken words). Both are separate English H.264 1920x1080/24 fps MP4s with AAC 48 kHz mono, embedded English subtitles and matching external SRT/VTT. Local synthetic Microsoft Zira Desktop uses normal rate 0; no external paid service. UTF-8 was made explicit for the pitch's apostrophes before the final render.

Validation: [media QA](presentation/video-2026-10-05/qa-report.json), [independent timing/source checks](presentation/video-2026-10-05/validation-report.json) and [package QA](docs/2026-10-05-package-qa.md). Caption text matches the source and manifest phrase by phrase, with measured phrase-WAV anchors, no overlaps, at most two lines and no overrun. Full MP4 decoding found no errors; audio levels are below clipping. All 19 scene frames and all 11 deck PDF pages were visually reviewed. Correct UTF-8 inputs and phrase duration were checked; independent ASR/listening transcription was unavailable locally. Owner review of specialist pronunciation remains pending.

Owner review remains required for founder/team facts, pronunciation, final public URLs, registration/eligibility, track, prior work and actual funding history. The owner requested private Library delivery separately. Local creation does not authorize public upload, push, submission, deployment or a payment. Preserve all existing historical assets and unrelated work.
