# dtox x402 gateway

An isolated, read-only paid MCP surface for dtox research. It does not replace the existing MCP server and it never exposes claim-registry mutation tools.

## Modes

- `disabled`: tools execute without payment metadata. Safe default.
- `shadow`: tools execute for free and return the price and supported networks.
- `live`: paid tools require x402 v2 USDC payment. Startup fails closed unless both recipient addresses are present.

The default networks are Base Sepolia, Ethereum Sepolia, Arbitrum Sepolia and Solana Devnet. One EVM receiving address is used across the three EVM chains. Mainnet networks and a production facilitator are configuration, not code changes.

```bash
npm ci
npm test
npm run build

X402_MODE=shadow npm start
```

Live mode requires server-side environment values:

```text
X402_MODE=live
X402_FACILITATOR_URL=https://...
X402_EVM_NETWORKS=eip155:8453,eip155:1,eip155:42161
X402_SVM_NETWORK=solana:5eykt4UsFv8P8NJdTREpY1vzqKZKvdp
X402_EVM_PAY_TO=0x...
X402_SVM_PAY_TO=...
DTOX_UPSTREAM_MCP_URL=http://127.0.0.1:8011/mcp
```

No private key belongs on the resource server. `PAY_TO` values are public receiving addresses. Facilitator credentials, if required, must be injected by the service manager and never committed. The gateway calls the existing local dtox MCP rather than holding an internal research API key.
