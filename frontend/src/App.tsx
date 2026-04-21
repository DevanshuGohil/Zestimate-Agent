import { useState, useCallback, useRef } from "react";
import { lookupStream } from "./api";
import type {
  ApiError,
  StreamStep,
  ZestimateResponse,
  HistoryEntry,
  StepEvent,
} from "./types";

import Header from "./components/Header";
import SearchForm from "./components/SearchForm";
import ResultCard from "./components/ResultCard";
import ErrorCard from "./components/ErrorCard";
import StreamProgress from "./components/StreamProgress";
import HistoryPanel from "./components/HistoryPanel";
import AdminPanel from "./components/AdminPanel";

const HISTORY_KEY = "zestimate_history";
const MAX_HISTORY = 8;

function loadHistory(): HistoryEntry[] {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) ?? "[]") as HistoryEntry[];
  } catch {
    return [];
  }
}

function saveHistory(entries: HistoryEntry[]) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
}

function applyStepEvent(steps: StreamStep[], ev: StepEvent): StreamStep[] {
  if (ev.status === "running") {
    return [...steps, { node: ev.node, label: ev.label, status: "running" }];
  }
  const updated = [...steps];
  for (let i = updated.length - 1; i >= 0; i--) {
    if (updated[i].node === ev.node && updated[i].status === "running") {
      updated[i] = { ...updated[i], status: ev.status, detail: ev.detail };
      return updated;
    }
  }
  return [...steps, { node: ev.node, label: ev.label, status: ev.status, detail: ev.detail }];
}

type SearchState =
  | { kind: "idle" }
  | { kind: "streaming"; steps: StreamStep[] }
  | { kind: "success"; result: ZestimateResponse; steps: StreamStep[] }
  | { kind: "error"; error: ApiError; steps: StreamStep[] };

export default function App() {
  const [state, setState] = useState<SearchState>({ kind: "idle" });
  const [history, setHistory] = useState<HistoryEntry[]>(loadHistory);
  const [searchAddress, setSearchAddress] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const historyRef = useRef(history);
  historyRef.current = history;

  const handleSearch = useCallback(
    async (address: string, noCache: boolean) => {
      abortRef.current?.abort();
      abortRef.current = new AbortController();
      const { signal } = abortRef.current;

      setSearchAddress(address);
      setState({ kind: "streaming", steps: [] });

      let steps: StreamStep[] = [];

      try {
        for await (const event of lookupStream(address, noCache, signal)) {
          if (signal.aborted) break;

          if (event.type === "step") {
            steps = applyStepEvent(steps, event);
            setState({ kind: "streaming", steps });
          } else if (event.type === "result") {
            const result = event.data;
            setState({ kind: "success", result, steps });

            const entry: HistoryEntry = {
              id: `${Date.now()}-${Math.random()}`,
              address,
              result,
              timestamp: Date.now(),
            };
            const updated = [entry, ...historyRef.current].slice(0, MAX_HISTORY);
            setHistory(updated);
            saveHistory(updated);
          } else if (event.type === "error") {
            setState({
              kind: "error",
              error: {
                status: event.status,
                message: event.message,
                candidates: event.candidates,
                hint: event.hint,
                zpid: event.zpid,
              },
              steps,
            });
          }
        }
      } catch (err) {
        if (signal.aborted) return;
        const apiErr = err as ApiError;
        setState({
          kind: "error",
          error: {
            status: apiErr.status ?? 0,
            message: apiErr.message ?? "Unknown error",
          },
          steps,
        });
      }
    },
    []
  );

  function handleSelectCandidate(address: string) {
    handleSearch(address, false);
  }

  function handleHistorySelect(entry: HistoryEntry) {
    setState({ kind: "success", result: entry.result, steps: [] });
    setSearchAddress(entry.result.address);
  }

  const isStreaming = state.kind === "streaming";
  const steps = state.kind !== "idle" ? state.steps : [];

  return (
    <div className="min-h-screen bg-slate-50 font-sans">
      <Header />

      <main className="max-w-3xl mx-auto px-4 py-8 space-y-5">
        <SearchForm
          key={searchAddress}
          onSearch={handleSearch}
          loading={isStreaming}
          initialAddress={searchAddress}
        />

        {/* Live progress — visible during streaming; collapses to summary after */}
        {(isStreaming || (state.kind !== "idle" && steps.length > 0)) && (
          <StreamProgress steps={steps} />
        )}

        {state.kind === "success" && <ResultCard result={state.result} />}

        {state.kind === "error" && (
          <ErrorCard
            error={state.error}
            onSelectCandidate={handleSelectCandidate}
          />
        )}

        <HistoryPanel
          entries={history}
          onSelect={handleHistorySelect}
          onClear={() => {
            setHistory([]);
            saveHistory([]);
          }}
        />

        <AdminPanel />
      </main>
    </div>
  );
}
