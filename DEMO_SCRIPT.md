# dtox research: demo video script (under 3 minutes)

One continuous agent session. No architecture slides. Every number on screen comes from a live call.

Setup before recording:
- Qdrant collection status `green`, search responses without `partial`.
- Claude Code with the dtox MCP added (`claude mcp add --transport http dtox https://read.whoim.space/mcp`).
- Terminal 2 on the server with `x402-gateway/scripts/demo-buyer.ts` ready and the demo buyer wallet funded with devnet USDC.
- Browser tab on Solana Explorer (devnet).
- Rehearse the exact claim wording below the same day: `validate_project` must return `prior_art_confirmed` (checked live on 2026-09-29). If it does not, re-check the registry before recording.

## 0:00 to 0:20. The problem

Voice: "A founder wants to ship a Solana protocol with an AI component. Before writing code they need to know: has this been done, what are the equations, where does it break. Google gives links. An LLM gives confident guesses. dtox gives the agent evidence."

Screen: the prompt typed into Claude Code:

> I want to build an RL fine-tuning loop for a small on-chain trading agent without a critic model. Is there prior art, what is the exact objective, and what are its known limitations?

## 0:20 to 0:50. Evidence, not links

Screen: the agent calls `validate_project` with the claim below, then `get_code_or_math_spec` on arXiv 2402.03300.

Claim, exact wording (it returns `confirmed_prior_art` for arXiv 2402.03300):

> GRPO removes the separate value critic model by estimating the baseline from group relative rewards of sampled outputs

Show on screen, in this order:
1. The prior-art hit: DeepSeekMath (2402.03300), with `verdict: prior_art_confirmed`, source link and licence.
2. The GRPO objective returned as raw LaTeX, not a paraphrase.
3. `corpus.as_of` and the scope note, to show the answer states what it covers.

Voice: "It works on full text: method sections, equations and limitations are first-class objects. It says what it covers and when it has nothing."

## 0:50 to 1:10. Explore by paper

Screen: the agent calls `find_papers` with `query="MEV"` and `sort="foundational"` (or `query="KV cache compression"`), then `get_paper` on one result, for example arXiv 2101.05511 (Quantifying Blockchain Extractable Value) or 2309.17453 (StreamingLLM).

Show on screen:
1. `foundational` ranking: the works that the relevant papers cite, not just the best keyword match.
2. `get_paper`: abstract, outline of sections, what the paper cites and what cites it.

Voice: "It is a library you explore in steps. Which papers matter, then one paper in depth, then the exact section."

## 1:10 to 1:45. The agent pays per result

Screen: terminal 2 runs the buyer against `https://read.whoim.space/x402/mcp`.

Show:
1. The HTTP 402 payment requirement: $0.01 USDC on Solana devnet.
2. The agent signs, the facilitator settles, the tool result arrives.
3. Click the settlement transaction in Solana Explorer.

Voice: "No signup, no API key. The agent pays a cent in USDC per evidence bundle through x402. It does not even need SOL: the facilitator pays the fee."

## 1:45 to 2:35. The verdict goes on chain

Screen: the agent calls `get_verdict_message` (free), signs it with its Solana wallet, calls `record_signed_verdict` through the paid endpoint ($0.01). Use a fresh demo claim and paper, or the existing attestation for the GRPO claim will be idempotent.

Show:
1. The canonical message the wallet signs.
2. The response with the attestation address.
3. Solana Explorer: the attestation account owned by the Solana Attestation Service program, with the claim, paper, verdict, reviewer and signature inside.
4. The same claim read back through `validate_project`: status `confirmed_prior_art`, with the number of readers on record.

Voice: "Retrieval scores measure wording, not truth. So the reading agent files a verdict, signed by its own wallet, and dtox anchors it with the Solana Attestation Service. Anyone can check who said what without trusting us. A claim is only settled when distinct readers agree. Writing through the free endpoint costs nothing, the paid endpoint costs a cent, so this raises the price of flooding the registry but does not make it impossible."

## 2:35 to 2:58. Why it matters

Screen: the landing page `https://read.whoim.space/research/` with corpus numbers.

Voice: "Over [N] papers, protocol specs and whitepapers, [M] structural chunks, Web3 first. Open source under AGPL. One line to add it to any MCP client. dtox is the evidence layer for crypto agents."

End card: GitHub link, MCP install line, devnet notice.

## Honesty checks before publishing

- Every on-screen number read from a live response recorded the same day.
- Say "devnet" out loud once.
- No claim about users or revenue that is not in the submission's own evidence.
- Read N and M from the landing page on the recording day.
- Say plainly that the two demo attestations come from wallets the team controls.
