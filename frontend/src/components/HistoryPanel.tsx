import { Clock, Trash2 } from "lucide-react";
import type { HistoryEntry, Confidence } from "../types";

const CONFIDENCE_DOT: Record<Confidence, string> = {
  HIGH: "bg-emerald-500",
  MEDIUM: "bg-amber-400",
  LOW: "bg-red-400",
};

function formatCurrency(n: number) {
  if (n >= 1_000_000)
    return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000)
    return `$${Math.round(n / 1_000)}K`;
  return `$${n}`;
}

function relativeTime(ts: number) {
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

interface Props {
  entries: HistoryEntry[];
  onSelect: (entry: HistoryEntry) => void;
  onClear: () => void;
}

export default function HistoryPanel({ entries, onSelect, onClear }: Props) {
  if (entries.length === 0) return null;

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-1.5 text-sm font-medium text-slate-600">
          <Clock size={14} />
          Recent Searches
        </div>
        <button
          onClick={onClear}
          className="text-slate-400 hover:text-red-400 transition-colors"
          title="Clear history"
        >
          <Trash2 size={14} />
        </button>
      </div>

      <ul className="space-y-1">
        {entries.map((e) => (
          <li key={e.id}>
            <button
              onClick={() => onSelect(e)}
              className="w-full flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-slate-50 transition-colors text-left group"
            >
              <span
                className={`w-2 h-2 rounded-full shrink-0 ${CONFIDENCE_DOT[e.result.confidence]}`}
              />
              <span className="flex-1 text-sm text-slate-700 truncate">
                {e.result.address}
              </span>
              <span className="text-sm font-medium text-slate-500 shrink-0">
                {formatCurrency(e.result.zestimate)}
              </span>
              <span className="text-xs text-slate-400 shrink-0 group-hover:text-slate-500">
                {relativeTime(e.timestamp)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
