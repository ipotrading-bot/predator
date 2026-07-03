import React, { useState, useEffect } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, BarChart, Bar } from 'recharts';
import { useSettlement } from '../hooks/useSettlement';

/**
 * SettlementChart — Performance trends over time
 * Shows CLV and Win Rate trends
 */

const SettlementChart = ({ timeRange, loading }) => {
  const { getChartData } = useSettlement();
  const [chartData, setChartData] = useState([]);
  const [chartType, setChartType] = useState('clv');

  useEffect(() => {
    const data = getChartData(timeRange);
    setChartData(data);
  }, [timeRange, getChartData]);

  if (loading || !chartData.length) {
    return (
      <div className="h-64 flex items-center justify-center">
        <p className="text-gray-400">Loading chart...</p>
      </div>
    );
  }

  return (
    <div>
      {/* Chart type selector */}
      <div className="flex gap-2 mb-4">
        <button
          onClick={() => setChartType('clv')}
          className={`px-3 py-1 text-xs rounded font-medium transition ${
            chartType === 'clv'
              ? 'bg-green-500/20 border border-green-500 text-green-400'
              : 'bg-gray-800/50 border border-gray-700 text-gray-300'
          }`}
        >
          CLV Cumulative
        </button>
        <button
          onClick={() => setChartType('winrate')}
          className={`px-3 py-1 text-xs rounded font-medium transition ${
            chartType === 'winrate'
              ? 'bg-green-500/20 border border-green-500 text-green-400'
              : 'bg-gray-800/50 border border-gray-700 text-gray-300'
          }`}
        >
          Win Rate
        </button>
        <button
          onClick={() => setChartType('profit')}
          className={`px-3 py-1 text-xs rounded font-medium transition ${
            chartType === 'profit'
              ? 'bg-green-500/20 border border-green-500 text-green-400'
              : 'bg-gray-800/50 border border-gray-700 text-gray-300'
          }`}
        >
          Profit €
        </button>
      </div>

      {/* Chart */}
      <div className="h-64">
        {chartType === 'winrate' ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} margin={{ top: 10, right: 20, left: -20, bottom: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2d3748" />
              <XAxis dataKey="date" stroke="#9ca3af" style={{ fontSize: '12px' }} />
              <YAxis stroke="#9ca3af" style={{ fontSize: '12px' }} />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#1a1f3a',
                  border: '1px solid #22c55e',
                  borderRadius: '8px',
                }}
                formatter={(value) => `${(value * 100).toFixed(1)}%`}
              />
              <Bar dataKey="winRate" fill="#22c55e" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 10, right: 20, left: -20, bottom: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2d3748" />
              <XAxis dataKey="date" stroke="#9ca3af" style={{ fontSize: '12px' }} />
              <YAxis stroke="#9ca3af" style={{ fontSize: '12px' }} />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#1a1f3a',
                  border: '1px solid #22c55e',
                  borderRadius: '8px',
                }}
                formatter={(value) =>
                  chartType === 'clv' ? value.toFixed(4) : `€${value.toFixed(2)}`
                }
              />
              <Line
                type="monotone"
                dataKey={chartType === 'clv' ? 'clvCumulative' : 'profitCumulative'}
                stroke="#22c55e"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
};

export default SettlementChart;
