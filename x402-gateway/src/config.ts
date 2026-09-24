export type PaymentMode = "disabled" | "shadow" | "live";

export interface GatewayConfig {
  host: string;
  port: number;
  mode: PaymentMode;
  upstreamMcpUrl: string;
  facilitatorUrl: string;
  evmNetworks: `${string}:${string}`[];
  svmNetwork: `${string}:${string}`;
  evmPayTo: string;
  svmPayTo: string;
  prices: {
    evidence: string;
    spec: string;
    compare: string;
    trends: string;
    audit: string;
  };
}

const DEFAULT_EVM_NETWORKS = [
  "eip155:84532",
  "eip155:11155111",
  "eip155:421614"
] as `${string}:${string}`[];

function parseEvmNetworks(env: NodeJS.ProcessEnv): `${string}:${string}`[] {
  const raw = env.X402_EVM_NETWORKS ?? env.X402_EVM_NETWORK;
  const networks = raw ? raw.split(",").map((value) => value.trim()).filter(Boolean) : DEFAULT_EVM_NETWORKS;
  const unique = [...new Set(networks)];
  if (!unique.length || unique.some((network) => !/^eip155:\d+$/.test(network))) {
    throw new Error("X402_EVM_NETWORKS must contain comma-separated eip155 chain identifiers");
  }
  return unique as `${string}:${string}`[];
}

function parseMode(value: string | undefined): PaymentMode {
  if (!value || value === "disabled") return "disabled";
  if (value === "shadow" || value === "live") return value;
  throw new Error(`X402_MODE must be disabled, shadow, or live; got ${value}`);
}

function parsePort(value: string | undefined): number {
  const port = Number(value ?? "8012");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("X402_PORT must be a valid TCP port");
  }
  return port;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  const config: GatewayConfig = {
    host: env.X402_HOST ?? "127.0.0.1",
    port: parsePort(env.X402_PORT),
    mode: parseMode(env.X402_MODE),
    upstreamMcpUrl: env.DTOX_UPSTREAM_MCP_URL ?? "http://127.0.0.1:8011/mcp",
    facilitatorUrl: env.X402_FACILITATOR_URL ?? "https://x402.org/facilitator",
    evmNetworks: parseEvmNetworks(env),
    svmNetwork: (env.X402_SVM_NETWORK ?? "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1") as `${string}:${string}`,
    evmPayTo: env.X402_EVM_PAY_TO ?? "",
    svmPayTo: env.X402_SVM_PAY_TO ?? "",
    prices: {
      evidence: env.X402_PRICE_EVIDENCE ?? "$0.01",
      spec: env.X402_PRICE_SPEC ?? "$0.03",
      compare: env.X402_PRICE_COMPARE ?? "$0.05",
      trends: env.X402_PRICE_TRENDS ?? "$0.03",
      audit: env.X402_PRICE_AUDIT ?? "$0.25"
    }
  };

  if (config.mode === "live") {
    const missing = [
      !config.evmPayTo && "X402_EVM_PAY_TO",
      !config.svmPayTo && "X402_SVM_PAY_TO"
    ].filter(Boolean);
    if (missing.length) {
      throw new Error(`live mode is missing: ${missing.join(", ")}`);
    }
    if (!/^0x[0-9a-fA-F]{40}$/.test(config.evmPayTo)) {
      throw new Error("X402_EVM_PAY_TO must be an EVM address");
    }
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(config.svmPayTo)) {
      throw new Error("X402_SVM_PAY_TO must be a Solana address");
    }
  }

  return config;
}
