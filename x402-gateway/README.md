# dtox x402 gateway

An isolated paid MCP surface for dtox research. It does not replace the existing MCP server. It exposes only read-only research tools plus one narrow write path: `record_signed_verdict`, which never touches the shared-API-key claim registry directly -- it accepts only a caller's own wallet-signed verdict, gated by payment as a sybil mitigation. Every other mutation tool on the upstream dtox MCP stays unreachable through this gateway.

## Modes

- `disabled`: tools execute without payment metadata. Safe default.
- `shadow`: tools execute for free and return the price and supported networks.
- `live`: paid tools require x402 v2 USDC payment. Startup fails closed unless a Solana recipient address is present.

The default networks are Base Sepolia, Ethereum Sepolia, Arbitrum Sepolia and Solana Devnet. One EVM receiving address is used across the three EVM chains. Mainnet networks and a production facilitator are configuration, not code changes.

### Solana-only live mode

`live` mode can run with only Solana registered, no EVM scheme at all:

- Leave `X402_EVM_PAY_TO` unset, **or**
- Set `X402_EVM_NETWORKS=""` (the literal empty string) to explicitly opt out of EVM even if an EVM payTo happens to be configured.

Either condition drops all EVM networks from `evmNetworks`, skips EVM address validation, and the gateway starts with a single SVM network. `X402_SVM_PAY_TO` is still required and validated either way -- fail-closed is unconditional on the Solana side. Setting a real `X402_EVM_PAY_TO` without emptying `X402_EVM_NETWORKS` keeps the original both-required behavior.

```bash
npm ci
npm test
npm run build

X402_MODE=shadow npm start
```

Live mode (Solana-only) requires:

```text
X402_MODE=live
X402_SVM_NETWORK=solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1
X402_SVM_PAY_TO=...
DTOX_UPSTREAM_MCP_URL=http://127.0.0.1:8011/mcp
# X402_EVM_PAY_TO left unset (or X402_EVM_NETWORKS="") keeps this Solana-only.
```

No private key belongs on the resource server. `PAY_TO` values are public receiving addresses. Facilitator credentials, if required, must be injected by the service manager and never committed. The gateway calls the existing local dtox MCP rather than holding an internal research API key.

## Facilitator: Solana devnet support, fee payer, and the recipient ATA

Checked directly against the default facilitator's `/supported` endpoint
(`https://x402.org/facilitator/supported`) rather than assumed from docs:

```json
{"x402Version":2,"scheme":"exact","network":"solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
 "extra":{"feePayer":"CKPKJWNdJEqa81x7CkZ14BVPiY6y16Sxs7owznqtWYp5","features":{"smartWalletSupported":true}}}
```

- **Yes, the default `https://x402.org/facilitator` supports Solana devnet for the `exact` scheme** (V2, CAIP-2 network `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1`), so no alternate facilitator is needed for this project. It also lists the V1 `solana-devnet` network for older clients.
- **The facilitator pays the transaction fee**, not the buyer. `extra.feePayer` names the facilitator's own key; `@x402/svm`'s exact-scheme client (`exact/client/index.js`) reads `paymentRequirements.extra.feePayer` and sets it as the transaction's fee payer, so the buyer only signs as the SPL transfer authority. **A buyer's wallet needs devnet USDC but does not need any SOL.**
- **The recipient does need a pre-created USDC associated token account (ATA).** The client scheme builds a bare `TransferChecked` straight to the recipient's ATA (`findAssociatedTokenPda` + `getTransferCheckedInstruction`) with no `CreateAssociatedTokenAccount` instruction anywhere in the flow. If `X402_SVM_PAY_TO` has never held USDC, its ATA does not exist and the very first paid call fails on-chain. `scripts/ensure-recipient-ata.ts` creates it once, idempotently, using `getCreateAssociatedTokenIdempotentInstructionAsync`.
- Devnet USDC mint: `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU` (matches Solana's own x402 docs).

```bash
# One-time, run by the operator (payer needs a little devnet SOL for rent + fee; never a buyer's job):
npx tsx scripts/ensure-recipient-ata.ts ./operator-keypair.json <X402_SVM_PAY_TO>
```

## Paid tools and the verdict write path

`dtox_service_info` and `search_research_preview` are free, as is `get_verdict_message` (it only returns the canonical string to sign; it writes nothing). `get_evidence_bundle`, `get_code_or_math_spec`, `compare_methods`, `research_trends`, and `validate_project` are paid, read-only research tools, priced independently. `record_signed_verdict` is paid (`X402_PRICE_VERDICT`, default `$0.01`) and is the only tool that writes anything: it proxies straight to the upstream dtox MCP's `record_signed_verdict`, which verifies the caller's own ed25519 signature over the verdict text and writes a Solana Attestation Service attestation to devnet (see `attestor/README.md`). The gateway does not add its own signature check -- that already happens upstream -- it adds the payment gate on top, so filing a bad-faith verdict costs money per attempt, not just per identity.

## Demo buyer

`scripts/demo-buyer.ts` is an x402-paying MCP client (built on `@x402/mcp`'s `createx402MCPClient` + `@x402/svm`'s `ExactSvmScheme` client) that talks to a running gateway over devnet:

```bash
# Ordinary paid research call:
npx tsx scripts/demo-buyer.ts ./buyer-keypair.json http://127.0.0.1:8012/mcp

# Specific tool + args:
npx tsx scripts/demo-buyer.ts ./buyer-keypair.json http://127.0.0.1:8012/mcp \
  get_evidence_bundle '{"query":"verifiable delay functions"}'

# Sign-and-record a verdict (get_verdict_message -> ed25519 sign -> paid record_signed_verdict):
npx tsx scripts/demo-buyer.ts ./buyer-keypair.json http://127.0.0.1:8012/mcp \
  --verdict "the protocol tolerates byzantine faults under partial synchrony" 2401.12345 asserts
```

`<keypair.json>` is a solana-keygen JSON keypair; it needs devnet USDC (not SOL, see above) to pay for tool calls. The script prints the amount charged, network, and settlement transaction signature from the payment response, plus (for `--verdict`) the attestation's `explorer_url`. It only ever talks to a devnet RPC and the gateway URL given on the command line.
