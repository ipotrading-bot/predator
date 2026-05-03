"use client";
// ui/app/signals/page.tsx — Page 2 : Live Signals — Ticket 7/9
import { useEffect, useState } from "react";

interface Signal {
  id: string;
  event_name: string;
  sport: string;
  match_time: string;
  market_key: string;
  selection: string;
  bookmaker_target: string;
  ev_plus: number;
  snr_ratio: number;
  sharp_prob: number;
  implied_prob_soft: number;
  recommended_stake: number;
  clv_estimate: number;
  ai_context: string;
  status: string;
}

const SPORT_EMOJI: Record<string, string> = {
  soccer: "⚽", basketball: "🏀", tennis: "🎾",
  baseball: "⚾", icehockey: "🏒", mma: "🥊",
  volleyball: "🏐", esports: "🎮",
};

function sportIcon(sport: string): string {
  const key = Object.keys(SPORT_EMOJI).find((k) => sport.includes(k));
  return key ? SPORT_EMOJI[key] : "🏅";
}

function xbetUrl(event_name: string): string {
  const q = encodeURIComponent(event_name);
  return `https://1xbet.com/en/search?q=${q}`;
}

export default function SignalsPage() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  const load = async () => {
    try {
      const r = await fetch("/api/signals?limit=9&status=pending");
      const d = await r.json();
      setSignals(d.signals ?? []);
      setLastUpdate(new Date());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const iv = setInterval(load, 60_000);
    return () => clearInterval(iv);
  }, []);

  const combos = (n: number, k: number): number => {
    if (n < k) return 0;
    const fact = (x: number): number => (x <= 1 ? 1 : x * fact(x - 1));
    let s = 0;
    for (let i = k; i <= n; i++) s += fact(n) / (fact(i) * fact(n - i));
    return s;
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">⚡ Live Signals — Ticket 7/9</h1>
          <p className="text-sm text-gray-500 mt-1">
            {signals.length}/9 sélections · {combos(signals.length, 7)} combinaisons
            {lastUpdate && ` · MAJ ${lastUpdate.toLocaleTimeString("fr-FR")}`}
          </p>
        </div>
        <button
          onClick={load}
          className="px-4 py-2 rounded-lg border border-gray-700 text-sm
            hover:border-gray-500 transition-colors"
        >
          🔄 Actualiser
        </button>
      </div>

      {/* System banner */}
      {signals.length >= 7 && (
        <div className="rounded-lg bg-emerald-950 border border-emerald-700 px-5 py-3 flex items-center gap-3">
          <span className="text-emerald-400 text-xl">✅</span>
          <div>
            <p className="text-emerald-300 font-semibold text-sm">
              Ticket Système {signals.length}/9 complet
            </p>
            <p className="text-emerald-600 text-xs">
              {combos(signals.length, 7)} combinaisons · Profit garanti dès 7 bons résultats
            </p>
          </div>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center h-48 text-gray-500">
          <div className="animate-pulse">Chargement des signaux...</div>
        </div>
      ) : signals.length === 0 ? (
        <div className="rounded-xl border border-gray-800 bg-gray-900/50 p-12 text-center">
          <p className="text-gray-500">Aucun signal actif pour ce cycle.</p>
          <p className="text-gray-600 text-sm mt-2">
            Prochain scan automatique dans moins de 8h.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
          {signals.map((sig, i) => (
            <SignalCard key={sig.id} signal={sig} index={i + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

function SignalCard({ signal, index }: { signal: Signal; index: number }) {
  const ev = signal.ev_plus * 100;
  const evClass = ev >= 15 ? "text-orange-400" : ev >= 10 ? "text-emerald-400" : "text-emerald-300";
  const evBorder = ev >= 15 ? "border-orange-800" : "border-emerald-900";
  const snrFill = Math.min(Math.round((signal.snr_ratio / 5) * 5), 5);

  return (
    <div className={`rounded-xl border ${evBorder} bg-gray-900/60 p-5 space-y-3 hover:bg-gray-900 transition-colors`}>
      {/* Top row */}
      <div className="flex items-start justify-between">
        <div>
          <span className="text-xs text-gray-500 mr-2">#{index}</span>
          <span className="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded-full">
            {sportIcon(signal.sport)} {signal.sport.split("_")[0]}
          </span>
        </div>
        <span className={`text-xs font-bold ${evClass}`}>EV+ {ev.toFixed(1)}%</span>
      </div>

      {/* Event name */}
      <p className="font-semibold text-sm leading-tight">{signal.event_name}</p>

      {/* Selection */}
      <div className="flex items-center gap-2">
        <span className="px-3 py-1 rounded-full bg-emerald-900/50 border border-emerald-700 text-emerald-300 font-bold text-sm uppercase tracking-wider">
          {signal.selection}
        </span>
        <span className="text-xs text-gray-500">via {signal.bookmaker_target.toUpperCase()}</span>
      </div>

      {/* Métriques */}
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="bg-gray-800/50 rounded-lg px-3 py-2">
          <p className="text-gray-500">Sharp Prob</p>
          <p className="font-mono font-bold">{(signal.sharp_prob * 100).toFixed(1)}%</p>
        </div>
        <div className="bg-gray-800/50 rounded-lg px-3 py-2">
          <p className="text-gray-500">Soft Prob</p>
          <p className="font-mono font-bold">{(signal.implied_prob_soft * 100).toFixed(1)}%</p>
        </div>
        <div className="bg-gray-800/50 rounded-lg px-3 py-2">
          <p className="text-gray-500">CLV Est.</p>
          <p className="font-mono font-bold">{(signal.clv_estimate * 100).toFixed(2)}%</p>
        </div>
        <div className="bg-gray-800/50 rounded-lg px-3 py-2">
          <p className="text-gray-500">SNR</p>
          <p className="font-mono font-bold">{"█".repeat(snrFill)}{"░".repeat(5 - snrFill)}</p>
        </div>
      </div>

      {/* Mise + match time */}
      <div className="flex items-center justify-between pt-1 border-t border-gray-800">
        <div>
          <p className="text-xs text-gray-500">Mise Kelly</p>
          <p className="font-bold text-emerald-400">{signal.recommended_stake.toFixed(0)}€</p>
        </div>
        <div className="text-right">
          <p className="text-xs text-gray-500">Match</p>
          <p className="text-xs font-mono">
            {new Date(signal.match_time).toLocaleDateString("fr-FR", {
              weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
            })}
          </p>
        </div>
      </div>

      {/* AI Context */}
      {signal.ai_context && (
        <p className="text-xs text-gray-500 italic border-t border-gray-800 pt-2">
          🤖 {signal.ai_context}
        </p>
      )}

      {/* 1XBet Link */}
      <a
        href={xbetUrl(signal.event_name)}
        target="_blank"
        rel="noopener noreferrer"
        className="block w-full text-center py-2 rounded-lg border border-gray-700
          text-xs text-gray-400 hover:border-emerald-700 hover:text-emerald-400
          transition-colors"
      >
        Ouvrir sur 1XBet →
      </a>
    </div>
  );
}
