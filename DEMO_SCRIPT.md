# dtox research: demo video script (under 3 minutes)

One continuous agent session. No architecture slides. Every number on screen comes from a live call.

Setup before recording:
- Qdrant collection status `green`, search responses without `partial`.
- Claude Code with the dtox MCP added (`claude mcp add --transport http dtox https://read.whoim.space/mcp`).
- Terminal 2 on the server with `x402-gateway/scripts/demo-buyer.ts` ready and the demo buyer wallet funded with devnet USDC.
- Browser tab on Solana Explorer (devnet).

## 0:00 to 0:20. The problem

Voice: "A founder wants to ship a Solana protocol with an AI component. Before writing code they need to know: has this been done, what are the equations, where does it break. Google gives links. An LLM gives confident guesses. dtox gives the agent evidence."

Screen: the prompt typed into Claude Code:

> I want to build an RL fine-tuning loop for a small on-chain trading agent without a critic model. Is there prior art, what is the exact objective, and what are its known limitations?

## 0:20 to 1:00. Evidence, not links

Screen: the agent calls `validate_project`, then `get_code_or_math_spec` on arXiv 2402.03300.

Show on screen, in this order:
1. The prior-art hit: DeepSeekMath, method section, with source link and licence.
2. The GRPO objective returned as raw LaTeX, not a paraphrase.
3. A limitations chunk from a related paper.
4. `corpus.as_of` and the scope note, to show the answer states what it covers.

Voice: "It works on full text: method sections, equations and limitations are first-class objects. It says what it covers and when it has nothing."

## 1:00 to 1:40. The agent pays per result

Screen: terminal 2 runs the buyer against `https://read.whoim.space/x402/mcp`.

Show:
1. The HTTP 402 payment requirement: $0.01 USDC on Solana devnet.
2. The agent signs, the facilitator settles, the tool result arrives.
3. Click the settlement transaction in Solana Explorer.

Voice: "No signup, no API key. The agent pays a cent in USDC per evidence bundle through x402. It does not even need SOL: the facilitator pays the fee."

## 1:40 to 2:30. The verdict goes on chain

Screen: the agent calls `get_verdict_message`, signs it with its Solana wallet, calls `record_signed_verdict` (paid, $0.01).

Show:
1. The canonical message the wallet signs.
2. The response with the attestation address.
3. Solana Explorer: the attestation account owned by the Solana Attestation Service program, with the claim, paper, verdict, reviewer and signature inside.
4. The same claim read back through search: status `confirmed_prior_art` because two independent wallets agree.

Voice: "Retrieval scores measure wording, not truth. So the reading agent files a verdict, signed by its own wallet, and dtox anchors it with the Solana Attestation Service. Anyone can check who said what without trusting us. A claim is only settled when independent wallets agree, and every write costs money, so flooding the registry is not free."

## 2:30 to 2:55. Why it matters

Screen: the landing page `https://read.whoim.space/research/` with corpus numbers.

Voice: "Over [N] papers, protocol specs and whitepapers, [M] structural chunks, Web3 first. Open source under AGPL. One line to add it to any MCP client. dtox is the evidence layer for crypto agents."

End card: GitHub link, MCP install line, devnet notice.

## Honesty checks before publishing

- Every on-screen number read from a live response recorded the same day.
- Say "devnet" out loud once.
- No claim about users or revenue that is not in the submission's own evidence.
