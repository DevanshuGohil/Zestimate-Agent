import { useState } from "react";
import { ChevronDown, ChevronUp, Trash2, CheckCircle, AlertCircle } from "lucide-react";
import { clearCache } from "../api";

export default function AdminPanel() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  async function handleClear() {
    setStatus("loading");
    setErrorMsg("");
    try {
      await clearCache();
      setStatus("ok");
      setTimeout(() => setStatus("idle"), 3000);
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : "Failed to clear cache");
    }
  }

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-5 py-4 text-sm font-medium text-slate-500 hover:text-slate-700 transition-colors"
      >
        <span>Admin</span>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {open && (
        <div className="px-5 pb-5 border-t border-slate-100 pt-4 space-y-3">
          <button
            onClick={handleClear}
            disabled={status === "loading"}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-red-600 border border-red-200 rounded-lg hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <Trash2 size={14} />
            {status === "loading" ? "Clearing…" : "Clear Cache"}
          </button>

          {status === "ok" && (
            <p className="flex items-center gap-1.5 text-xs text-emerald-600">
              <CheckCircle size={12} />
              Cache cleared successfully.
            </p>
          )}
          {status === "error" && (
            <p className="flex items-center gap-1.5 text-xs text-red-500">
              <AlertCircle size={12} />
              {errorMsg}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
