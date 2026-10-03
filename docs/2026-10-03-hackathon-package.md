# dtox research: hackathon submission package

Prepared 2026-10-03 from the current repository at `322ff3a`. Copy-ready product text is below. Crypto World's Fair is a multichain event. Official schedule and video requirements were verified for this update. Live, deployment and chain facts in this package are main-attributed, with exact timestamps included where supplied; this is not a production certificate. Do not upload or submit without owner review and explicit authorization.

## Submission fields

**Event:** Crypto World's Fair, a multichain contest  
**Project:** dtox research  
**Category:** Developer Infrastructure  
**Tagline:** Web3 research infrastructure for execution agents.

### Project description

Execution agents need more than a list of papers before their builders choose a transaction path or account architecture. dtox is a Web3-first research service that lets agents find and read protocol specifications, technical papers and whitepapers through MCP. It returns source-linked evidence with acquisition and extraction provenance, including equations, algorithms, code, tables and authors' stated limitations when those materials are available. A bounded research bundle assembles a reading packet under a character budget; it is evidence for a human or agent to inspect, not an automatic security review or novelty verdict.

For example, dtox can retrieve the MEV auction model in *Quantifying Blockchain Extractable Value: How dark is the forest?* alongside the ERC-4337 specification's discussion of EntryPoint risk concentration and protections against account hijacking or fee draining. The paper's expected-utility expression is `E[u_i^PA | alpha] = (1-alpha) Pr^PA(O,S_i) R_i(O) - b_i^PA(O,S_i)`. The source object identifies this as original LaTeX; the ERC-4337 passage is specification text, not dtox-generated advice. The two sources support cross-ecosystem research, not a claim that their assumptions are equivalent.

The public read beta is free and needs no key or signup. Separately, agents can sign readings with a wallet and anchor them through Solana Attestation Service. A signature makes attribution checkable; it does not establish that a claim is correct or that reviewers reasoned independently. Payment, wallet evidence and scientific agreement are distinct capabilities. dtox is built for Web3 developer teams and agent frameworks. Optional usage credits and team integrations are proposed business models, not proven demand or live exclusive tiers.

### Demo: free public MCP

