"""
signals/obfuscator.py — Obfuscation comportementale
Jitter temporel + Smart Staking pour briser la signature robotique
"""
from __future__ import annotations

import asyncio
import random


class Obfuscator:
    """
    Applique les techniques d'obfuscation pour éviter la détection bookmaker:
    1. Jitter temporel entre les envois de signaux
    2. Smart Staking (arrondi) sur les mises calculées
    """

    def __init__(
        self,
        jitter_min: float = 1.5,
        jitter_max: float = 4.0,
        stake_base: int = 10,
    ):
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self.stake_base = stake_base

    async def jitter_delay(self) -> float:
        """Délai aléatoire uniforme ∈ [min, max] secondes."""
        delay = random.uniform(self.jitter_min, self.jitter_max)
        await asyncio.sleep(delay)
        return delay

    def smart_stake(self, raw_stake: float) -> float:
        """Arrondit la mise au multiple de stake_base le plus proche."""
        return round(raw_stake / self.stake_base) * self.stake_base

    def humanize_stake(self, stake: float) -> float:
        """
        Ajoute un micro-offset humain sur la mise arrondie.
        Ex: 240€ → 237€ ou 243€ (variation ±5%)
        """
        noise_pct = random.uniform(-0.03, 0.03)
        noisy = stake * (1 + noise_pct)
        return round(noisy / self.stake_base) * self.stake_base
