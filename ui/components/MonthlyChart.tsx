export function MonthlyChart({ data }: { data: any[] }) {
  if (!data.length) return (
    <div className="h-32 flex items-center justify-center text-gray-600 text-sm">
      Pas encore de données mensuelles.
    </div>
  );
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800">
            {["Mois","Paris","Gagnés","Win %","Profit","CLV Réel"].map(h =>
              <th key={h} className="text-left px-3 py-2 text-xs text-gray-500 uppercase tracking-wider">{h}</th>
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800/40">
          {data.map((row, i) => (
            <tr key={i} className="hover:bg-gray-900/30">
              <td className="px-3 py-2 font-mono text-xs">{new Date(row.month).toLocaleDateString("fr-FR", { month: "long", year: "numeric" })}</td>
              <td className="px-3 py-2 text-center">{row.bets}</td>
              <td className="px-3 py-2 text-center text-emerald-400">{row.wins}</td>
              <td className={`px-3 py-2 text-center font-bold ${row.win_rate_pct >= 60 ? "text-emerald-400" : "text-yellow-400"}`}>{row.win_rate_pct}%</td>
              <td className={`px-3 py-2 font-mono font-bold ${row.profit_eur >= 0 ? "text-emerald-400" : "text-red-400"}`}>{row.profit_eur >= 0 ? "+" : ""}{row.profit_eur.toFixed(0)}€</td>
              <td className="px-3 py-2 text-blue-400 font-mono">{row.avg_clv_real_pct?.toFixed(2)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
