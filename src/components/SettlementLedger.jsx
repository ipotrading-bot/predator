import React, { useState } from 'react';

/**
 * SettlementLedger — Detailed table of all settled signals
 * Shows match, result, CLV, profit, etc.
 */

const SettlementLedger = ({ signals, loading }) => {
  const [sortBy, setSortBy] = useState('date');
  const [sortOrder, setSortOrder] = useState('desc');

  const sorted = [...signals].sort((a, b) => {
    let aVal, bVal;

    switch (sortBy) {
      case 'date':
        aVal = new Date(a.created_at);
        bVal = new Date(b.created_at);
        break;
      case 'clv':
        aVal = a.clv_final || 0;
        bVal = b.clv_final || 0;
        break;
      case 'profit':
        aVal = a.profit_units || 0;
        bVal = b.profit_units || 0;
        break;
      case 'edge':
        aVal = a.initial_edge || 0;
        bVal = b.initial_edge || 0;
        break;
      default:
        return 0;
    }

    if (sortOrder === 'asc') return aVal > bVal ? 1 : -1;
    return aVal < bVal ? 1 : -1;
  });

  if (loading) {
    return (
      <div className="bg-gradient-to-br from-gray-900/50 to-gray-900/30 border border-green-500/20 rounded-lg p-4">
        <p className="text-gray-400 text-center">Loading ledger...</p>
      </div>
    );
  }

  if (!sorted.length) {
    return (
      <div className="bg-gradient-to-br from-gray-900/50 to-gray-900/30 border border-green-500/20 rounded-lg p-4">
        <p className="text-gray-400 text-center">No signals settled yet</p>
      </div>
    );
  }

  return (
    <div className="bg-gradient-to-br from-gray-900/50 to-gray-900/30 border border-green-500/20 rounded-lg overflow-hidden">
      {/* Table header */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-green-500/20 bg-gray-900/50">
              <th
                className="px-4 py-3 text-left text-xs font-bold text-green-400 cursor-pointer hover:bg-gray-800/50 transition"
                onClick={() => {
                  if (sortBy === 'date') {
                    setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
                  } else {
                    setSortBy('date');
                    setSortOrder('desc');
                  }
                }}
              >
                Match {sortBy === 'date' && (sortOrder === 'asc' ? '↑' : '↓')}
              </th>
              <th className="px-4 py-3 text-left text-xs font-bold text-green-400">Sport</th>
              <th className="px-4 py-3 text-left text-xs font-bold text-green-400">Marché</th>
              <th className="px-4 py-3 text-left text-xs font-bold text-green-400">Cote</th>
              <th className="px-4 py-3 text-left text-xs font-bold text-green-400">Résultat</th>
              <th
                className="px-4 py-3 text-right text-xs font-bold text-green-400 cursor-pointer hover:bg-gray-800/50 transition"
                onClick={() => {
                  if (sortBy === 'clv') {
                    setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
                  } else {
                    setSortBy('clv');
                    setSortOrder('desc');
                  }
                }}
              >
                CLV {sortBy === 'clv' && (sortOrder === 'asc' ? '↑' : '↓')}
              </th>
              <th
                className="px-4 py-3 text-right text-xs font-bold text-green-400 cursor-pointer hover:bg-gray-800/50 transition"
                onClick={() => {
                  if (sortBy === 'profit') {
                    setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
                  } else {
                    setSortBy('profit');
                    setSortOrder('desc');
                  }
                }}
              >
                Profit {sortBy === 'profit' && (sortOrder === 'asc' ? '↑' : '↓')}
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((signal, idx) => (
              <tr
                key={signal.id || idx}
                className="border-b border-gray-800/50 hover:bg-gray-800/30 transition"
              >
                {/* Match */}
                <td className="px-4 py-3 text-white text-sm font-medium">
                  {signal.match || 'N/A'}
                </td>

                {/* Sport */}
                <td className="px-4 py-3 text-gray-300 text-sm">
                  <span className="inline-block px-2 py-1 bg-gray-800/50 rounded text-xs">
                    {signal.sport}
                  </span>
                </td>

                {/* Market */}
                <td className="px-4 py-3 text-gray-300 text-sm max-w-xs truncate">
                  {signal.market_type || 'N/A'}
                </td>

                {/* Odds */}
                <td className="px-4 py-3 text-gray-300 text-sm">
                  @{(signal.xbet_odd || 0).toFixed(2)}
                </td>

                {/* Result */}
                <td className="px-4 py-3 text-sm font-medium">
                  {signal.actual_result === 1 ? (
                    <span className="text-green-400">✅ WIN</span>
                  ) : signal.actual_result === 0 ? (
                    <span className="text-red-400">❌ LOSS</span>
                  ) : (
                    <span className="text-gray-400">⏳ PENDING</span>
                  )}
                </td>

                {/* CLV */}
                <td className="px-4 py-3 text-sm text-right font-mono">
                  <span
                    className={`${
                      signal.was_clv_positive
                        ? 'text-green-400'
                        : 'text-red-400'
                    }`}
                  >
                    {(signal.clv_final || 0).toFixed(4)}
                  </span>
                </td>

                {/* Profit */}
                <td className="px-4 py-3 text-sm text-right font-mono">
                  <span
                    className={`${
                      (signal.profit_units || 0) > 0
                        ? 'text-green-400'
                        : 'text-red-400'
                    }`}
                  >
                    €{(signal.profit_units || 0).toFixed(2)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Footer */}
      <div className="px-4 py-3 bg-gray-900/50 border-t border-green-500/20 text-xs text-gray-400">
        {sorted.length} signal(s) affichés
      </div>
    </div>
  );
};

export default SettlementLedger;
