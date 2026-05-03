"use client";
// ui/app/audit/page.tsx — Page 4 : Audit Quantitatif (Analytics)
import { useEffect, useState } from "react";
import EquityCurve from "@/components/EquityCurve";
import MonthlyChart from "@/components/MonthlyChart";

interface AuditData {
  performance: {
    total_bets: number;
    win_rate: number;
    total_profit_eur: number;
    avg_ev: number;
    avg_clv_real: number;
    avg_clv_estimate: number;
    avg_snr: number;
  };
  equity_curve: { timestamp: string; balance: number; roi: number; drawdown: number }[];
  monthly: { month: string; bets: number; wins: number; win_rate_pct: number; profit_eur: number; avg_clv_real_pct: number }[];
  brier: { brier_score: number; sample_size: number; computed_at: string };
}

function clvRating(clv: number): { label: string; cls: string } {
  if (clv >= 0.07) return { label: "Excellent ★★★", cls: "text-orange-400" };
  if (clv >= 0.05) return { label: "Bon ★★☆", cls: "text-emerald-400" };
  if (clv >= 0.02) return { label: "Correct ★☆☆", cls: "text-yellow-400" };
  return { label: "Insuffisant ☆☆☆", cls: "text-red-400" };
}

function brierRating(score: number): { label: string; cls: string } {
  if (score < 0.15) return { label: "Précision Haute", cls: "text-emerald-400" };
  if (score < 0.20) return { label: "Précision Correcte", cls: "text-yellow-400" };
  return { label: "Précision Faible", cls: "text-red-400" };
}

export default function AuditPage() {
  const [data, setData] = useState<AuditData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/audit")
      .then((r) => r.json())
      .then(setData)
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="h-64 flex items-center justify-center text-gray-500 animate-pulse">
        Chargement des métriques d'audit...
      </div>
    );
  }

  const p = data?.performance;
  const clvStatus = p?.avg_clv_real != null ? clvRating(p.avg_clv_real) : null;
  const brierStatus = data?.brier?.brier_score != null ? brierRating(data.brier.brier_score) : null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">📊 Audit Quantitatif</h1>
        <p className="text-sm text-gray-500 mt-1">
          Preuve mathématique de l'Edge — CLV, Brier Score, Equity Curve
        </p>
      </div>

      {/* CLV + Brier row */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* CLV Index */}
        <div className="col-span-1 rounded-xl border border-gray-800 bg-gray-900/50 p-5">
          <p className="text-xs text-gray-500 uppercase tracking-widest mb-1">CLV Index</p>
          <p className={`text-3xl font-bold ${clvStatus?.cls ?? "text-gray-400"}`}>
            {p?.avg_clv_real != null ? `${(p.avg_clv_real * 100).toFixed(2)}%` : "—"}
          </p>
          {clvStatus && <p className={`text-xs mt-1 ${clvStatus.cls}`}>{clvStatus.label}</p>}
          <p className="text-xs text-gray-600 mt-2">Cible : > 5%</p>
        </div>

        {/* Brier Score */}
        <div className="col-span-1 rounded-xl border border-gray-800 bg-gray-900/50 p-5">
          <p className="text-xs text-gray-500 uppercase tracking-widest mb-1">Brier Score IA</p>
          <p className={`text-3xl font-bold ${brierStatus?.cls ?? "text-gray-400"}`}>
            {data?.brier?.brier_score != null ? data.brier.brier_score.toFixed(4) : "—"}
          </p>
          {brierStatus && <p className={`text-xs mt-1 ${brierStatus.cls}`}>{brierStatus.label}</p>}
          <p className="text-xs text-gray-600 mt-2">
            {data?.brier?.sample_size ? `Sur ${data.brier.sample_size} paris` : "En attente de données"}
          </p>
        </div>

        {/* EV + SNR Moyens */}
        <div className="col-span-1 rounded-xl border border-gray-800 bg-gray-900/50 p-5">
          <p className="text-xs text-gray-500 uppercase tracking-widest mb-3">Moyennes du modèle</p>
          <div className="space-y-2">
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">EV+ moyen</span>
              <span className="text-sm font-mono font-bold text-emerald-400">
                {p?.avg_ev != null ? `${(p.avg_ev * 100).toFixed(1)}%` : "—"}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">SNR moyen</span>
              <span className="text-sm font-mono font-bold text-blue-400">
                {p?.avg_snr != null ? p.avg_snr.toFixed(2) : "—"}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Win Rate</span>
              <span className={`text-sm font-mono font-bold ${(p?.win_rate ?? 0) >= 0.6 ? "text-emerald-400" : "text-yellow-400"}`}>
                {p?.win_rate != null ? `${(p.win_rate * 100).toFixed(1)}%` : "—"}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Equity Curve */}
      <div className="rounded-xl border border-gray-800 bg-gray-900/50 p-6">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-4">
          Equity Curve — Bankroll 10 000€
        </h2>
        <EquityCurve data={data?.equity_curve ?? []} />
      </div>

      {/* Monthly Performance */}
      <div className="rounded-xl border border-gray-800 bg-gray-900/50 p-6">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-4">
          Performance Mensuelle
        </h2>
        <MonthlyChart data={data?.monthly ?? []} />
      </div>
    </div>
  );
}
