"use client";
// ui/app/ledger/page.tsx — Page 3 : Ledger — Historique & Résultats
import { useEffect, useState } from "react";

interface Signal {
  id: string;
  event_name: string;
  sport: string;
  match_time: string;
  selection: string;
  bookmaker_target: string;
  ev_plus: number;
  recommended_stake: number;
  outcome: number | null;
  profit_eur: number | null;
  clv_real: number | null;
  status: string;
  created_at: string;
}

const OUTCOME_MAP: Record<number, { label: string; cls: string }> = {
  1:  { label: "✅ Gagné",      cls: "text-emerald-400 bg-emerald-950 border-emerald-800" },
  0:  { label: "❌ Perdu",      cls: "text-red-400 bg-red-950 border-red-800" },
  [-1]: { label: "↩️ Remboursé", cls: "text-gray-400 bg-gray-800 border-gray-700" },
};

export default function LedgerPage() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"all" | "settled" | "pending">("all");

  useEffect(() => {
    const status = filter === "all" ? "settled" : filter;
    fetch(`/api/signals?limit=50&status=${status}`)
      .then((r) => r.json())
      .then((d) => setSignals(d.signals ?? []))
      .finally(() => setLoading(false));
  }, [filter]);

  const totalProfit = signals.reduce((s, sig) => s + (sig.profit_eur ?? 0), 0);
  const wins = signals.filter((s) => s.outcome === 1).length;
  const settled = signals.filter((s) => s.outcome !== null).length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">📋 Ledger — Historique</h1>
          <p className="text-sm text-gray-500 mt-1">
            {settled} paris réglés · {wins}/{settled} gagnés ·{" "}
            <span className={totalProfit >= 0 ? "text-emerald-400" : "text-red-400"}>
              {totalProfit >= 0 ? "+" : ""}{totalProfit.toFixed(0)}€
            </span>
          </p>
        </div>
        <div className="flex gap-2">
          {(["all", "settled", "pending"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                filter === f
                  ? "bg-gray-700 border-gray-500 text-white"
                  : "border-gray-800 text-gray-500 hover:border-gray-600"
              }`}
            >
              {f === "all" ? "Tous" : f === "settled" ? "Réglés" : "En cours"}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="h-48 flex items-center justify-center text-gray-500 animate-pulse">
          Chargement...
        </div>
      ) : (
        <div className="rounded-xl border border-gray-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-900 border-b border-gray-800">
                <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">Événement</th>
                <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">Sélection</th>
                <th className="text-right px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">EV+</th>
                <th className="text-right px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">Mise</th>
                <th className="text-right px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">CLV Réel</th>
                <th className="text-center px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">Résultat</th>
                <th className="text-right px-4 py-3 text-xs text-gray-500 font-medium uppercase tracking-wider">P&L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {signals.map((sig) => {
                const outcome = sig.outcome != null ? OUTCOME_MAP[sig.outcome] : null;
                const pnlClass = (sig.profit_eur ?? 0) >= 0 ? "text-emerald-400" : "text-red-400";
                return (
                  <tr key={sig.id} className="hover:bg-gray-900/50 transition-colors">
                    <td className="px-4 py-3">
                      <p className="font-medium truncate max-w-[180px]">{sig.event_name}</p>
                      <p className="text-xs text-gray-500">
                        {new Date(sig.match_time).toLocaleDateString("fr-FR", {
                          day: "numeric", month: "short",
                        })}
                      </p>
                    </td>
                    <td className="px-4 py-3">
                      <span className="px-2 py-0.5 rounded text-xs font-mono uppercase bg-gray-800 text-gray-300">
                        {sig.selection}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-emerald-400 text-xs">
                      {(sig.ev_plus * 100).toFixed(1)}%
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs">
                      {sig.recommended_stake.toFixed(0)}€
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs text-gray-400">
                      {sig.clv_real != null ? `${(sig.clv_real * 100).toFixed(2)}%` : "—"}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {outcome ? (
                        <span className={`px-2 py-0.5 rounded-full text-xs border ${outcome.cls}`}>
                          {outcome.label}
                        </span>
                      ) : (
                        <span className="text-xs text-gray-600">En attente</span>
                      )}
                    </td>
                    <td className={`px-4 py-3 text-right font-mono font-bold text-sm ${pnlClass}`}>
                      {sig.profit_eur != null
                        ? `${sig.profit_eur >= 0 ? "+" : ""}${sig.profit_eur.toFixed(0)}€`
                        : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {signals.length === 0 && (
            <div className="text-center py-12 text-gray-500 text-sm">
              Aucun résultat trouvé.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
