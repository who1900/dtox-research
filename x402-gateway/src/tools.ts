import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { createPaymentWrapper } from "@x402/mcp";
import { z } from "zod";
import type { createResearchUpstream } from "./upstream.js";
import type { GatewayConfig } from "./config.js";

type Api = ReturnType<typeof createResearchUpstream>;
type Paid = ReturnType<typeof createPaymentWrapper>;
type ToolResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };

function result(value: unknown): ToolResult {
  return { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] };
}

function failed(error: unknown): ToolResult {
  return {
    isError: true,
    content: [{ type: "text", text: JSON.stringify({ error: error instanceof Error ? error.message : "unknown error" }) }]
  };
}

function handler<T extends Record<string, unknown>>(fn: (args: T) => Promise<unknown>) {
  return async (args: T): Promise<ToolResult> => {
    try {
      return result(await fn(args));
    } catch (error) {
      return failed(error);
    }
  };
}

function paidOrShadow<T extends Record<string, unknown>>(
  config: GatewayConfig,
  paid: Paid | null,
  price: string,
  fn: (args: T) => Promise<unknown>
) {
  const run = handler(fn);
  if (config.mode === "live" && paid) return paid(run);
  return async (args: T) => {
    const response = await run(args);
    if (config.mode === "shadow" && !response.isError) {
      const parsed = JSON.parse(response.content[0].text) as Record<string, unknown>;
      response.content[0].text = JSON.stringify({
        ...parsed,
        payment_preview: { price, networks: [config.evmNetwork, config.svmNetwork], charged: false }
      }, null, 2);
    }
    return response;
  };
}

export interface PaidWrappers {
  evidence: Paid | null;
  spec: Paid | null;
  compare: Paid | null;
  trends: Paid | null;
  audit: Paid | null;
}

export function registerTools(
  server: McpServer,
  api: Api,
  config: GatewayConfig,
  wrappers: PaidWrappers
): void {
  server.tool("dtox_service_info", "Free discovery: capabilities, prices and supported payment networks.", {}, async () =>
    result({
      service: "dtox research",
      mode: config.mode,
      networks: [config.evmNetwork, config.svmNetwork],
      settlement_asset: "USDC",
      free_tools: ["dtox_service_info", "search_research_preview"],
      paid_tools: {
        get_evidence_bundle: config.prices.evidence,
        get_code_or_math_spec: config.prices.spec,
        compare_methods: config.prices.compare,
        research_trends: config.prices.trends,
        validate_project: config.prices.audit
      },
      note: "Prices are quoted before execution. Payment does not grant permission to write to the claim registry."
    })
  );

  server.tool(
    "search_research_preview",
    "Free preview over dtox full-text research. Returns at most three papers without expensive expansion.",
    {
      query: z.string().min(3).max(500),
      layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional()
    },
    handler(async ({ query, layer }) => api.search({ query, layer, limit: 3, dedupe: true }))
  );

  server.tool(
    "get_evidence_bundle",
    `Structured full-text evidence with sources and provenance. Price: ${config.prices.evidence} USDC.`,
    {
      query: z.string().min(3).max(500),
      layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional(),
      section_type: z.string().max(40).optional(),
      element_type: z.enum(["algorithm", "equation", "table", "code", "prose"]).optional(),
      year_from: z.number().int().min(1990).max(2100).optional(),
      limit: z.number().int().min(1).max(15).default(8)
    },
    paidOrShadow(config, wrappers.evidence, config.prices.evidence, async (args) => api.search({ ...args, dedupe: true }))
  );

  server.tool(
    "get_code_or_math_spec",
    `Equations, algorithms, code and tables from one indexed paper. Price: ${config.prices.spec} USDC.`,
    {
      paper_id: z.string().min(3).max(100),
      target_elements: z.string().max(100).default("algorithm,equation,code,table"),
      include_prose: z.boolean().default(false),
      max_chars: z.number().int().min(500).max(30000).default(12000)
    },
    paidOrShadow(config, wrappers.spec, config.prices.spec, async ({ paper_id, target_elements, include_prose, max_chars }) => {
      return api.spec({ arxiv_id: paper_id, target_elements, include_prose, max_chars });
    })
  );

  server.tool(
    "compare_methods",
    `Results, limitations and benchmark tables from two indexed papers. Price: ${config.prices.compare} USDC.`,
    {
      paper_id_a: z.string().min(3).max(100),
      paper_id_b: z.string().min(3).max(100),
      tables_only: z.boolean().default(false),
      max_chars: z.number().int().min(500).max(30000).default(12000)
    },
    paidOrShadow(config, wrappers.compare, config.prices.compare, async ({ paper_id_a, paper_id_b, tables_only, max_chars }) => {
      return api.compare({
        arxiv_id_a: paper_id_a,
        arxiv_id_b: paper_id_b,
        extract_tables_only: tables_only,
        max_chars
      });
    })
  );

  server.tool(
    "research_trends",
    `Topic-normalized research trends by year. Price: ${config.prices.trends} USDC.`,
    {
      about: z.string().min(3).max(500),
      layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional(),
      year_from: z.number().int().min(1990).max(2100).default(2023),
      year_to: z.number().int().min(1990).max(2100).optional(),
      top: z.number().int().min(1).max(50).default(20)
    },
    paidOrShadow(config, wrappers.trends, config.prices.trends, async ({ about, layer, year_from, year_to, top }) => {
      return api.trends({ about, layer, year_from, year_to, top });
    })
  );

  server.tool(
    "validate_project",
    `Full literature-backed audit of technical claims. Price: ${config.prices.audit} USDC.`,
    {
      idea: z.string().min(10).max(2000),
      claims: z.array(z.union([
        z.string().min(3).max(1000),
        z.object({ claim: z.string().min(3).max(1000), phrasings: z.array(z.string().min(3).max(1000)).max(4).optional() })
      ])).min(1).max(8),
      layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional(),
      evidence_per_claim: z.number().int().min(1).max(8).default(4),
      deep: z.boolean().default(true)
    },
    paidOrShadow(config, wrappers.audit, config.prices.audit, async (args) => api.validate(args))
  );
}
