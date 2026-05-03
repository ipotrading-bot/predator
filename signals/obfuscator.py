"""
signals/obfuscator.py — Obfuscation des mises et délais aléatoires
"""
from __future__ import annotations

import asyncio
import random


class Obfuscator:
    """Ajoute un jitter aléatoire entre les envois pour éviter la détection."""

    def __init__(self, jitter_min: float = 1.5, jitter_max: float = 4.0, stake_base: int = 10):
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self.stake_base = stake_base

    async def jitter_delay(self) -> None:
        delay = random.uniform(self.jitter_min, self.jitter_max)
        await asyncio.sleep(delay)

    def obfuscate_stake(self, stake: float) -> float:
        """Arrondit la mise au multiple de stake_base le plus proche."""
        return round(stake / self.stake_base) * self.stake_base
