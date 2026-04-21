import { useEffect, useState } from "react";
import { Building2, Wifi, WifiOff } from "lucide-react";
import { getHealth } from "../api";

export default function Header() {
  const [apiStatus, setApiStatus] = useState<"checking" | "ok" | "error">("checking");
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        const h = await getHealth();
        if (!cancelled) {
          setApiStatus("ok");
          setVersion(h.version);
        }
      } catch {
        if (!cancelled) setApiStatus("error");
      }
    }
    check();
    const interval = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return (
    <header className="bg-white border-b border-slate-200 px-6 py-4">
      <div className="max-w-3xl mx-auto flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="bg-indigo-600 text-white p-1.5 rounded-lg">
            <Building2 size={20} />
          </div>
          <div>
            <h1 className="font-semibold text-slate-900 text-lg leading-none">
              Zestimate Agent
            </h1>
            {version && (
              <span className="text-xs text-slate-400">v{version}</span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1.5 text-sm">
          {apiStatus === "checking" && (
            <span className="flex items-center gap-1.5 text-slate-400">
              <span className="w-2 h-2 rounded-full bg-slate-300 animate-pulse" />
              Connecting
            </span>
          )}
          {apiStatus === "ok" && (
            <span className="flex items-center gap-1.5 text-emerald-600">
              <Wifi size={14} />
              API online
            </span>
          )}
          {apiStatus === "error" && (
            <span className="flex items-center gap-1.5 text-red-500">
              <WifiOff size={14} />
              API offline
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
