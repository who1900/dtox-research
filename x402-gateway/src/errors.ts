import { StreamableHTTPError } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";

export class UpstreamToolError extends Error {
  constructor(readonly payload: Record<string, unknown>) {
    super(typeof payload.error === "string" ? payload.error : JSON.stringify(payload));
    this.name = "UpstreamToolError";
  }
}

const transientStatuses = new Set([502, 503, 504]);
const transientTransportCodes = new Set([
  "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EPIPE", "EAI_AGAIN",
  "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT", "UND_ERR_SOCKET"
]);

export function isTransientUpstreamError(error: unknown): boolean {
  const seen = new Set<object>();
  for (let depth = 0; depth < 8 && error !== null && typeof error === "object"; depth++) {
    if (seen.has(error)) return false;
    seen.add(error);
    if (error instanceof StreamableHTTPError) return typeof error.code === "number" && transientStatuses.has(error.code);
    if (error instanceof McpError && (error.code === ErrorCode.RequestTimeout || error.code === ErrorCode.ConnectionClosed)) {
      return true;
    }
    const details = error instanceof UpstreamToolError ? error.payload : error as Record<string, unknown>;
    const status = details.status ?? details.statusCode;
    if (typeof status === "number") return transientStatuses.has(status);
    if (typeof details.code === "string" && transientTransportCodes.has(details.code)) return true;
    error = details.cause;
  }
  return false;
}
