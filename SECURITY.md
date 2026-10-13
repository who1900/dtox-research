# Security

## Reporting

Report suspected vulnerabilities through [GitHub private vulnerability reporting](https://github.com/who1900/dtox-research/security/advisories/new). Include the affected component, reproduction steps and expected impact.

## Trust boundaries

- Qdrant, the embedding service, and the research API are internal services and
  should bind to loopback or a private network.
- The public MCP provides research reads and wallet-signed readings. Keep
  unsigned registry operations behind distinct authenticated identities;
  `MCP_REGISTRY_WRITES_ENABLED=0` is the public-endpoint default.
- API keys, alert tokens, payment configuration, databases, caches, and model
  artefacts are runtime state and must not be committed.
- The x402 gateway never needs a wallet private key. It only receives public
  payment addresses and delegates verification to the configured facilitator.

## Supported version

Security fixes are applied to the `main` branch.
