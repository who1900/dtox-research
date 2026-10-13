import { toJsonSchemaCompat } from "@modelcontextprotocol/sdk/server/zod-json-schema-compat.js";
import { z } from "zod";

export const paidToolSchemas = {
  get_evidence_bundle: z.object({
    query: z.string().min(3).max(500),
    layer: z.enum(["llm-slm", "ai-agents", "web3", "builder-tech"]).optional(),
    limit: z.number().int().min(1).max(5).default(3),
    max_chars: z.number().int().min(500).max(20000).default(12000),
    strict: z.boolean().default(true)
  }).strict(),
  get_code_or_math_spec: z.object({
    paper_id: z.string().min(3).max(100),
    target_elements: z.string().max(100).default("algorithm,equation,code,table"),
    include_prose: z.boolean().default(false),
    max_chars: z.number().int().min(500).max(30000).default(12000),
    cursor: z.string().min(1).max(1024).optional()
  }),
  compare_methods: z.object({
    paper_id_a: z.string().min(3).max(100),
    paper_id_b: z.string().min(3).max(100),
    tables_only: z.boolean().default(false),
    max_chars: z.number().int().min(500).max(30000).default(12000)
  }),
  research_trends: z.object({
    about: z.string().min(3).max(500),
    layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional(),
    year_from: z.number().int().min(1990).max(2100).default(2023),
    year_to: z.number().int().min(1990).max(2100).optional(),
    top: z.number().int().min(1).max(50).default(20)
  }),
  validate_project: z.object({
    idea: z.string().min(10).max(2000),
    claims: z.array(z.union([
      z.string().min(3).max(1000),
      z.object({ claim: z.string().min(3).max(1000), phrasings: z.array(z.string().min(3).max(1000)).max(4).optional() })
    ])).min(1).max(8),
    layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional(),
    evidence_per_claim: z.number().int().min(1).max(8).default(4),
    deep: z.boolean().default(true)
  }),
  record_signed_verdict: z.object({
    claim: z.string().min(3).max(1000),
    judgments: z.array(z.object({
      id: z.string().min(3).max(100),
      verdict: z.enum(["asserts", "does_not_assert", "partial"]),
      reason: z.string().max(2000).optional(),
      evidence_sha256: z.string().max(64).optional(),
      reviewer: z.string().min(32).max(44),
      signature: z.string().min(1),
      issued_at: z.string().min(1)
    })).min(1).max(20),
    layer: z.enum(["llm-slm", "ai-agents", "web3"]).optional()
  })
};

export type PaidToolName = keyof typeof paidToolSchemas;

export function paidToolJsonSchema(name: PaidToolName): Record<string, unknown> {
  return toJsonSchemaCompat(paidToolSchemas[name], { strictUnions: true, pipeStrategy: "input" });
}
