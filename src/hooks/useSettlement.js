import { useState, useCallback } from 'react';
import { supabase } from '../lib/supabaseClient';

/**
 * useSettlement — Real-time data fetching from Supabase
 * Fetches signals + ai_learning_ledger for performance analysis
 */

export const useSettlement = () => {
  const [stats, setStats] = useState(null);
  const [ledger, setLedger] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const refreshData = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      // Fetch all signals with settlement data
      const { data: signals, error: signalsError } = await supabase
        .from('signals')
        .select(`
          id,
          match,
          sport,
          league,
          market,
          xbet_odd,
          sharp_prob,
          consensus_score,
          edge_pct,
          scanned_at,
          match_time,
          ai_learning_ledger (
            id,
            actual_result,
            clv_final,
            profit_units,
            was_clv_positive,
            initial_edge,
            created_at
          )
        `)
        .order('scanned_at', { ascending: false });

      if (signalsError) throw signalsError;

      // Transform and flatten data
      const settlementData = signals
        .filter((sig) => sig.ai_learning_ledger && sig.ai_learning_ledger.length > 0)
        .flatMap((sig) =>
          sig.ai_learning_ledger.map((ledg) => ({
            id: sig.id,
            signal_id: sig.id,
            match: sig.match,
            sport: sig.sport,
            league: sig.league,
            market_type: sig.market,
            xbet_odd: sig.xbet_odd,
            sharp_prob: sig.sharp_prob,
            ai_confidence_score: sig.consensus_score,
            initial_edge: sig.edge_pct,
            actual_result: ledg.actual_result,
            clv_final: ledg.clv_final,
            profit_units: ledg.profit_units,
            was_clv_positive: ledg.was_clv_positive,
            created_at: ledg.created_at || sig.scanned_at,
          }))
        );

      setLedger(settlementData);

      // Calculate stats
      const totalSignals = settlementData.length;
      const wins = settlementData.filter((s) => s.actual_result === 1).length;
      const losses = settlementData.filter((s) => s.actual_result === 0).length;
      const winRate = totalSignals > 0 ? wins / totalSignals : 0;
      const clvTotal = settlementData.reduce((sum, s) => sum + (s.clv_final || 0), 0);
      const profitTotal = settlementData.reduce((sum, s) => sum + (s.profit_units || 0), 0);
      const avgClv = totalSignals > 0 ? clvTotal / totalSignals : 0;

      setStats({
        totalSignals,
        settledSignals: totalSignals,
        wins,
        losses,
        winRate,
        clvTotal,
        profitTotal,
        avgClv,
      });
    } catch (err) {
      console.error('Settlement fetch error:', err);
      setError(`Failed to load data: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  const getChartData = useCallback(
    (timeRange) => {
      if (!ledger.length) return [];

      const now = new Date();
      const startDate = new Date();

      if (timeRange === '24h') {
        startDate.setDate(now.getDate() - 1);
      } else if (timeRange === '7d') {
        startDate.setDate(now.getDate() - 7);
      } else if (timeRange === '30d') {
        startDate.setDate(now.getDate() - 30);
      }

      // Group by date
      const grouped = {};
      ledger.forEach((sig) => {
        const date = new Date(sig.created_at);
        if (date < startDate) return;

        const dateKey = date.toISOString().split('T')[0];
        if (!grouped[dateKey]) {
          grouped[dateKey] = {
            date: dateKey,
            signals: [],
            wins: 0,
            losses: 0,
            clvSum: 0,
            profitSum: 0,
          };
        }

        grouped[dateKey].signals.push(sig);
        if (sig.actual_result === 1) grouped[dateKey].wins++;
        else if (sig.actual_result === 0) grouped[dateKey].losses++;
        grouped[dateKey].clvSum += sig.clv_final || 0;
        grouped[dateKey].profitSum += sig.profit_units || 0;
      });

      // Calculate cumulative and percentages
      const chartData = Object.values(grouped)
        .sort((a, b) => new Date(a.date) - new Date(b.date))
        .reduce((acc, item, idx) => {
          const clvCumulative =
            idx === 0 ? item.clvSum : acc[idx - 1].clvCumulative + item.clvSum;
          const profitCumulative =
            idx === 0 ? item.profitSum : acc[idx - 1].profitCumulative + item.profitSum;
          const totalSignals = item.wins + item.losses;
          const winRate = totalSignals > 0 ? item.wins / totalSignals : 0;

          return [
            ...acc,
            {
              date: item.date.substring(5),
              clvCumulative,
              profitCumulative,
              winRate,
              dailyWins: item.wins,
              dailyLosses: item.losses,
            },
          ];
        }, []);

      return chartData;
    },
    [ledger]
  );

  return {
    stats,
    ledger,
    loading,
    error,
    refreshData,
    getChartData,
  };
};
