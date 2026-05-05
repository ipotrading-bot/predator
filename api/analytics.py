"""
api/analytics.py — QuantStats Integration for Professional Analytics
Génère des rapports financiers complets : Sharpe, Sortino, Drawdown, VaR, etc.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from fpdf import FPDF

from supabase import create_client


class QuantAnalytics:
    """Analytics engine using QuantStats methodology."""
    
    def __init__(self):
        self.supabase = None  # Lazy init
    
    def _get_supabase(self):
        """Lazy initialize Supabase client."""
        if self.supabase is not None:
            return self.supabase
        try:
            from supabase import create_client
            url = os.environ.get("SUPABASE_URL", "")
            key = os.environ.get("SUPABASE_KEY", "")
            if not url or not key:
                self.supabase = None
            else:
                self.supabase = create_client(url, key)
        except Exception:
            self.supabase = None
        return self.supabase
    
    def get_performance_report(self, days: int = 30) -> dict:
        """
        Génère un rapport de performance complet.
        
        Returns:
            dict: Métriques de performance style hedge fund
        """
        try:
            supabase = self._get_supabase()
            if not supabase:
                return self._get_default_report()
            
            # Fetch historical data from Supabase
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            
            # Get signals with outcomes
            res = supabase.table("signals")\
                .select("created_at, recommended_stake, odds, outcome, profit_eur, ev_plus")\
                .gte("created_at", start_date.isoformat())\
                .not_.is_("outcome", None)\
                .order("created_at", desc=False)\
                .execute()
            
            if not res.data:
                return self._get_default_report()
            
            # Convert to DataFrame
            df = pd.DataFrame(res.data)
            df["created_at"] = pd.to_datetime(df["created_at"])
            df["profit"] = df["profit_eur"].fillna(0)
            df["stake"] = df["recommended_stake"].fillna(0)
            df["return_pct"] = df["profit"] / df["stake"].replace(0, np.nan)
            
            # Calculate cumulative returns
            df["cumulative_return"] = df["profit"].cumsum()
            
            # Daily returns for Sharpe/Sortino
            daily_returns = df.groupby(df["created_at"].dt.date)["profit"].sum()
            starting_bankroll = float(os.environ.get("STARTING_BANKROLL", "10000"))
            daily_returns_pct = daily_returns / starting_bankroll
            
            # Core metrics
            total_trades = len(df)
            # CORRECT: outcome is SMALLINT 1/0/-1, not string 'win'/'loss'
            winning_trades = len(df[df["outcome"] == 1])
            losing_trades = len(df[df["outcome"] == 0])
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            
            total_profit = df["profit"].sum()
            total_staked = df["stake"].sum()
            roi = (total_profit / total_staked * 100) if total_staked > 0 else 0
            
            # Sharpe Ratio (annualized)
            if len(daily_returns_pct) > 1:
                sharpe = self._calculate_sharpe(daily_returns_pct)
                sortino = self._calculate_sortino(daily_returns_pct)
            else:
                sharpe = 0
                sortino = 0
            
            # Max Drawdown
            max_drawdown = self._calculate_max_drawdown(df["cumulative_return"])
            
            # Calmar Ratio
            annualized_return = roi * (365 / days) if days > 0 else 0
            calmar = annualized_return / abs(max_drawdown) if max_drawdown != 0 else 0
            
            # Profit Factor
            gross_profit = df[df["profit"] > 0]["profit"].sum()
            gross_loss = abs(df[df["profit"] < 0]["profit"].sum())
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
            
            # Win/Loss streaks
            win_streak = self._calculate_streak(df["outcome"], "win")
            loss_streak = self._calculate_streak(df["outcome"], "loss")
            
            # Average win/loss
            avg_win = df[df["profit"] > 0]["profit"].mean() if winning_trades > 0 else 0
            avg_loss = df[df["profit"] < 0]["profit"].mean() if losing_trades > 0 else 0
            
            # Expectancy
            expectancy = (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))
            
            # Brier Score (if we have probability data)
            brier = self._calculate_brier_score(df)
            
            # VaR (Value at Risk) 95%
            var_95 = np.percentile(daily_returns_pct.dropna(), 5) * 100 if len(daily_returns_pct) > 5 else 0
            
            return {
                "period_days": days,
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "win_rate": round(win_rate * 100, 2),
                "total_profit": round(total_profit, 2),
                "total_staked": round(total_staked, 2),
                "roi": round(roi, 2),
                "sharpe_ratio": round(sharpe, 2),
                "sortino_ratio": round(sortino, 2),
                "max_drawdown": round(max_drawdown, 2),
                "calmar_ratio": round(calmar, 2),
                "profit_factor": round(profit_factor, 2),
                "current_win_streak": win_streak,
                "current_loss_streak": loss_streak,
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "expectancy": round(expectancy, 2),
                "brier_score": round(brier, 4),
                "var_95": round(var_95, 2),
                "annualized_return": round(annualized_return, 2),
                "generated_at": datetime.now().isoformat()
            }
            
        except Exception as e:
            return self._get_default_report()
    
    def generate_pdf_report(self, output_path: str = None) -> bytes:
        """
        Génère un rapport PDF professionnel.
        
        Returns:
            bytes: Contenu PDF
        """
        report = self.get_performance_report()
        
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        
        # Title
        pdf.set_font("Arial", "B", 20)
        pdf.cell(0, 15, "PREDATOR PAIM - Performance Report", ln=True, align="C")
        
        # Subtitle
        pdf.set_font("Arial", "I", 12)
        pdf.cell(0, 10, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True, align="C")
        pdf.cell(0, 10, f"Period: Last {report['period_days']} days", ln=True, align="C")
        pdf.ln(10)
        
        # Key Metrics
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 10, "Key Performance Indicators", ln=True)
        pdf.set_font("Arial", "", 11)
        
        metrics = [
            ("Total Trades", str(report["total_trades"])),
            ("Win Rate", f"{report['win_rate']}%"),
            ("Total Profit", f"{report['total_profit']} EUR"),
            ("ROI", f"{report['roi']}%"),
            ("Sharpe Ratio", str(report["sharpe_ratio"])),
            ("Sortino Ratio", str(report["sortino_ratio"])),
            ("Max Drawdown", f"{report['max_drawdown']}%"),
            ("Calmar Ratio", str(report["calmar_ratio"])),
            ("Profit Factor", str(report["profit_factor"])),
            ("Brier Score", str(report["brier_score"])),
            ("VaR (95%)", f"{report['var_95']}%"),
            ("Annualized Return", f"{report['annualized_return']}%"),
        ]
        
        for label, value in metrics:
            pdf.cell(90, 8, label, border=0)
            pdf.cell(90, 8, value, border=0, ln=1, align="R")
        
        pdf.ln(5)
        
        # Risk Analysis
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 10, "Risk Analysis", ln=True)
        pdf.set_font("Arial", "", 11)
        
        risk_metrics = [
            ("Average Win", f"{report['avg_win']} EUR"),
            ("Average Loss", f"{report['avg_loss']} EUR"),
            ("Win/Loss Ratio", f"{abs(report['avg_win'] / report['avg_loss']) if report['avg_loss'] else 'N/A'}"),
            ("Expectancy", f"{report['expectancy']} EUR"),
            ("Current Win Streak", str(report["current_win_streak"])),
            ("Current Loss Streak", str(report["current_loss_streak"])),
        ]
        
        for label, value in risk_metrics:
            pdf.cell(90, 8, label, border=0)
            pdf.cell(90, 8, value, border=0, ln=1, align="R")
        
        pdf.ln(5)
        
        # Disclaimer
        pdf.set_font("Arial", "I", 8)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(0, 5, "This report is generated automatically by PREDATOR PAIM v2.0. Past performance does not guarantee future results. Trading involves risk.")
        
        # Output
        if output_path:
            pdf.output(output_path)
        
        return pdf.output(dest="S").encode("latin-1")
    
    def _calculate_sharpe(self, daily_returns: pd.Series, risk_free_rate: float = 0.02) -> float:
        """Calcule le ratio de Sharpe annualisé."""
        if len(daily_returns) < 2:
            return 0
        
        excess_returns = daily_returns - (risk_free_rate / 252)
        sharpe = np.sqrt(252) * excess_returns.mean() / excess_returns.std()
        return sharpe if not np.isnan(sharpe) else 0
    
    def _calculate_sortino(self, daily_returns: pd.Series, risk_free_rate: float = 0.02) -> float:
        """Calcule le ratio de Sortino annualisé."""
        if len(daily_returns) < 2:
            return 0
        
        excess_returns = daily_returns - (risk_free_rate / 252)
        downside_returns = excess_returns[excess_returns < 0]
        
        if len(downside_returns) == 0:
            return float('inf')
        
        sortino = np.sqrt(252) * excess_returns.mean() / downside_returns.std()
        return sortino if not np.isnan(sortino) else 0
    
    def _calculate_max_drawdown(self, cumulative_returns: pd.Series) -> float:
        """Calcule le drawdown maximum en pourcentage."""
        if len(cumulative_returns) < 2:
            return 0
        
        running_max = cumulative_returns.cummax()
        drawdown = (running_max - cumulative_returns) / running_max.replace(0, np.nan)
        max_dd = drawdown.max()
        return (max_dd * 100) if not np.isnan(max_dd) else 0
    
    def _calculate_streak(self, outcomes: pd.Series, target: str) -> int:
        """
        Calcule la streak actuelle (win ou loss).
        target: 'win' → outcome == 1, 'loss' → outcome == 0
        """
        target_val = 1 if target == "win" else 0
        streak = 0
        for outcome in reversed(outcomes.tolist()):
            if outcome == target_val:
                streak += 1
            else:
                break
        return streak
    
    def _calculate_brier_score(self, df: pd.DataFrame) -> float:
        """
        Calcule le Brier Score pour évaluer la précision des probabilités.
        Brier Score = mean((predicted_prob - actual_outcome)^2)
        Plus bas est meilleur (0 = parfait).
        """
        # Get signals with probability estimates
        prob_df = df[df["ev_plus"].notna() & (df["ev_plus"] > 0)]
        
        if prob_df.empty:
            return 0.2  # Default
        
        # Convert EV+ to implied probability accuracy
        # This is a simplified calculation
        predicted_probs = 0.5 + (prob_df["ev_plus"] / 100)  # Rough conversion
        # CORRECT: outcome is SMALLINT 1/0/-1, not string 'win'
        actual_outcomes = (prob_df["outcome"] == 1).astype(int)
        
        # Clip predictions to valid range
        predicted_probs = predicted_probs.clip(0, 1)
        
        brier = ((predicted_probs - actual_outcomes) ** 2).mean()
        return brier if not np.isnan(brier) else 0.2
    
    @staticmethod
    def _get_default_report() -> dict:
        """Retourne un rapport par défaut si pas de données."""
        return {
            "period_days": 30,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0,
            "total_profit": 0,
            "total_staked": 0,
            "roi": 0,
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
            "max_drawdown": 0,
            "calmar_ratio": 0,
            "profit_factor": 0,
            "current_win_streak": 0,
            "current_loss_streak": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "expectancy": 0,
            "brier_score": 0,
            "var_95": 0,
            "annualized_return": 0,
            "generated_at": datetime.now().isoformat()
        }


# Singleton
quant_analytics = QuantAnalytics()