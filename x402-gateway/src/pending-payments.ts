import { createHash, randomUUID } from "node:crypto";
import { closeSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY, type createPaymentWrapper, type PaymentWrappedHandler, type WrappedToolResult } from "@x402/mcp";
import type { SettleFailureContext } from "@x402/core/server";
import type { SettleResponse } from "@x402/core/types";
import { DEVNET_NETWORK, inspectDevnetSettlement, type SettlementCheck } from "./settlement.js";

type Paid = ReturnType<typeof createPaymentWrapper>;
type Entry = {
  version: 1; id: string; key: string; binding: string; tool: string; args: Record<string, unknown>;
  wire: string; receipt: SettleResponse; state: "pending" | "executing" | "done";
  handlerResult?: WrappedToolResult; result?: WrappedToolResult;
};
function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
}
const hash = (value: string) => createHash("sha256").update(value).digest("hex");
const binding = (tool: string, args: unknown) => hash(canonical({ tool, args }));
function paymentKey(payment: unknown): string | undefined {
  const p = payment as { accepted?: { network?: string }; payload?: { transaction?: string } } | undefined;
  return p?.accepted?.network === DEVNET_NETWORK && typeof p.payload?.transaction === "string"
    ? hash(`${DEVNET_NETWORK}:${p.payload.transaction}`) : undefined;
}
const jsonResult = (body: Record<string, unknown>, isError = false): WrappedToolResult => ({
  content: [{ type: "text", text: JSON.stringify(body) }], structuredContent: body, isError
});

/** One small durable record per pending payment; existing SAS owns verdict idempotency. */
export class PendingPayments {
  private entries = new Map<string, Entry>();
  private handlers = new Map<string, PaymentWrappedHandler>();
  private inflight = new Map<string, { binding: string; promise: Promise<WrappedToolResult> }>();
  private recovering = new Set<string>();
  private timer?: ReturnType<typeof setInterval>;

