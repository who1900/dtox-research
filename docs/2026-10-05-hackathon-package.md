# dTox Research: Crypto World's Fair local package

Prepared 2026-10-05 against local `main` at `1aa0b5d9cbe60186a536b800c602efee93ce48e7`, including the completed, uncommitted jury-deck revision. This is a local review package, not an accepted submission or public release. The [October 3 package](2026-10-03-hackathon-package.md) and its dated results remain historical evidence.

## Materials

| Material | Local file | Status |
| --- | --- | --- |
| Editable presentation | [Jury PPTX](../presentation/pitch-visual/dtox-research-jury.pptx) | Completed by the existing DTOX editor: 9 main slides + 2 appendices |
| Presentation PDF | [Jury PDF](../presentation/pitch-visual/dtox-research-jury.pdf) | Matching export |
| Browser presentation | [HTML](../presentation/pitch-visual/index.html) | Local file; source slides preserved |
| Spoken pitch | [Speaker notes](../presentation/pitch-visual/speaker-notes.md) | Source for the 9 main video scenes; appendices are not spoken |
| Pitch video | [MP4](../presentation/video-2026-10-05/presentation-preview.mp4), [SRT](../presentation/video-2026-10-05/presentation.srt) | Final local English render: 168.479667 s; normal-rate synthetic voice |
| Product demo | [MP4](../presentation/video-2026-10-05/demo-preview.mp4), [SRT](../presentation/video-2026-10-05/demo.srt) | Final local render: 171.229667 s; editorial saved-response viewer |
| Demo instructions | [Demo script](../DEMO_SCRIPT.md) | Read-only BEV/ERC-4337 path with separate dated devnet proof |
| Submission copy | [SUBMISSION](../SUBMISSION.md) | Current product claims with dated historical evidence |
| Requirements and unresolved fields | [Checklist](2026-10-05-submission-checklist.md) | Registration and eligibility remain unchecked |

The October 2 videos were found locally before this revision: presentation **160.104667 s**, demo **168.896333 s**, both with SRT and source assets in `presentation/video-2026-10-02`. They remain historical editorial previews. Neither old nor new local videos supplies a public video URL. Private ChatGPT Library delivery was explicitly requested. No public upload, submission, commit, push, deployment, access change, payment, signature or registry write was performed for this package.

Final October 5 media: pitch **168.479667 seconds** (9 scenes, 292 spoken words); demo **171.229667 seconds** (10 scenes, 322 spoken words). Both are separate English H.264 1920x1080/24 fps MP4s with AAC 48 kHz mono, embedded English subtitles and matching external SRT/VTT. Local synthetic Microsoft Zira Desktop uses normal rate 0; no external paid service. UTF-8 was made explicit for the pitch's apostrophes before the final render.

See [package QA](2026-10-05-package-qa.md) and [local video review](2026-10-05-submission-review.html). SRT and browser VTT use measured phrase timings. Automated audio checks passed; owner specialist-pronunciation review remains pending.

## Copy-ready product description

**Project:** dTox Research  
**Tagline:** Source-backed Web3 research through your AI agent.  
**Positioning:** Developer infrastructure; final ecosystem track is an owner decision.

A protocol engineer choosing an auction rule needs the paper's assumptions, not just a promising abstract. dTox helps Web3 teams inspect source evidence through their AI agents before they make that design decision. Its free public MCP beta retrieves research papers and protocol specifications, source sections, equations and author-stated limitations when those materials are available.

The demo uses a real BEV auction-model response: a bid formula comes with assumptions about reward distribution and participant count. It also reads ERC-4337's discussion of security risk concentrated in EntryPoint. The practical output is a source, an assumption and an engineering check. The engineering consequence is an interpretation to review, not an automated audit or a claim that an Ethereum model applies unchanged to Solana.

Thirteen public MCP tools expose the research workflow, including bounded research bundles; twelve are read or preview tools and one is a protected signed-write tool. Coverage depends on each record's acquisition and extraction provenance. arXiv records can be abstract-only; IACR records can contain recovered open-access full text. Corpus counters describe dated inventory, not users, revenue or universal full-text availability. An observation time is not a reproducible index snapshot.

Solana supports optional USDC x402 payments and wallet-attributed SAS reading records. A dated October 3 devnet test demonstrates one paid signed-write path; its claim remains `read_once` with one `does_not_assert` reading. Payment and signatures do not prove scientific truth or independent consensus. Mainnet is not approved, and EVM settlement validation remains pending.

The beta stays free. Paid intensive usage and team integrations are plans, not demonstrated revenue. We seek three protocol teams for a proposed fourteen-day pilot. Each brings one real design decision and produces a memo connecting evidence to an engineering consequence. The proposed success criterion is two teams completing the memo and returning with a second research question. No pilot participants are confirmed.

