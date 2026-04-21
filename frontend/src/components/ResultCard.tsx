import { ExternalLink, Zap, Clock, Shield } from "lucide-react";
import type { ZestimateResponse, Confidence } from "../types";

const CONFIDENCE_STYLES: Record<Confidence, string> = {
  HIGH: "bg-emerald-100 text-emerald-700",
  MEDIUM: "bg-amber-100 text-amber-700",
  LOW: "bg-red-100 text-red-700",
};

function formatCurrency(n: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function formatTime(iso: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

interface Props {
  result: ZestimateResponse;
}

export default function ResultCard({ result }: Props) {
  const zillowUrl = `https://www.zillow.com/homedetails/${result.zpid}_zpid/`;

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      {/* Top accent */}
      <div className="h-1 bg-gradient-to-r from-indigo-500 to-violet-500" />

      <div className="p-6">
        {/* Address */}
        <p className="text-sm text-slate-500 font-medium truncate">{result.address}</p>

        {/* Zestimate */}
        <div className="mt-3 mb-5">
          <div className="text-4xl font-bold text-slate-900 tracking-tight">
            {formatCurrency(result.zestimate)}
          </div>
          <div className="text-sm text-slate-400 mt-0.5">Zillow Zestimate</div>
        </div>

        {/* Badges row */}
        <div className="flex flex-wrap gap-2 mb-5">
          <span
            className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${CONFIDENCE_STYLES[result.confidence]}`}
          >
            <Shield size={11} />
            {result.confidence} confidence
          </span>

          {result.cache_hit && (
            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
              <Zap size={11} />
              Cached
            </span>
          )}

          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-slate-100 text-slate-600">
            <Clock size={11} />
            {result.elapsed_ms}ms
          </span>
        </div>

        {/* Metadata */}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs mb-5">
          <div>
            <dt className="text-slate-400">ZPID</dt>
            <dd className="font-mono text-slate-600">{result.zpid}</dd>
          </div>
          <div>
            <dt className="text-slate-400">Provider</dt>
            <dd className="text-slate-600 capitalize">{result.provider_used}</dd>
          </div>
          <div className="col-span-2">
            <dt className="text-slate-400">Fetched at</dt>
            <dd className="text-slate-600">{formatTime(result.fetched_at)}</dd>
          </div>
        </dl>

        {/* Zillow link */}
        <a
          href={zillowUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-indigo-600 hover:text-indigo-800 transition-colors"
        >
          View on Zillow
          <ExternalLink size={13} />
        </a>
      </div>
    </div>
  );
}
