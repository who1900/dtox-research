# dtox research: Crypto World's Fair submission
Updated 2026-10-05. Application draft, not release approval. Evidence dates below are intentional.

## One-liner

Before choosing an auction rule, protocol teams can use dtox to inspect relevant research, source equations, assumptions, and protocol limitations through MCP.

## Application description

A protocol engineer choosing an auction rule needs more than a list of papers. Before a team builds, it needs the mechanism's equation, assumptions, related work and the passages where researchers or specification authors state what can fail. dtox gives builders and agents a bounded reading interface over research papers, protocol specifications and technical whitepapers.

The Web3-first demo retrieves the MEV auction model from "Quantifying Blockchain Extractable Value: How dark is the forest?" and the security considerations of ERC-4337. The first is original LaTeX; the second is specification text. Both retain source links and acquisition/extraction provenance. These are retrieved source objects, not generated security advice. The pair supports research across ecosystems; it does not establish that execution or security assumptions are identical across chains.

Thirteen public MCP tools let an agent find a paper, read its sections, fetch equations and algorithms, or assemble a character-budgeted research bundle. Public MCP access is free beta with no key or signup. Dense retrieval, BM25 and citation-graph traversal support discovery; retrieved candidates are not automatically confirmed prior art.

Solana adds optional x402 payments and SAS wallet-attributed readings. The September 29 examples are separate historical events about different AI claims. A later, authorized October 3 devnet payment and signed ERC-4337 test record were independently read back through public RPC. That one reading is `does_not_assert` with registry status `read_once`, not an independent quorum. A signature proves wallet attribution, not truth or independent reasoning. Base, Ethereum and Arbitrum have test-network configuration only; their paid end-to-end settlement validation is pending.

Designed for Web3 protocol teams and agent frameworks. The public research beta and the same source bundle remain free. Optional paid transport or intensive usage is a business hypothesis, not exclusive sources, better evidence quality, a launched tier, or revenue. No pilot participants are confirmed.

## Contest and required assets

