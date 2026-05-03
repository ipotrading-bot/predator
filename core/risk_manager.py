"""
core/risk_manager.py — Gestionnaire de risque : Kill-Switch, Drawdown, LVM
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)


class SystemState(Enum):
    ACTIVE = auto()
    PAUSED = auto()
    KILLED = auto()    # Kill-Switch déclenché


@dataclass
class BankrollSnapshot:
    timestamp: float
    balance: float
    total_staked: float
    total_profit: float


class RiskManager:
    """
    Surveille le capital en temps réel.
    Déclenche le Kill-Switch automatique si drawdown > seuil.
    """

    def __init__(
        self,
        starting_bankroll: float,
        max_drawdown_pct: float = 0.15,
        max_single_stake_pct: float = 0.05,   # 5% max par pari
        max_daily_loss_pct: float = 0.08,      # 8% perte max / jour
    ):
        self.starting_bankroll = starting_bankroll
        self.peak_bankroll = starting_bankroll
        self.current_bankroll = starting_bankroll
        self.max_drawdown_pct = max_drawdown_pct
        self.max_single_stake_pct = max_single_stake_pct
        self.max_daily_loss_pct = max_daily_loss_pct

        self.state = SystemState.ACTIVE
        self.history: list[BankrollSnapshot] = []
        self._daily_starting: float = starting_bankroll

    # ── Métriques ─────────────────────────────────────────────────

    @property
    def current_drawdown(self) -> float:
        """Drawdown actuel depuis le pic (0.0 à 1.0)."""
        if self.peak_bankroll <= 0:
            return 0.0
        return (self.peak_bankroll - self.current_bankroll) / self.peak_bankroll

    @property
    def daily_loss(self) -> float:
        return max(0.0, (self._daily_starting - self.current_bankroll) / self._daily_starting)

    @property
    def roi(self) -> float:
        return (self.current_bankroll - self.starting_bankroll) / self.starting_bankroll

    # ── Mises ─────────────────────────────────────────────────────

    def validate_stake(self, stake: float) -> tuple[bool, str]:
        """Valide une mise avant d'envoyer le signal."""
        if self.state == SystemState.KILLED:
            return False, "Kill-Switch actif — système arrêté."

        if self.state == SystemState.PAUSED:
            return False, "Système en pause."

        max_stake = self.current_bankroll * self.max_single_stake_pct
        if stake > max_stake:
            return False, f"Mise {stake:.0f}€ > plafond {max_stake:.0f}€ (5% bankroll)."

        return True, "OK"

    # ── Mise à jour après résultat ─────────────────────────────────

    def update(
        self,
        profit: float,
        timestamp: float,
        total_staked: float,
        total_profit: float,
    ) -> SystemState:
        """
        Mise à jour du capital après règlement d'un pari.
        Déclenche automatiquement le Kill-Switch si nécessaire.
        """
        self.current_bankroll += profit

        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll

        self.history.append(BankrollSnapshot(
            timestamp=timestamp,
            balance=self.current_bankroll,
            total_staked=total_staked,
            total_profit=total_profit,
        ))

        # ── Kill-Switch ───────────────────────────────────────
        if self.current_drawdown >= self.max_drawdown_pct:
            self.state = SystemState.KILLED
            logger.critical(
                f"🛑 KILL-SWITCH DÉCLENCHÉ | Drawdown: {self.current_drawdown:.1%} "
                f"| Balance: {self.current_bankroll:.0f}€"
            )
            return self.state

        # ── Pause journalière ─────────────────────────────────
        if self.daily_loss >= self.max_daily_loss_pct:
            self.state = SystemState.PAUSED
            logger.warning(
                f"⚠️ PAUSE JOURNALIÈRE | Perte: {self.daily_loss:.1%} "
                f"| Balance: {self.current_bankroll:.0f}€"
            )
            return self.state

        return self.state

    def reset_daily(self) -> None:
        """Réinitialise le compteur journalier (appel à minuit)."""
        self._daily_starting = self.current_bankroll
        if self.state == SystemState.PAUSED:
            self.state = SystemState.ACTIVE
            logger.info("✅ Système réactivé après reset journalier.")

    def get_summary(self) -> dict:
        return {
            "state": self.state.name,
            "current_bankroll": round(self.current_bankroll, 2),
            "peak_bankroll": round(self.peak_bankroll, 2),
            "drawdown": round(self.current_drawdown, 4),
            "daily_loss": round(self.daily_loss, 4),
            "roi": round(self.roi, 4),
        }
