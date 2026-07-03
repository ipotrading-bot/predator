import React, { useState, useEffect } from 'react';
import SettlementStats from '../components/SettlementStats';
import SettlementChart from '../components/SettlementChart';
import SettlementLedger from '../components/SettlementLedger';
import { useSettlement } from '../hooks/useSettlement';

/**
 * Settlement Dashboard — Real-time audit of signal performance
 * Route: /settlement
 * Replaces: WC 2026 page
 */

const SettlementDashboard = () => {
  const {
    stats,
    ledger,
    loading,
    error,
    refreshData,
  } = useSettlement();

  const [timeRange, setTimeRange] = useState('24h');
  const [sportFilter, setSportFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('ALL');

  useEffect(() => {
    refreshData();
    // Auto-refresh every 5 minutes
    const interval = setInterval(refreshData, 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, []);

  // Filter ledger based on UI selections
  const filteredLedger = ledger.filter((signal) => {
    const sportMatch = sportFilter === 'ALL' || signal.sport === sportFilter;
    const statusMatch = statusFilter === 'ALL' || 
      (statusFilter === 'WIN' && signal.actual_result === 1) ||
      (statusFilter === 'LOSS' && signal.actual_result === 0);
    return sportMatch && statusMatch;
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#0a0e27] via-[#0f1429] to-[#0a0e27] pb-24">
      {/* Header */}
      <div className="sticky top-0 z-40 border-b border-green-500/20 bg-[#0a0e27]/95 backdrop-blur">
        <div className="px-4 py-4">
          {/* Top bar */}
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <span className="text-2xl">⚖️</span>
              <span className="text-sm text-gray-400">PREDATOR</span>
            </div>
            <button
              onClick={refreshData}
              disabled={loading}
              className="px-3 py-1 text-xs bg-green-500/10 border border-green-500/30 rounded text-green-400 hover:bg-green-500/20 transition disabled:opacity-50"
            >
              {loading ? '⟳ Syncing...' : '⟳ Refresh'}
            </button>
          </div>

          {/* Title section */}
          <div className="mb-4">
            <h1 className="text-3xl font-bold text-white mb-1">SETTLEMENT AUDIT</h1>
            <p className="text-sm text-gray-400">
              {new Date().toLocaleString('fr-FR', {
                day: '2-digit',
                month: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
              })} UTC
            </p>
          </div>

          {/* Error state */}
          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded p-3 mb-4 text-sm text-red-300">
              {error}
            </div>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="px-4 py-6">
        {/* Stats Cards */}
        <SettlementStats stats={stats} loading={loading} />

        {/* Time Range Selector */}
        <div className="mt-8 mb-6">
          <h2 className="text-lg font-bold text-white mb-3">📊 PERFORMANCE</h2>
          <div className="flex gap-2 overflow-x-auto pb-2">
            {['24h', '7d', '30d'].map((range) => (
              <button
                key={range}
                onClick={() => setTimeRange(range)}
                className={`px-4 py-2 rounded text-sm font-medium whitespace-nowrap transition ${
                  timeRange === range
                    ? 'bg-green-500/20 border border-green-500 text-green-400'
                    : 'bg-gray-800/50 border border-gray-700 text-gray-300 hover:bg-gray-700/50'
                }`}
              >
                {range === '24h' ? '24 heures'
                  : range === '7d' ? '7 jours'
                  : '30 jours'}
              </button>
            ))}
          </div>
        </div>

        {/* Chart */}
        <div className="bg-gradient-to-br from-gray-900/50 to-gray-900/30 border border-green-500/20 rounded-lg p-4 mb-8">
          <SettlementChart timeRange={timeRange} loading={loading} />
        </div>

        {/* Filters */}
        <div className="mb-6">
          <h2 className="text-lg font-bold text-white mb-3">🔍 FILTRES</h2>
          <div className="flex gap-2 overflow-x-auto pb-2">
            {/* Sport filter */}
            <div className="flex-shrink-0">
              <label className="text-xs text-gray-400 block mb-2">Sport</label>
              <div className="flex gap-2">
                {['ALL', 'soccer', 'basketball', 'baseball', 'hockey'].map((sport) => (
                  <button
                    key={sport}
                    onClick={() => setSportFilter(sport)}
                    className={`px-3 py-1 text-xs rounded font-medium transition ${
                      sportFilter === sport
                        ? 'bg-green-500/20 border border-green-500 text-green-400'
                        : 'bg-gray-800/50 border border-gray-700 text-gray-300'
                    }`}
                  >
                    {sport === 'ALL' ? 'Tous' : sport}
                  </button>
                ))}
              </div>
            </div>

            {/* Status filter */}
            <div className="flex-shrink-0">
              <label className="text-xs text-gray-400 block mb-2">Statut</label>
              <div className="flex gap-2">
                {['ALL', 'WIN', 'LOSS'].map((status) => (
                  <button
                    key={status}
                    onClick={() => setStatusFilter(status)}
                    className={`px-3 py-1 text-xs rounded font-medium transition ${
                      statusFilter === status
                        ? 'bg-green-500/20 border border-green-500 text-green-400'
                        : 'bg-gray-800/50 border border-gray-700 text-gray-300'
                    }`}
                  >
                    {status === 'ALL' ? 'Tous' : status === 'WIN' ? '✅ WIN' : '❌ LOSS'}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Ledger Table */}
        <div>
          <h2 className="text-lg font-bold text-white mb-3">📋 LEDGER</h2>
          <SettlementLedger signals={filteredLedger} loading={loading} />
        </div>
      </div>
    </div>
  );
};

export default SettlementDashboard;