The [current event](https://colosseum.com/worldsfair) runs September 14 to October 12, 2026. Deadline: October 12, 2026 at 23:59 Pacific (PDT), equivalent to October 13 at **06:59 UTC / 11:59 Asia/Almaty**. Each teammate must register and the leader must submit before the cutoff; Colosseum's clock controls. [Official rules, sections 5-6](https://colosseum.com/legal/Crypto%20World%27s%20Fair%20Hackathon%20Rules.pdf).

The [official fall-2026 page and FAQ](https://colosseum.com/hackathon?year=fall2026) require a 2-3 minute presentation and a separate product-demo video of no more than 3 minutes. Submission content must be English. FAQ judging covers founder-market fit, insight, execution, market, communication, viability and traction; no official percentage weights are asserted. Earlier development, actual funding history, team background and location must be disclosed. Registration, eligibility and the signed-in submission fields have not been checked.

The current [jury deck](presentation/pitch-visual/README.md) is complete: 9 main slides and 2 appendix slides, with matching PPTX, PDF and HTML exports. Its [speaker notes](presentation/pitch-visual/speaker-notes.md) are the pitch narration source; appendix notes are for questions. The proposed 2:40 estimate at about 130 words per minute is not measured recording time. Current video outputs belong in `presentation/video-2026-10-05`; final media timings and visual/audio QA are recorded in the [current script and production notes](DEMO_SCRIPT.md). Both videos are editorial presentations of captured source responses and dated evidence, not continuous live-session recordings.

Final October 5 media: pitch **168.479667 seconds** (9 scenes, 292 spoken words); demo **171.229667 seconds** (10 scenes, 322 spoken words). Both are separate English H.264 1920x1080/24 fps MP4s with AAC 48 kHz mono, embedded English subtitles and matching external SRT/VTT. Local synthetic Microsoft Zira Desktop uses normal rate 0; no external paid service. UTF-8 was made explicit for the pitch's apostrophes before the final render.

Validation: [media QA](presentation/video-2026-10-05/qa-report.json), [independent timing/source checks](presentation/video-2026-10-05/validation-report.json) and [package QA](docs/2026-10-05-package-qa.md). Owner review of specialist pronunciation remains pending.

The following October 2 previews are historical assets and reflect the earlier narrative. Main independently verified their media metadata: 1920x1080, 24 fps, H.264, AAC 48 kHz mono. These durations do not certify the October 5 render. Owner review and separately authorized upload remain pending. No public video URLs, public upload or form submission are claimed or authorized; private Library delivery was requested separately.

- [Presentation preview](presentation/video-2026-10-02/presentation-preview.mp4): 160.104667 seconds (2:40); [SRT](presentation/video-2026-10-02/presentation.srt), [contact sheet](presentation/video-2026-10-02/presentation-contact-sheet.png).
- [Demo preview](presentation/video-2026-10-02/demo-preview.mp4): 168.896333 seconds (2:49); [SRT](presentation/video-2026-10-02/demo.srt), [contact sheet](presentation/video-2026-10-02/demo-contact-sheet.png).

Historical October 2 QA: main inspected all 20 contact-sheet frames with no overflow and checked provenance/history labels; metadata and durations (160.104667/168.896333 seconds) were independently verified; audio mean -21 dB, max -2.5 dB, no clipping. Owner manual pronunciation and SRT timing review remained pending. The historical read workflow is retained in [October 2 demo notes](docs/2026-10-02-web3-demo.md); the active narration is in `DEMO_SCRIPT.md`.

## Try the free beta

```bash
claude mcp add --transport http dtox https://read.whoim.space/mcp
```

Codex:

```bash
codex mcp add dtox --url https://read.whoim.space/mcp
```

Main verified the installed Codex CLI with `codex mcp add --help`: it accepts `NAME --url URL`. No client configuration was changed.

Other MCP clients: `{"type":"http","url":"https://read.whoim.space/mcp"}`. Start with the three read-only calls below. No payment or wallet is needed for this research demo.

Public tools (13): `search_research_paper`, `find_papers`, `get_paper`, `read_paper_section`, `count_papers`, `similar_papers`, `get_code_or_math_spec`, `compare_methods`, `research_trends`, `validate_project`, `get_research_bundle`, `get_verdict_message`, `record_signed_verdict`. Twelve are read/preview paths; `record_signed_verdict` is a protected signed write and is not called for this read-only demo.

`get_research_bundle` is a bounded read packet, not a verdict write, payment or automatic scientific review.

## Earlier verified Web3 demo evidence (2026-10-02)

Tool calls, using MCP argument names:

```text
find_papers(query="Quantifying Blockchain Extractable Value", layer="web3", limit=3)
get_code_or_math_spec(arxiv_id="2101.05511", target_elements="equation,algorithm", max_chars=5500)
read_paper_section(paper_id="eip:4337", section_type="limitations", max_chars=3500)
```

- Discovery ranks `2101.05511` first: full title above, 2021 IEEE S&P, citation_count 364 in this response, `fulltext=true`, `acquisition=latex`, `extraction=latex_source`, `exact_latex=true`. Citation counts can change.
- The spec call returns 7/7 objects: one Transaction Replay Algorithm and six equations, 2,211 characters, `truncated=false`. Show the BEV Auction Modeling equation from the response, not replay-attack implementation. Readable notation: `E[u_i^PA|alpha]=(1-alpha) Pr^PA(O,S_i) R_i(O)-b_i^PA(O,S_i)`. Original source: [arXiv 2101.05511](https://arxiv.org/abs/2101.05511).
- ERC-4337 limitations return 1,205 characters of CC0 Markdown text, `exact_latex=false`. The indexed passage addresses EntryPoint risk concentration and hijacking/fee-draining safety requirements. It is the spec authors' text, not dtox-generated advice. [ERC-4337 source](https://eips.ethereum.org/EIPS/eip-4337).

These checks establish this paper/spec pair, not corpus-wide full-text coverage or chain equivalence.

## Fresh jury-story source checks (2026-10-05)

Safe MCP calls supplied for this revision:

```text
get_code_or_math_spec({"arxiv_id":"2101.05511","target_elements":"equation","max_chars":4500})
read_paper_section({"paper_id":"2101.05511","section_title":"Incentive Compatibility of the Relay auction Participation","max_chars":2200})
read_paper_section({"paper_id":"eip:4337","section_type":"limitations","max_chars":1800})
```

- BEV equation response: 6/6 equations, 1,245 characters, not truncated; full-text acquired from LaTeX source, `exact_latex=true`, `parse_completeness=unknown`, and `source_url_status=source_not_verified`. Equation 4 returns the bid strategy `b_i^{PA}(O,S_i)=((n-1)/n) R_i(O)` in the paper's notation. Preserve `PA`; do not change it to `RA`. The source URL was independently opened and manually checked against the equation. Do not claim the MCP independently verified its own source URL or parser completeness. The deck's research question is presenter framing, not a captured end-to-end natural-language discovery query.
- Assumption section: 2,200 of 3,087 characters, with a duplicated paragraph/artifact and truncation. Paraphrase the model assumptions; do not present this extraction as perfect.
- EIP-4337 limitations: 1,205 characters, full text, CC0, acquired and extracted from specification Markdown. The EntryPoint trust boundary is source text; any engineering checklist or security implication is presenter interpretation, not an audit.

The evidence ledger records response metadata, hashes, boundaries, and source links. An 11-word source excerpt appears in the [MCP demo excerpts](presentation/pitch-visual/mcp-demo-excerpts.json) and proof slide.

## Corpus and source honesty

Repository README observation `2026-10-05T10:00Z`: 180,834 documents; 17,365 Web3-tagged records; 10,801,902 chunks; 3,388,476 citation edges. The observation timestamp is not a fresh snapshot or reproducible index version. Tags can overlap. These are inventory counters, not adoption, usage, or paying-customer counts, and do not establish universal full-text availability. Use the dated observation or omit the counts; do not describe them as live traction.

Four layers: Web3 (primary), AI agents, LLMs/small language models, and builder-tech.

Sources include arXiv, IACR ePrint, ACL Anthology, PMLR, OpenAlex open-access works, EIPs/ERCs, Solana SIMDs, whitepapers and curated GitHub technical docs. Full-text availability is per record. Original LaTeX can be exact; recovered PDF text or math reconstruction is not exact original LaTeX. Abstract-only fallbacks remain possible, including arXiv. Some IACR records recover full text through open-access twins; IACR is not uniformly abstract-only. The October 5 BEV call returned exact-LaTeX source objects, but reports `parse_completeness=unknown` and `source_url_status=source_not_verified`; the source paper was separately opened and manually checked. A related section read contained a duplicated paragraph/artifact and was truncated, so it is not presented as perfect extraction.

Inspect each response's provenance, scope and fallback markers. No claim is made that every response includes a corpus snapshot or that all indexed papers contain failure sections. Positioning is the combined Web3 evidence workflow, not unverified uniqueness against Elicit or arXiv MCP tools.

## Signed readings and security boundary

Current policy: `api/registry_policy.py`. Model identities must be established by the server, filtered for provenance and deduplicated by the caller; client-declared model names and wallet/API-key pseudonyms are not trusted diversity.

- Agreement needs the configured decisive-reading quorum (default 2) and at least two distinct trusted models to reach `confirmed_prior_art` or `ruled_out`.
- Wallet-only agreement remains `read_once` (pending); one trusted model at quorum gives `agreed_same_model`. Opposing decisive readings give `contested`.
- One wallet signature cannot by itself establish `confirmed_prior_art`. More wallets do not prove independent reasoning. Even policy-qualified agreement is a registry reading status, not scientific consensus or audit certification.
- Solana signing and SAS anchoring establish attribution and a checkable record, not the correctness of the claim. Free writes have no payment-based Sybil cost; paid writes do not solve Sybil resistance.

Main independently verified the October 2 hardening checks: 47/47 attestor tests, 19 targeted API tests, `tsc --noEmit` and the attestor build passed. These are bounded checks, not a full release benchmark. Scoped fixes are deployed: Contabo attestor build passed, service active and read-only health HTTP 200; API `py_compile` passed, service restarted and health HTTP 200. Pipeline and MCP remain active; pipeline and Qdrant were not restarted. The post-deployment free public read-smoke passed; no fresh paid-chain execution or consensus is claimed.

Verified smoke call: `get_research_bundle(query="EIP-4337", layer="web3", limit=1, max_chars=2000, strict=true)`. It returned actual specification text, code and Security Considerations, with `isError=false`, `generated=false`, `search_partial=false` and `warnings=[]`. `partial=true` is expected: the 2000-character budget clipped prose/code, and equations/algorithms/tables were absent from the retrieved chunks. This proves the public read path is healthy, not complete parsing or corpus-wide completeness.

Live negative-offset check: `read_paper_section(paper_id="eip:4337", section_type="limitations", offset=-1, max_chars=500)` returned `{"error":"research API error (status422)"}`. The MCP envelope has `isError=false` despite the error payload; this is verified deployed validation, not a successful section read or `isError=true` transport response.

Stale updates and conflicting equal-time readings return HTTP 409, not 403; exact retries keep the original transaction. Signer authentication precedes quota consumption; health and lookup stay read-only. Same-PDA updates are serialized with process-local locks (256 active PDAs, 32 pending requests per PDA). SQLite uses conditional monotonic updates with nullable INTEGER timestamps and propagates 409; negative section offsets return 422. Locks cover one attestor instance only. No external formal review or mainnet readiness is claimed. See [local hardening follow-up](docs/2026-10-02-registry-hardening.html).

## Payments and chain status

The free beta at `/mcp` is not being converted into a paywall. `https://read.whoim.space/x402/mcp` is an optional x402 devnet experiment. An unpaid call can carry a `payment_required` MCP challenge inside HTTP 200; it is not guaranteed to use HTTP 402 transport. A challenge is not settlement.

The separately dated October 3 proof below establishes one tested Solana devnet payment/write path. It is existing evidence, not a transaction performed for these videos. Base, Ethereum and Arbitrum have configured testnet support only. Paid mainnet and full production remain **NO-GO** under the [October 3 release gate](docs/2026-10-03-payment-release-gate.md): EVM settlement is unverified, RAM headroom and a full application/Qdrant restore remain production prerequisites. Do not claim mainnet readiness, four paid E2Es, fresh settlement, paid-read verification or sustained capacity. No new payments, signatures or writes are authorized for this preparation.

### Existing devnet payment and one reading: 2026-10-03

At `2026-10-03T09:55:43.992Z`, one authorized call to the public x402 endpoint returned `isError=false` and `receipt.success=true`. Independent public Solana RPC readback verified finalized transactions (`err=null`), devnet genesis, SAS ownership, record fields and the decoded reviewer's Ed25519 signature. The payment was exactly 0.01 devnet USDC; payment slot `506956512` preceded attestation slot `506956519` by seven slots. This is one path, not a production certificate. [Live release evidence](docs/2026-10-03-live-release-evidence.md).

- [Payment transaction](https://explorer.solana.com/tx/31iL5AZXhVpBFVDF2Je42C3uZAirzk2Hg9LpLtLBXd3mw5w8inWYwbvgoYVCW3t4zxMrbTVZVsHcoxsukf8adu8J?cluster=devnet).
- [Attestation transaction](https://explorer.solana.com/tx/4wwnu49FV5gSshoXyfBteZZhEod5wcNSuQsyBx5e148ESpupQyPQQ47Upzip1tLgjoVQ1AaEyahtpXRK8qysJ3x3?cluster=devnet); PDA `G1BBNygRmArfr4Ut7s3D84SBBwsgSQPRxv6uy6dnFH6b`.
- Test claim: `Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack`; paper `eip:4337`; verdict **`does_not_assert`**; evidence fragment hash `335696d9c582a3285a719928621789cf8ce38438f51d39b8905ed124b0d0978a`.
- Registry result: **`read_once`**, one reader, one `does_not_assert`; `judged_by_model=unspecified` is not trusted-model attribution. Do not force `confirmed_prior_art` or infer independent scientific consensus.

No paid read, invalid-signature live call, replay, batch, retry or second payment/write was tested in that run. The current video shows a dated proof card and existing public Explorer links; it does not execute this flow again.

### Historical on-chain evidence: 2026-09-29

Retained for traceability, not presented as a new Web3 demo or independent votes:

- Team-controlled reviewer wallet A: GRPO critic-free group-relative baseline claim, paper `2402.03300`, `asserts`. [SAS attestation](https://explorer.solana.com/address/729nUikYt2SUJaq1Brx2YwXvgLT5J1eVp9sfo31ncswu?cluster=devnet).
- Team-controlled reviewer wallet B: DeepSeek-R1-Zero without preliminary SFT claim, paper `2501.12948`, `asserts`, historical paid x402 call. [SAS attestation](https://explorer.solana.com/address/6pC4NS96euD9G7jPtjvXbVs1ffaWAjKdJMkX799Mq6eL?cluster=devnet).
- [Historical Solana devnet x402 settlement](https://explorer.solana.com/tx/5DWkk85AFVHSTCqeSEbq8SpxEAtfkSRgQZLPJy9ZyCnTvZczsyD86yKELGWf5xYrDrF5wTvt3txUL19NFLYmrBvG?cluster=devnet).

The attestations concern different AI claims and were made by team-controlled wallets. They demonstrate attribution/anchoring, not independent agreement. The payment transaction is a separate historical event. Wallet-count status observations do not establish settlement under the current trusted-model policy.

## Evaluation and test status

Historical measured retrieval results, not a new release benchmark:

- Citation-context held-out test split, 334 queries: 2026-09-26 hit@3 0.195 / hit@10 0.272; 2026-09-27 0.359 / 0.425; 2026-09-28 0.377 / 0.458.
- September 28-29 security hand set, 28 queries: hit@3 0.571 to 0.714. Historical Web3 hand set, 16 queries: hit@3 0.406, a known weak spot.
- Latest bounded Web3 probe: 7/9 informative results. This is a small diagnostic, not a release benchmark or evidence of 17/17 fresh golden passes.
- Proposed shared 20-question competitor comparison: not run. No comparative win, accuracy result, or speed claim is made.

Historical pre-hardening report dated 2026-10-02: 497 API + 11 MCP + 40 eval + 27 attestor + 15 gateway passes, one skip. These totals are retained as history, not post-fix validation. Main independently verified the October 2 bounded checks: attestor 47/47 in 2.4 seconds, `tsc --noEmit` and attestor build passed; 19 targeted API tests passed in 1.65 seconds with three pre-existing DeprecationWarnings. This is not a fresh full-suite run. Code backups and a `VACUUM INTO` registry copy were created on Contabo; the copy's integrity check passed. Before those edits, four checked source files matched HEAD: `attestor/src/message.ts`, `attestor/src/server.ts`, `attestor/src/sas.ts` and `api/main.py`; no divergence was found in those files. Scoped deployment, attestor/API health HTTP 200 and the bounded free public read-smoke were verified.

October 3 reported validation supersedes the earlier gateway count: gateway 38/38 in 2.25 seconds and standalone TypeScript compilation passed; the helper 3/3 subset overlaps and is not additive. Attestor 47/47 and TypeScript compilation passed. These local SDK/mock-chain tests are separate from the one public devnet RPC proof above. Twelve public read/preview paths passed reported smoke checks; their individual timings are observations, not SLOs. No full new benchmark or paid-mainnet approval is claimed. See the dated [release gate](docs/2026-10-03-payment-release-gate.md) and [live observations](docs/2026-10-03-live-release-evidence.md).

## Built before vs during the contest

Development existed before September 14, 2026 (initial work from July 31). Disclose it, rather than presenting the entire corpus or platform as contest-built.

Before the contest: ingestion/quality/chunking/embedding/citation pipeline, REST API, MCP read tools, API-identity registry, and shadow-mode x402 gateway without settlement.

During the contest, documented in [git history](https://github.com/who1900/dtox-research/commits/main):

- Wallet-signed verdicts, Solana Attestation Service integration and Solana devnet x402 settlement, including historical paid verdict writes.
- Hierarchical paper-then-chunk retrieval, paper BM25 and rank fusion, paper embeddings, citation-graph traversal, twin merging, boilerplate filtering and scope handling; citation-context and security evaluations.
- LaTeX input-resolution repair and reprocessing, Web3 standards/technical docs/whitepapers, arXiv harvest work, false-positive niche cleanup, and builder-tech coverage.
- Paper exploration tools, bounded `get_research_bundle`, deployment/embedding reliability work and public corpus counters.
- October 2: Web3-first source-verified demo and submission revision; monotonic/idempotent registry hardening, authentication-before-quota and read-only health/lookup. Bounded checks passed (47 attestor, 19 targeted API, TypeScript check and attestor build); scoped fixes deployed with attestor/API health HTTP 200 and a passing free public read-smoke.
- October 3: one authorized current public Solana devnet payment and signed ERC-4337 `does_not_assert` test reading, independently read back through public RPC; registry status remained `read_once`. Paid-mainnet and production gates remained NO-GO.
- October 5: documentation consistency work, the shorter 9+2 jury deck and matched editorial video preparation. These materials do not create new user, payment or scientific-quorum evidence.

## Business and distribution proposal

Designed for Web3 protocol teams and agent frameworks doing protocol research. Proposed distribution: open-source MCP integration and reproducible source-backed examples. No external pilot feedback is being collected for this submission update.

Proposed pilot, not actual traction: 3 teams over 14 days, each bringing one real protocol decision. Each team produces a task-cited design memo naming the source, the assumption, and the engineering consequence, then returns with a second research question. Success target: at least 2 of 3 teams complete that memo and return with the second question. No participants are confirmed.

Proposed monetization: optional paid transport or intensive usage. Keep the same source bundle free; paid access is not exclusive evidence or better evidence quality. Pricing, willingness to pay, conversion, and revenue are unvalidated. A shared 20-question competitor comparison is proposed but not run; no measured win is claimed.

## Links and remaining owner fields

- [Free MCP](https://read.whoim.space/mcp), [research site](https://read.whoim.space/research/), [optional x402 MCP](https://read.whoim.space/x402/mcp).
- [Public code](https://github.com/who1900/dtox-research), AGPL-3.0; MCP, x402 and SAS integration.

TODO(owner), no guesses:

- Contact email/handle for judges.
- Confirm team roster/leader, location, roles, background, founder-market fit and motivation; founder facts in the deck need owner review.
- Verify registration/eligibility for every member, rules consent and authority to submit; inspect the actual portal and choose the ecosystem track.
- Supply actual prior-work/funding/sponsorship history and third-party code/IP disclosures; no numeric funding threshold is assumed.
- Public presentation video URL (2-3 minutes, English) and separate demo video URL (no more than 3 minutes, English), after owner review and separately authorized upload. Match the approved videos to the final reviewed repository revision.
