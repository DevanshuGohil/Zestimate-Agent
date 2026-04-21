import { AlertCircle, Info, MapPin } from "lucide-react";
import type { ApiError, Candidate } from "../types";

interface Props {
  error: ApiError;
  onSelectCandidate?: (address: string) => void;
}

function candidateLabel(c: Candidate): string {
  const parts = [
    [c.street_number, c.street_name].filter(Boolean).join(" "),
    c.city,
    c.state,
    c.zip5,
  ].filter(Boolean);
  return parts.join(", ");
}

const STATUS_LABELS: Record<number, string> = {
  401: "Authentication required",
  403: "Forbidden",
  404: "No Zestimate available",
  422: "Address needs clarification",
  429: "Rate limit exceeded",
  500: "Server error",
  503: "Provider unavailable",
  504: "Request timed out",
};

export default function ErrorCard({ error, onSelectCandidate }: Props) {
  const title = STATUS_LABELS[error.status] ?? `Error ${error.status}`;
  const isAmbiguous = error.status === 422 && error.candidates && error.candidates.length > 0;
  const isNoZestimate = error.status === 404;

  return (
    <div className="bg-white rounded-2xl border border-red-100 shadow-sm overflow-hidden">
      <div className="h-1 bg-red-400" />
      <div className="p-6">
        <div className="flex items-start gap-3 mb-3">
          <div className="mt-0.5 text-red-500 shrink-0">
            <AlertCircle size={18} />
          </div>
          <div>
            <h3 className="font-semibold text-slate-800 text-sm">{title}</h3>
            <p className="text-sm text-slate-500 mt-0.5">{error.message}</p>
          </div>
        </div>

        {/* Hint for 404 */}
        {isNoZestimate && error.hint && (
          <div className="flex items-start gap-2 mt-3 p-3 bg-amber-50 rounded-lg text-xs text-amber-700">
            <Info size={13} className="shrink-0 mt-0.5" />
            {error.hint}
          </div>
        )}

        {/* Candidate picker for 422 */}
        {isAmbiguous && (
          <div className="mt-4">
            <p className="text-xs font-medium text-slate-500 mb-2">
              Did you mean one of these?
            </p>
            <ul className="space-y-1.5">
              {error.candidates!.map((c, i) => {
                const label = candidateLabel(c);
                return (
                  <li key={i}>
                    <button
                      onClick={() => onSelectCandidate?.(label)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left rounded-lg hover:bg-indigo-50 hover:text-indigo-700 transition-colors text-slate-600 border border-slate-100"
                    >
                      <MapPin size={13} className="shrink-0 text-slate-400" />
                      {label || `Candidate ${i + 1}`}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* Rate limit hint */}
        {error.status === 429 && (
          <p className="mt-3 text-xs text-slate-400">
            Wait a moment before trying again, or use an API key for a dedicated rate-limit bucket.
          </p>
        )}

      </div>
    </div>
  );
}
