import type { ApiError, StreamEvent, ZestimateResponse } from "./types";

const BASE = "/api";

async function parseError(res: Response): Promise<ApiError> {
  let detail: unknown;
  try {
    detail = await res.json();
  } catch {
    detail = { detail: res.statusText };
  }

  const d = (detail as { detail?: unknown })?.detail;

  if (typeof d === "object" && d !== null) {
    const obj = d as Record<string, unknown>;
    return {
      status: res.status,
      message: String(obj.reason ?? obj.detail ?? res.statusText),
      candidates: Array.isArray(obj.candidates)
        ? (obj.candidates as ApiError["candidates"])
        : undefined,
      original_input: obj.original_input as string | undefined,
      zpid: obj.zpid as string | undefined,
      hint: obj.hint as string | undefined,
    };
  }

  return {
    status: res.status,
    message: typeof d === "string" ? d : res.statusText,
  };
}

export async function lookup(
  address: string,
  noCache: boolean
): Promise<ZestimateResponse> {
  const res = await fetch(`${BASE}/lookup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, no_cache: noCache }),
  });

  if (!res.ok) throw await parseError(res);
  return res.json() as Promise<ZestimateResponse>;
}

export async function* lookupStream(
  address: string,
  noCache: boolean,
  signal?: AbortSignal
): AsyncGenerator<StreamEvent, void, unknown> {
  const response = await fetch(`${BASE}/lookup/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, no_cache: noCache }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw await parseError(response);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";

      for (const part of parts) {
        const line = part.trim();
        if (line.startsWith("data: ")) {
          try {
            yield JSON.parse(line.slice(6)) as StreamEvent;
          } catch {
            // skip malformed event
          }
        }
      }
    }
    // flush any remainder
    const tail = buffer.trim();
    if (tail.startsWith("data: ")) {
      try {
        yield JSON.parse(tail.slice(6)) as StreamEvent;
      } catch {
        // ignore
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function clearCache(): Promise<void> {
  const res = await fetch(`${BASE}/cache`, { method: "DELETE" });
  if (!res.ok) throw await parseError(res);
}

export async function getHealth(): Promise<{ status: string; version: string }> {
  const res = await fetch(`${BASE}/health`);
  if (!res.ok) throw new Error("API unreachable");
  return res.json();
}
