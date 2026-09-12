# Security

## Reporting

Do not open a public issue for a suspected vulnerability. Contact the repository
owner through the private contact method on their GitHub profile and include the
affected component, reproduction steps, and expected impact.

## Trust boundaries

- Qdrant, the embedding service, and the research API are internal services and
  should bind to loopback or a private network.
- The public MCP is read-only by default. Keep
  `MCP_REGISTRY_WRITES_ENABLED=0` unless callers have distinct authenticated
  identities.
- API keys, alert tokens, payment configuration, databases, caches, and model
  artefacts are runtime state and must not be committed.
- The x402 gateway never needs a wallet private key. It only receives public
  payment addresses and delegates verification to the configured facilitator.

## Supported version

Security fixes are applied to the `main` branch.