Endpoint: [https://read.whoim.space/mcp](https://read.whoim.space/mcp)  
Product: [research site](https://read.whoim.space/research/)  
Repository: [who1900/dtox-research](https://github.com/who1900/dtox-research), AGPL-3.0.

Connect a client:

```bash
claude mcp add --transport http dtox https://read.whoim.space/mcp
codex mcp add dtox --url https://read.whoim.space/mcp
```

For another MCP client, use `{"type":"http","url":"https://read.whoim.space/mcp"}`. These commands configure the local client. The demo uses free read tools only; no wallet or payment is needed.

Try the source-backed Web3 path with these MCP calls:

```text
find_papers(query="Quantifying Blockchain Extractable Value", layer="web3", limit=3)
get_code_or_math_spec(arxiv_id="2101.05511", target_elements="equation,algorithm", max_chars=5500)
read_paper_section(paper_id="eip:4337", section_type="limitations", max_chars=3500)
```

In the recorded response dated 2026-10-02, discovery ranked arXiv `2101.05511` first. The spec call returned seven objects (one algorithm and six equations), 2,211 characters, and `truncated=false`; show the BEV auction model, not the transaction-replay algorithm. The ERC-4337 call returned 1,205 characters of CC0 Markdown specification text. These are specific recorded responses, not a guarantee of identical future retrieval.

The bounded bundle tool is `get_research_bundle(query="EIP-4337", layer="web3", limit=1, max_chars=2000, strict=true)`. In the recorded 2026-10-02 smoke it returned actual specification text with `generated=false`, `search_partial=false`, and no warnings. `partial=true` was expected because the character budget clipped the returned prose/code; equations, algorithms and tables were absent from those chunks. A bounded sample is not exhaustive coverage. Inspect each result's provenance and abstract-only fallback markers before citing it. Candidates are not confirmed prior art.

### Wallet readings: traceable historical evidence

These are historical Solana devnet records from 2026-09-29, not fresh demo writes, independent votes or proof of scientific agreement. Both reviewer wallets were team-controlled and addressed different claims.

- Wallet A, GRPO critic-free group-relative baseline claim, paper `2402.03300`, reading `asserts`: [SAS devnet attestation](https://explorer.solana.com/address/729nUikYt2SUJaq1Brx2YwXvgLT5J1eVp9sfo31ncswu?cluster=devnet).
- Wallet B, DeepSeek-R1-Zero without preliminary SFT claim, paper `2501.12948`, reading `asserts`: [SAS devnet attestation](https://explorer.solana.com/address/6pC4NS96euD9G7jPtjvXbVs1ffaWAjKdJMkX799Mq6eL?cluster=devnet). This was a historical paid x402 call.
- [Historical Solana devnet x402 settlement transaction](https://explorer.solana.com/tx/5DWkk85AFVHSTCqeSEbq8SpxEAtfkSRgQZLPJy9ZyCnTvZczsyD86yKELGWf5xYrDrF5wTvt3txUL19NFLYmrBvG?cluster=devnet).

Under current policy, a decisive registry status requires agreement at the configured quorum and at least two distinct trusted model identities established by the server. Wallet-only agreement remains pending; wallet count, client-declared model names and signatures alone do not prove independent reasoning. An attestation proves signer/message attribution, not scientific correctness or consensus.

### Fresh devnet release smoke: 2026-10-03

One authorized payment and one signed test record were submitted to the public [x402 MCP endpoint](https://read.whoim.space/x402/mcp) at `2026-10-03T09:55:43.992Z`. The MCP/SDK response returned `isError=false` and `receipt.success=true`. An independent public Solana RPC readback then passed: the full genesis hash matched devnet, the SAS account owner matched, the on-chain claim/evidence/paper/verdict/reviewer/issued_at fields matched, and the decoded on-chain reviewer signature passed Ed25519 verification. Both transactions finalized with `err=null`; payment finalized at slot `506956512`, attestation at `506956519`, seven slots after payment. This verifies the minimum live devnet payment-and-write path, not production or mainnet readiness.

- Payment transaction: [devnet explorer](https://explorer.solana.com/tx/31iL5AZXhVpBFVDF2Je42C3uZAirzk2Hg9LpLtLBXd3mw5w8inWYwbvgoYVCW3t4zxMrbTVZVsHcoxsukf8adu8J?cluster=devnet).
- Attestation transaction: [devnet explorer](https://explorer.solana.com/tx/4wwnu49FV5gSshoXyfBteZZhEod5wcNSuQsyBx5e148ESpupQyPQQ47Upzip1tLgjoVQ1AaEyahtpXRK8qysJ3x3?cluster=devnet).
- Attestation PDA: `G1BBNygRmArfr4Ut7s3D84SBBwsgSQPRxv6uy6dnFH6b`; reviewer: `HCTag5BBmexuDNsY6QAE9kkiNev3Xe7bgZQ4eAmE2jfc`.
- Verified genesis hash: `EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG`.
- The `4zMMC9...` devnet mint balance delta was buyer `-10000` and recipient `+10000`, exactly `0.01 USDC`.

The exact claim was “Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack,” paper `eip:4337`, verdict `does_not_assert`. Evidence SHA-256: `335696d9c582a3285a719928621789cf8ce38438f51d39b8905ed124b0d0978a`; claim SHA-256: `f22e64c9c301e383abeb45419ae4b2d36708d4d42f38f65c1b24585b0289eab4`. Registry status was `read_once`, one reader, one `does_not_assert` reading, not quorum. The script used the exact devnet mint and recipient, cap `10000` atomic units, and no automatic retry. The transfer was `10000` atomic units, exactly `0.01` devnet USDC. No additional payment or write was made. The payment verifies the one tested devnet flow only; it does not create scientific quorum. Post-write DB verification found one unique PDA row matching the exact claim, reviewer and verdict; `judged_by_model=unspecified` is not a trusted model. Read-only global counts changed from 53 nodes/15 judgments/2 links before the paid write to 54/16/2 after that single write.

### Videos and event fields

Existing local previews (not public video URLs):

- Presentation, 2:40 (160.104667 seconds): [MP4](../presentation/video-2026-10-02/presentation-preview.mp4), [SRT](../presentation/video-2026-10-02/presentation.srt).
- Product demo, 2:49 (168.896333 seconds): [MP4](../presentation/video-2026-10-02/demo-preview.mp4), [SRT](../presentation/video-2026-10-02/demo.srt).

Both MP4s are editorial videos built from historical recorded MCP responses. They are not continuous footage of a live MCP client session. Technical and visual checks are reported complete; owner review of pronunciation and subtitle timing, public upload, and approval to submit remain pending. No public video link is available in this package. The FAQ requires two separate English-language videos: a presentation of 2 to 3 minutes and a product demo of no more than 3 minutes.

**Verified event schedule:** September 14, 2026 at 6:00 a.m. PT through October 12, 2026 at 11:59 p.m. PT. The deadline is October 13, 2026 at 11:59 a.m. in Asia/Qyzylorda. Official sources: [Crypto World's Fair](https://colosseum.com/worldsfair), [hackathon FAQ](https://colosseum.com/hackathon), [official rules](https://colosseum.com/legal/Crypto%20World%27s%20Fair%20Hackathon%20Rules.pdf).  
**Prior work:** development before September 14 is allowed, but must be disclosed; judging is for work done during the contest period. dtox development began July 31, 2026. Owner to confirm the disclosure and any funding details.  
**Contact, team names/roles, location, founder-market fit, registration and eligibility:** owner to supply and confirm.  
**Upload and submission authority:** pending explicit owner approval. Nothing here implies permission to upload, register, or submit.

## Readiness and claim boundaries

- **Read beta:** the public `/mcp` research read path is free; the October 3 audit reports the API, MCP, pipeline, extractor, backup, monitor, gateway and site active after scoped deployment. Two CI checks are reported green. These facts do not establish an SLO, full production release approval, or whole-corpus completeness.
- **Retrieval context:** current source includes a scoped example for the BEV paper and ERC-4337. The paper's full LaTeX and the specification's Markdown have different provenance and format. Abstract-only fallback remains possible per record. No corpus-growth or adoption metric is asserted here.
- **Solana devnet:** one current authorized payment and signed record passed independent public RPC readback, with transaction links above. Registry status is `read_once`, not quorum. Do not claim scientific confirmation.
- **EVM:** Base Sepolia, Ethereum Sepolia and Arbitrum Sepolia are planned/configured support, not demonstrated paid end-to-end flows. Ethereum Sepolia has a documented SDK asset-mapping gate. No EVM settlement is claimed.
- **Mainnet:** paid mainnet remains NO-GO. No mainnet readiness, scientific consensus, restore SLO, or external formal review is claimed. EVM payment support is unverified.
- **Model/index configuration:** small Oracle is off; bge-base is unchanged. No claim of a new indexing run or corpus expansion.

## Separate checklists

### Hackathon submission

- [ ] Owner verifies current official event rules, deadline, account registration and eligibility.
- [ ] Owner confirms team/contact/founder fields and prior-work/funding disclosure.
- [ ] Owner reviews narration and SRT timing, then decides whether these local MP4s are approved.
- [ ] Owner supplies public URLs after separately authorized upload; confirm video requirements against current official rules.
- [ ] Owner explicitly approves the final form and submission.

### Full production readiness (not required to make this package copy-ready)

- [ ] Separately review resource headroom and authorize any operational maintenance; small Oracle is currently off.
- [ ] Keep full production readiness blocked on RAM headroom and a verified full restore; the bounded state-backup check ended incomplete.
- [ ] Resolve and verify EVM asset/facilitator configuration before claiming EVM payments.
- [ ] Complete owner-approved recovery/restore and broader release-readiness checks.
- [ ] Keep mainnet claims out until a distinct security and operational review supports them.
