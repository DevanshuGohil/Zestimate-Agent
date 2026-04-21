import { useState, type FormEvent } from "react";
import { Search, ChevronDown, ChevronUp, RefreshCw } from "lucide-react";

interface Props {
  onSearch: (address: string, noCache: boolean) => void;
  loading: boolean;
  initialAddress?: string;
}

const EXAMPLES = [
  "101 Lombard St, San Francisco, CA 94111",
  "1600 Pennsylvania Ave NW, Washington, DC 20500",
  "350 Fifth Ave, New York, NY 10118",
];

export default function SearchForm({ onSearch, loading, initialAddress = "" }: Props) {
  const [address, setAddress] = useState(initialAddress);
  const [noCache, setNoCache] = useState(false);
  const [showOptions, setShowOptions] = useState(false);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (address.trim().length < 10) return;
    onSearch(address.trim(), noCache);
  }

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-6">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label
            htmlFor="address"
            className="block text-sm font-medium text-slate-700 mb-1.5"
          >
            US Property Address
          </label>
          <div className="relative">
            <input
              id="address"
              type="text"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="123 Main St, Springfield, IL 62701"
              className="w-full px-4 py-3 pr-12 text-slate-900 bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent placeholder:text-slate-400 text-sm"
              disabled={loading}
              autoFocus
            />
            <button
              type="submit"
              disabled={loading || address.trim().length < 10}
              className="absolute right-2 top-1/2 -translate-y-1/2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 text-white p-2 rounded-lg transition-colors"
            >
              {loading ? (
                <RefreshCw size={16} className="animate-spin" />
              ) : (
                <Search size={16} />
              )}
            </button>
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => setAddress(ex)}
                className="text-xs text-indigo-500 hover:text-indigo-700 hover:bg-indigo-50 px-2 py-0.5 rounded-full transition-colors truncate max-w-xs"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>

        <div>
          <button
            type="button"
            onClick={() => setShowOptions((v) => !v)}
            className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 transition-colors"
          >
            {showOptions ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            Options
          </button>

          {showOptions && (
            <div className="mt-3 pl-1">
              <label className="flex items-center gap-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={noCache}
                  onChange={(e) => setNoCache(e.target.checked)}
                  className="w-4 h-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span className="text-sm text-slate-600">
                  Bypass cache (force fresh lookup)
                </span>
              </label>
            </div>
          )}
        </div>

        <button
          type="submit"
          disabled={loading || address.trim().length < 10}
          className="w-full py-3 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-200 disabled:text-slate-400 text-white font-medium rounded-xl transition-colors text-sm"
        >
          {loading ? "Looking up Zestimate…" : "Get Zestimate"}
        </button>
      </form>
    </div>
  );
}
