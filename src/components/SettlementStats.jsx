import React from 'react';

/**
 * SettlementStats — Display key metrics
 * Win Rate, CLV, Profit, Signal Count
 */

const SettlementStats = ({ stats, loading }) => {
  const {
    totalSignals = 0,
    settledSignals = 0,
    wins = 0,
    losses = 0,
    winRate = 0,
    clvTotal = 0,
    profitTotal = 0,
    avgClv = 0,
  } = stats || {};

  const statCards = [
    {
      label: 'Win Rate',
      value: `${(winRate * 100).toFixed(1)}%`,
      icon: '📊',
      color: winRate > 0.5 ? 'green' : 'yellow',
      subtext: `${wins}W / ${losses}L`,
    },
    {
      label: 'CLV Total',
      value: clvTotal.toFixed(4),
      icon: '💎',
      color: clvTotal > 0 ? 'green' : 'red',
      subtext: `Avg: ${avgClv.toFixed(4)}`,
    },
    {
      label: 'Profit',
      value: `€${profitTotal.toFixed(2)}`,
      icon: '💰',
      color: profitTotal > 0 ? 'green' : 'red',
      subtext: `on €${(settledSignals * 2.5).toFixed(0)}`,
    },
    {
      label: 'Settlés',
      value: `${settledSignals}/${totalSignals}`,
      icon: '✅',
      color: 'blue',
      subtext: `${totalSignals - settledSignals} pending`,
    },
  ];

  const colorClasses = {
    green: 'border-green-500/30 bg-green-500/5 text-green-400',
    red: 'border-red-500/30 bg-red-500/5 text-red-400',
    yellow: 'border-yellow-500/30 bg-yellow-500/5 text-yellow-400',
    blue: 'border-blue-500/30 bg-blue-500/5 text-blue-400',
  };

  return (
    <div className="grid grid-cols-2 gap-3 mb-8">
      {statCards.map((card) => (
        <div
          key={card.label}
          className={`border rounded-lg p-4 transition ${
            loading ? 'opacity-50' : 'opacity-100'
          } ${colorClasses[card.color]}`}
        >
          <div className="flex items-center justify-between mb-2">
            <span className="text-2xl">{card.icon}</span>
            <span className="text-xs text-gray-400 uppercase">{card.label}</span>
          </div>
          <div className="text-2xl font-bold text-white mb-1">{card.value}</div>
          <div className="text-xs text-gray-400">{card.subtext}</div>
        </div>
      ))}
    </div>
  );
};

export default SettlementStats;