## Read-only judge path

Public product: [research interface](https://read.whoim.space/research/)  
Public MCP: [https://read.whoim.space/mcp](https://read.whoim.space/mcp)  
Repository: [who1900/dtox-research](https://github.com/who1900/dtox-research), AGPL-3.0.

```bash
codex mcp add dtox --url https://read.whoim.space/mcp
claude mcp add --transport http dtox https://read.whoim.space/mcp
```

These commands configure the judge's local client. Ask the agent to discover the target paper first, read the evidence, and report provenance and limitations. No wallet or payment is needed for this path.

```text
find_papers(query="Quantifying Blockchain Extractable Value", layer="web3", limit=3)
get_code_or_math_spec(arxiv_id="2101.05511", target_elements="equation", max_chars=4500)
read_paper_section(paper_id="eip:4337", section_type="limitations", max_chars=1800)
get_research_bundle(query="EIP-4337", layer="web3", limit=1, max_chars=2000, strict=true)
```

Current retrieval can change. Inspect the actual returned markers instead of assuming the recorded response will recur. See [technical setup and safety](technical-reference.md#running-it), [judges' guide](judges-guide.md), and [evidence ledger](../presentation/pitch-visual/evidence.md).

## Evidence dates and limits

| Date | Observation | Limit |
| --- | --- | --- |
| 2026-10-05 | Jury BEV capture: 6/6 equations, 1,245 chars, not truncated; selected bid formula and source assumptions | `exact_latex=true` applies to this captured source; parser completeness unknown, source URL status unverified; the related section contains duplication and truncation |
| 2026-10-05 | ERC-4337 section: 1,205 chars, full specification Markdown, CC0 | Source security discussion, not a dTox audit |
| 2026-10-05T10:00Z | README observation: 180,834 documents, 17,365 Web3-tagged records, 10,801,902 chunks, 3,388,476 citation edges | Dated inventory; overlapping tags, variable coverage, no traction or frozen snapshot claim |
| 2026-10-03T09:55:43.992Z | One authorized devnet payment and signed test write; independent RPC readback reported passed | Historical, main-attributed report, not repeated here; `read_once`, one `does_not_assert`, no scientific quorum |
| 2026-09-29 | Team-controlled SAS readings and historical devnet x402 payment examples used in pitch | Separate examples concerning different AI claims; do not combine them into one joined event |
| 2026-10-02 | Earlier MCP capture and preview videos | Historical responses; older counters and timings are not current guarantees |

The October 3 claim was: "Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack." Paper `eip:4337`; verdict `does_not_assert`. The reported payment was exactly **0.01 devnet USDC**, with payment finalized at slot `506956512` and attestation at `506956519`. `judged_by_model=unspecified` does not establish a trusted model identity.

- [October 3 payment transaction](https://explorer.solana.com/tx/31iL5AZXhVpBFVDF2Je42C3uZAirzk2Hg9LpLtLBXd3mw5w8inWYwbvgoYVCW3t4zxMrbTVZVsHcoxsukf8adu8J?cluster=devnet)
- [October 3 attestation transaction](https://explorer.solana.com/tx/4wwnu49FV5gSshoXyfBteZZhEod5wcNSuQsyBx5e148ESpupQyPQQ47Upzip1tLgjoVQ1AaEyahtpXRK8qysJ3x3?cluster=devnet)
- [Payment release gate](2026-10-03-payment-release-gate.md) and [live-release evidence](2026-10-03-live-release-evidence.md)

Keep the dated historical suite results in the technical reference and release reports. This package does not claim a new full product test run, a current paid E2E run, measured comparative superiority, independent reviewers, or actual pilot demand.

The completed jury deck remains unchanged. Its Appendix B phrase "Latest paid E2E and mainnet" is interpreted narrowly here: paid reads, failure/replay/batch behavior and production/mainnet readiness still require validation. It must not erase the October 3 minimum devnet payment-and-write proof. The pitch's September 29 examples remain distinct from the demo's explicitly dated October 3 proof. Appendix speaker notes summarize extraction limits; the evidence ledger records the captured section's duplication/truncation in full.

## Owner decisions before any public action

1. Confirm competition registration, eligibility, team roster/location and final track.
2. Confirm prior-work chronology and list work completed during September 14 through the deadline. Existing notes say development began July 31; the owner must verify this disclosure and actual funding/sponsorship history.
3. Review narration pronunciation and final captions, then choose the approved two video versions and hosting links.
4. Select the repository revision and exact files for any later commit/public release, preserving unrelated work.
5. Authorize upload and submission separately. Preparation of these local files does not authorize either action.
