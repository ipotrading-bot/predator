"use client";
// ui/app/page.tsx — Page 1 : Terminal de Contrôle
import { useEffect, useState, useCallback } from "react";
import CountdownTimer from "@/components/CountdownTimer";
import StatusBadge from "@/components/StatusBadge";
import ScanLogTable from "@/components/ScanLogTable";

interface ScanLog {
  session_name: string;
  events_analyzed: number;
  signals_validated: number;
  duration_seconds: number;
  scanned_at: string;
}

interface AuditData {
  performance: {
    total_bets: number;
    win_rate: number;
    total_profit_eur: number;
    avg_clv_real: number;
  };
  scan_logs: ScanLog[];
}

export default function TerminalPage() {
  const [data, setData] = useState<AuditData | null>(null);
  const [scanning, setScanning] = useState(false);
  const [lastScan, setLastScan] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const r = await fetch("/api/audit");
      const d = await r.json();
      setData(d);
    } catch (e) {
      setError("Erreur de connexion à l'API");
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 60_000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const forceScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const r = await fetch("/api/scan", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Predator-Secret": process.env.NEXT_PUBLIC_PREDATOR_SECRET || "",
        },
        body: JSON.stringify({ session: "force" }),
      });
      const result = await r.json();
      if (result.success) {
        setLastScan(new Date().toISOString());
        await fetchData();
      } else {
        setError(result.error || "Scan échoué");
      }
    } catch (e) {
      setError("Erreur réseau lors du scan");
    } finally {
      setScanning(false);
    }
  };

  const perf = data?.performance;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            🦅 Terminal de Contrôle
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Predator PAIM v2.0 — Algorithmic Information Arbitrage
          </p>
        </div>
        <StatusBadge scanning={scanning} />
      </div>

      {error && (
        <div className="rounded-lg bg-red-950 border border-red-800 px-4 py-3 text-red-300 text-sm">
          ⚠️ {error}
        </div>
      )}

      {/* KPI Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KPICard
          label="Profit Net"
          value={`${perf?.total_profit_eur != null ? (perf.total_profit_eur >= 0 ? "+" : "") + perf.total_profit_eur.toFixed(0) : "—"}€`}
          positive={perf ? perf.total_profit_eur >= 0 : null}
        />
        <KPICard
          label="Win Rate"
          value={perf?.win_rate != null ? `${(perf.win_rate * 100).toFixed(1)}%` : "—"}
          positive={perf ? perf.win_rate >= 0.6 : null}
        />
        <KPICard
          label="CLV Moyen"
          value={perf?.avg_clv_real != null ? `${(perf.avg_clv_real * 100).toFixed(2)}%` : "—"}
          positive={perf ? perf.avg_clv_real >= 0.05 : null}
        />
        <KPICard
          label="Total Paris"
          value={perf?.total_bets?.toString() ?? "—"}
          positive={null}
        />
      </div>

      {/* Countdown + Force Scan */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="rounded-xl border border-gray-800 bg-gray-900/50 p-6">
          <p className="text-xs text-gray-500 uppercase tracking-widest mb-3">
            Prochain scan automatique
          </p>
          <CountdownTimer />
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-900/50 p-6 flex flex-col justify-between">
          <p className="text-xs text-gray-500 uppercase tracking-widest mb-3">
            Scan manuel
          </p>
          <button
            onClick={forceScan}
            disabled={scanning}
            className="w-full py-3 rounded-lg font-bold text-sm tracking-wider
              bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-700
              disabled:text-gray-500 transition-all duration-150
              border border-emerald-500 disabled:border-gray-600"
          >
            {scanning ? "⏳ SCAN EN COURS..." : "⚡ FORCE SCAN"}
          </button>
          {lastScan && (
            <p className="text-xs text-gray-600 mt-2 text-center">
              Dernier : {new Date(lastScan).toLocaleTimeString("fr-FR")}
            </p>
          )}
        </div>
      </div>

      {/* Scan Logs */}
      <div className="rounded-xl border border-gray-800 bg-gray-900/50 p-6">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-4">
          Journal des scans récents
        </h2>
        <ScanLogTable logs={data?.scan_logs ?? []} />
      </div>
    </div>
  );
}

function KPICard({
  label,
  value,
  positive,
}: {
  label: string;
  value: string;
  positive: boolean | null;
}) {
  const color =
    positive === null
      ? "text-gray-100"
      : positive
      ? "text-emerald-400"
      : "text-red-400";
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/50 px-5 py-4">
      <p className="text-xs text-gray-500 uppercase tracking-widest mb-1">{label}</p>
      <p className={`text-2xl font-bold ${color}`}>{value}</p>
    </div>
  );
}