  constructor(private directory: string, private inspect = inspectDevnetSettlement) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    for (const name of readdirSync(directory).filter((name) => name.endsWith(".json"))) {
      const entry = JSON.parse(readFileSync(join(directory, name), "utf8")) as Entry;
      if (entry.version !== 1 || !["pending", "executing", "done"].includes(entry.state) ||
          name !== `${entry.id}.json` || !entry.key || !entry.wire || !entry.receipt?.transaction ||
          entry.binding !== binding(entry.tool, entry.args) || (entry.state === "done" && !entry.result)) throw new Error(`Invalid payment state: ${name}`);
      if (this.entries.has(entry.key)) throw new Error("Duplicate pending payment state");
      this.entries.set(entry.key, entry);
    }
  }

  private save(entry: Entry): void {
    const file = join(this.directory, `${entry.id}.json`);
    const temporary = `${file}.tmp`;
    const fd = openSync(temporary, "w", 0o600);
    try { writeFileSync(fd, JSON.stringify(entry)); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(temporary, file);
    // Persist the rename too on Linux, where the deployed service runs.
    if (process.platform !== "win32") {
      const dir = openSync(this.directory, "r");
      try { fsyncSync(dir); } finally { closeSync(dir); }
    }
    this.entries.set(entry.key, entry);
  }

  capture(context: SettleFailureContext): Entry | undefined {
    const error = context.error as Error & SettleResponse;
    const key = paymentKey(context.paymentPayload);
    const transport = context.transportContext as { toolName: string; arguments: Record<string, unknown>; result?: WrappedToolResult };
    if (!key || error.errorReason !== "settlement_pending" || !error.transaction ||
        error.network !== DEVNET_NETWORK || context.requirements.scheme !== "exact") return;
    const previous = this.entries.get(key);
    if (previous) return previous;
    const entry: Entry = { version: 1, id: randomUUID(), key,
      binding: binding(transport.toolName, transport.arguments), tool: transport.toolName, args: transport.arguments,
      wire: context.paymentPayload.payload.transaction as string,
      receipt: { success: false, transaction: error.transaction, network: error.network, payer: error.payer, errorReason: "settlement_pending" },
      state: "pending", handlerResult: transport.result };
    this.save(entry);
    return entry;
  }

  paid(entry: Entry, receipt: SettleResponse): void { entry.receipt = receipt; this.save(entry); }

  private finish(entry: Entry, result: WrappedToolResult): WrappedToolResult {
    entry.result = { ...result, _meta: { ...result._meta, [MCP_PAYMENT_RESPONSE_META_KEY]: entry.receipt } };
    entry.state = "done";
    this.save(entry);
    return entry.result;
  }

  private view(entry: Entry): WrappedToolResult {
    if (entry.state === "done") return entry.result!;
    return { ...jsonResult({ status: entry.receipt.success ? "processing" : "settlement_pending",
      operation_id: entry.id, transaction: entry.receipt.transaction, network: entry.receipt.network,
      next_tool: "get_payment_status", retry_after_seconds: 5,
      note: "Recovery is automatic. Poll this operation; do not create another payment or sign another verdict." }),
      _meta: { [MCP_PAYMENT_RESPONSE_META_KEY]: entry.receipt } };
  }

  status(id: string): WrappedToolResult {
    const entry = [...this.entries.values()].find((entry) => entry.id === id);
    if (!entry) return jsonResult({ error: "unknown_payment_operation" }, true);
    if (entry.state !== "done") return this.view(entry);
    // Include the receipt in content too: some MCP client conveniences discard
    // _meta on free calls. The original paid result remains intact underneath.
    return { ...jsonResult({ status: "completed", operation_id: entry.id,
      payment_response: entry.receipt, result: entry.result }, entry.result!.isError),
      _meta: { [MCP_PAYMENT_RESPONSE_META_KEY]: entry.receipt } };
  }

  private async execute(entry: Entry, handler: PaymentWrappedHandler, context: Parameters<PaymentWrappedHandler>[1]): Promise<WrappedToolResult> {
    if (entry.handlerResult) return entry.handlerResult;
    entry.state = "executing";
    this.save(entry);
    const result = await handler(entry.args, context);
    entry.handlerResult = result;
    this.save(entry);
    return result;
  }

  wrap(tool: string, paid: Paid): Paid {
    return (handler) => {
      const run = handler as PaymentWrappedHandler;
      this.handlers.set(tool, run);
      const invoke = paid(async (args, context) => {
        const entry = this.entries.get(paymentKey(context.meta?.[MCP_PAYMENT_META_KEY]) ?? "");
        return entry ? this.execute(entry, run, context) : run(args, context);
      });
      return async (args, extra) => {
        const key = paymentKey((extra as { _meta?: Record<string, unknown> })?._meta?.[MCP_PAYMENT_META_KEY]);
        if (!key) return invoke(args, extra);
        const requestBinding = binding(tool, args);
        const entry = this.entries.get(key);
        const running = this.inflight.get(key);
        if ((entry && entry.binding !== requestBinding) || (running && running.binding !== requestBinding)) {
          return jsonResult({ error: "payment_already_bound_to_another_request" }, true);
        }
        if (entry) return this.view(entry);
        if (running) return running.promise;
        const promise = (async () => {
          const result = await invoke(args, extra);
          const stored = this.entries.get(key);
          if (!stored) return result;
          const receipt = result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as SettleResponse | undefined;
          if (receipt?.success) { this.paid(stored, receipt); return this.finish(stored, result); }
          return this.view(stored); // no new PaymentRequired challenge after a broadcast
        })();
        this.inflight.set(key, { binding: requestBinding, promise });
        try { return await promise; } finally { this.inflight.delete(key); }
      };
    };
  }

  /** Reads chain status only. No facilitator settle/broadcast calls on recovery. */
  async tick(): Promise<void> {
    await Promise.all([...this.entries.values()].filter((entry) => entry.state !== "done").map(async (entry) => {
      if (this.inflight.has(entry.key) || this.recovering.has(entry.key)) return;
      const handler = this.handlers.get(entry.tool);
      if (!entry.handlerResult && !handler) return;
      this.recovering.add(entry.key);
      try {
        const check: SettlementCheck = entry.receipt.success
          ? { status: "confirmed", receipt: entry.receipt } : await this.inspect(entry.wire, entry.receipt);
        if (check.status === "pending") return;
        this.paid(entry, check.receipt);
        if (check.status === "failed") {
          this.finish(entry, jsonResult({ error: check.receipt.errorReason, operation_id: entry.id }, true));
          return;
        }
        // Exact signed args are retained. If the process died during the write,
        // SAS reuses the identical record without another on-chain mutation.
        const result = entry.handlerResult ?? await this.execute(entry, handler!, { toolName: entry.tool, arguments: entry.args });
        this.finish(entry, result);
      } catch (error) {
        console.error(JSON.stringify({ event: "payment_recovery_retry", operation_id: entry.id,
          error: error instanceof Error ? error.message : "recovery error" }));
      } finally { this.recovering.delete(entry.key); }
    }));
  }

  start(): void {
    if (this.timer) return;
    void this.tick();
    this.timer = setInterval(() => { void this.tick(); }, 5_000);
    this.timer.unref();
  }
  close(): void { clearInterval(this.timer); this.timer = undefined; }
}
