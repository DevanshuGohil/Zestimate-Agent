export type Confidence = "HIGH" | "MEDIUM" | "LOW";

export interface ZestimateResponse {
  address: string;
  zestimate: number;
  zpid: string;
  confidence: Confidence;
  provider_used: string;
  fetched_at: string;
  cache_hit: boolean;
  elapsed_ms: number;
}

export interface Candidate {
  zpid?: string;
  street_number?: string;
  street_name?: string;
  city?: string;
  state?: string;
  zip5?: string;
}

export interface ApiError {
  status: number;
  message: string;
  // 422 — ambiguous address
  candidates?: Candidate[];
  original_input?: string;
  // 404 — no zestimate
  zpid?: string;
  hint?: string;
}

// ---------------------------------------------------------------------------
// Streaming
// ---------------------------------------------------------------------------

export type StepStatus = "waiting" | "running" | "done" | "error";

export interface StreamStep {
  node: string;
  label: string;
  status: StepStatus;
  detail?: Record<string, unknown>;
}

export interface StepEvent {
  type: "step";
  node: string;
  status: "running" | "done" | "error";
  label: string;
  detail?: Record<string, unknown>;
}

export interface ResultEvent {
  type: "result";
  data: ZestimateResponse;
}

export interface ErrorStreamEvent {
  type: "error";
  status: number;
  message: string;
  candidates?: Candidate[];
  hint?: string;
  zpid?: string;
}

export type StreamEvent = StepEvent | ResultEvent | ErrorStreamEvent;

// ---------------------------------------------------------------------------

export interface HistoryEntry {
  id: string;
  address: string;
  result: ZestimateResponse;
  timestamp: number;
}
