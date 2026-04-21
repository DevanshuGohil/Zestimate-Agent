import { Check, X, Loader2 } from "lucide-react";
import type { StreamStep, StepStatus } from "../types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt$(n: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function truncate(s: string, max = 55) {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

const CONF_COLOR: Record<string, string> = {
  HIGH: "text-emerald-500",
  MEDIUM: "text-amber-500",
  LOW: "text-red-400",
};

// Safe typed accessors for detail values (type is Record<string, unknown>)
const str = (v: unknown): string => (v != null ? String(v) : "");
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

type Detail = Record<string, unknown>;

function Conf({ value }: { value: unknown }) {
  const c = str(value);
  if (!c) return null;
  return (
    <span className={`ml-1.5 font-medium ${CONF_COLOR[c] ?? "text-slate-400"}`}>{c}</span>
  );
}

// ---------------------------------------------------------------------------
// Per-node detail line
// ---------------------------------------------------------------------------

function DetailLine({
  node,
  detail,
}: {
  node: string;
  detail: Detail;
}) {
  const base = "text-xs mt-1 leading-snug";

  if (node === "normalize") {
    return (
      <p className={`${base} text-slate-400`}>
        {truncate(str(detail.address))}
        <Conf value={detail.confidence} />
      </p>
    );
  }

  if (node === "resolve" || node === "disambiguate") {
    return (
      <p className={`${base} text-slate-400`}>
        <span className="font-mono">zpid {str(detail.zpid)}</span>
        {detail.matched ? <span> · {truncate(str(detail.matched))}</span> : null}
        <Conf value={detail.confidence} />
      </p>
    );
  }

  if (node === "fetch") {
    const z = num(detail.zestimate);
    return (
      <p className={`${base} text-slate-400`}>
        {detail.address ? <span>{truncate(str(detail.address))} · </span> : null}
        {z != null ? (
          <span className="font-medium text-slate-600">{fmt$(z)}</span>
        ) : (
          <span className="text-amber-500 font-medium">No Zestimate in payload</span>
        )}
      </p>
    );
  }

  if (node === "validate") {
    if (detail.reason) {
      return <p className={`${base} text-red-400`}>{truncate(str(detail.reason), 80)}</p>;
    }
    const z = num(detail.zestimate);
    return (
      <p className={`${base} text-slate-400`}>
        {z != null ? <span className="font-medium text-slate-700">{fmt$(z)}</span> : null}
        <Conf value={detail.confidence} />
      </p>
    );
  }

  if (node === "retry") {
    return (
      <p className={`${base} text-amber-500`}>
        Attempt {str(detail.attempt)}
        {detail.failed_at ? <span> · failed at {str(detail.failed_at)}</span> : null}
        {detail.last_error ? (
          <span className="block text-slate-400 mt-0.5">
            {truncate(str(detail.last_error), 70)}
          </span>
        ) : null}
      </p>
    );
  }

  if (node === "clarify") {
    return (
      <p className={`${base} text-red-400`}>{truncate(str(detail.reason), 90)}</p>
    );
  }

  if (node === "finalize") {
    const z = num(detail.zestimate);
    return (
      <p className={`${base} text-slate-400`}>
        {z != null ? <span className="font-medium text-slate-700">{fmt$(z)} · </span> : null}
        {detail.address ? truncate(str(detail.address)) : null}
      </p>
    );
  }

  return null;
}

// ---------------------------------------------------------------------------
// Step icon
// ---------------------------------------------------------------------------

function StepIcon({ status }: { status: StepStatus }) {
  if (status === "running")
    return <Loader2 size={14} className="animate-spin text-indigo-500 shrink-0 mt-0.5" />;
  if (status === "done")
    return <Check size={14} className="text-emerald-500 shrink-0 mt-0.5" />;
  if (status === "error")
    return <X size={14} className="text-red-400 shrink-0 mt-0.5" />;
  return (
    <span className="w-3.5 h-3.5 rounded-full border-2 border-slate-200 shrink-0 inline-block mt-0.5" />
  );
}

const LABEL_COLOR: Record<StepStatus, string> = {
  waiting: "text-slate-300",
  running: "text-slate-800 font-medium",
  done: "text-slate-600",
  error: "text-red-500 font-medium",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface Props {
  steps: StreamStep[];
}

export default function StreamProgress({ steps }: Props) {
  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      <div className="h-1 bg-gradient-to-r from-indigo-400 via-violet-400 to-indigo-400 bg-[length:200%_100%] animate-[shimmer_1.5s_linear_infinite]" />
      <div className="p-6">
        <p className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-4">
          Processing
        </p>

        {steps.length === 0 ? (
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <Loader2 size={14} className="animate-spin" />
            Starting…
          </div>
        ) : (
          <ol className="space-y-4">
            {steps.map((step, i) => (
              <li key={i} className="flex items-start gap-3">
                <StepIcon status={step.status} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className={`text-sm leading-tight ${LABEL_COLOR[step.status]}`}>
                      {step.label}
                    </span>
                    {step.status === "running" && (
                      <span className="text-xs text-indigo-400 animate-pulse">
                        in progress
                      </span>
                    )}
                  </div>
                  {step.detail && step.status !== "running" && (
                    <DetailLine node={step.node} detail={step.detail} />
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
