export function ScanLogTable({ logs }: { logs: any[] }) {
  if (!logs.length) return (
    <p className="text-sm text-gray-600 text-center py-6">Aucun scan enregistré.</p>
  );
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-gray-800">
          {["Session","Events","Signaux","Durée","Heure"].map(h =>
            <th key={h} className="text-left px-3 py-2 text-xs text-gray-500 uppercase tracking-wider">{h}</th>
          )}
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-800/30">
        {logs.map((log, i) => (
          <tr key={i} className="hover:bg-gray-800/20">
            <td className="px-3 py-2 font-medium">{log.session_name}</td>
            <td className="px-3 py-2 text-gray-400">{log.events_analyzed}</td>
            <td className="px-3 py-2 text-emerald-400 font-bold">{log.signals_validated}</td>
            <td className="px-3 py-2 font-mono text-xs text-gray-400">{log.duration_seconds?.toFixed(1)}s</td>
            <td className="px-3 py-2 text-xs text-gray-500 font-mono">
              {new Date(log.scanned_at).toLocaleString("fr-FR", { day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit" })}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
