"""
core/bankroll.py — la bankroll MENSUELLE et le cadencement des mises.

Décision opérateur du 2026-09-09 (voir INCIDENTS.md, « Bankroll mensuelle »)
— 100 000 F le 1er de chaque mois, à dépenser en entier sur le mois :

  · budget du jour = (bankroll − dépensé − engagé avant aujourd'hui)
                     ÷ jours restants du mois, aujourd'hui compris ;
  · les signaux du jour se partagent ce budget AU PRORATA de leur Kelly,
    contre un « Kelly quotidien attendu » (moyenne des derniers jours) — un
    jour plus fourni que prévu n'excède pas son budget, un jour plus maigre
    reporte le reste sur les jours suivants ;
  · la mise est RÉSERVÉE à l'émission (`signals.stake_xof`, figée au premier
    enregistrement), ACTÉE au règlement (recopiée dans le ledger) ; un pari
    remboursé (PUSH) ou expiré rend sa mise au budget ;
  · résultat BRUT — 0 % d'impôt sur cette page (choix opérateur).

Tout ce qui calcule est pur et testé (tests/test_bankroll.py) ; le seul accès
à la base est `load_context`, qui ne fait que lire. Rien ici ne décide QUELS
signaux sortent : le cadencement vient APRÈS `_shadow_partition`, sur les
recommandés seulement.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.constants import (BANKROLL_DEFAULT_DAY_KELLY, BANKROLL_MONTHLY_XOF,
                            BANKROLL_PACING_LOOKBACK_D, STAKE_MIN_XOF, STAKE_ROUND_XOF)

log = logging.getLogger("PREDATOR.bankroll")

_DECISIF = ("WIN", "LOSS")          # la mise est réellement partie
_RENDU   = ("PUSH", "expired")      # la mise revient au budget


# ── Calendrier ───────────────────────────────────────────────────────────

def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def month_start_iso(now: datetime) -> str:
    return now.strftime("%Y-%m-01T00:00:00+00:00")


def next_month_start_iso(now: datetime) -> str:
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f"{y:04d}-{m:02d}-01T00:00:00+00:00"


def days_in_month(now: datetime) -> int:
    return calendar.monthrange(now.year, now.month)[1]


def days_left(now: datetime) -> int:
    """Jours restants du mois, AUJOURD'HUI COMPRIS (≥ 1)."""
    return days_in_month(now) - now.day + 1


def day_of(iso: str | None) -> str:
    """'YYYY-MM-DD' d'un horodatage ISO (UTC), '' si absent."""
    return (iso or "")[:10]


# ── Arithmétique d'une mise ──────────────────────────────────────────────

def round_stake(x: float) -> int:
    """Arrondi au billet (STAKE_ROUND_XOF) ; 0 si rien à miser. Le plancher
    STAKE_MIN_XOF s'applique au-dessus de zéro : une mise de 120 F n'existe
    pas, elle devient 500 F si le budget le permet — c'est `cap` qui borne."""
    if x <= 0:
        return 0
    # Arrondi « au billet supérieur à mi-chemin » (1 250 → 1 300), pas le
    # round() bancaire de Python (1 250 → 1 200).
    r = int((x / STAKE_ROUND_XOF + 0.5) // 1) * STAKE_ROUND_XOF
    return max(r, STAKE_MIN_XOF)


def stake_committed(row: dict) -> int:
    """Mise réellement partie : WIN/LOSS. PUSH, expiré, en cours → 0."""
    if row.get("outcome") in _DECISIF:
        return int(row.get("stake_xof") or 0)
    return 0


def gross_return(row: dict) -> float:
    """Retour brut du pari, mise comprise : WIN → mise × cote ; PUSH → mise
    rendue ; LOSS → 0. (Le résultat net d'un PUSH est nul : retour = mise.)"""
    st = int(row.get("stake_xof") or 0)
    o = row.get("outcome")
    if o == "WIN":
        return st * float(row.get("odds") or 0)
    if o == "PUSH":
        return float(st)
    return 0.0


def net_result(row: dict) -> float:
    """Gain ou perte du pari en francs (brut, 0 % d'impôt)."""
    o = row.get("outcome")
    if o == "WIN":
        return gross_return(row) - int(row.get("stake_xof") or 0)
    if o == "LOSS":
        return -float(row.get("stake_xof") or 0)
    return 0.0


# ── Reconstitution (septembre 2026, lignes d'avant la colonne) ───────────

def reconstitute(rows: list[dict], bankroll: int, n_days: int) -> dict:
    """Mise A POSTERIORI des lignes sans `stake_xof`, avec la même règle
    ramenée à un budget de jour plat (bankroll ÷ jours du mois) réparti au
    prorata du Kelly des lignes du même jour. Rend {id: mise}. Marquée
    « reconstituée » à l'affichage : c'est une lecture, pas ce qui a été
    joué."""
    budget_day = bankroll / max(n_days, 1)
    par_jour: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("stake_xof") is None:
            par_jour.setdefault(day_of(r.get("created_at")), []).append(r)
    out: dict = {}
    for jour, lot in par_jour.items():
        kelly = {id(r): max(float(r.get("kelly_pct") or 0), 0.0) for r in lot}
        total = sum(kelly.values())
        for r in lot:
            part = kelly[id(r)] / total if total > 0 else 1.0 / len(lot)
            out[id(r)] = round_stake(budget_day * part)
    return out


def with_stakes(rows: list[dict], bankroll: int, n_days: int) -> list[dict]:
    """Copie des lignes avec `stake_xof` garanti et `stake_reconstituee`
    (True quand la mise n'a pas été enregistrée à l'émission)."""
    recon = reconstitute(rows, bankroll, n_days)
    out = []
    for r in rows:
        c = dict(r)
        if c.get("stake_xof") is None:
            c["stake_xof"] = recon.get(id(r), 0)
            c["stake_reconstituee"] = True
        else:
            c["stake_xof"] = int(c["stake_xof"])
            c["stake_reconstituee"] = False
        out.append(c)
    return out


# ── Cadencement ──────────────────────────────────────────────────────────

@dataclass
class BankContext:
    now: datetime
    bankroll: int = BANKROLL_MONTHLY_XOF
    spent: int = 0                 # mises WIN/LOSS réglées ce mois
    engaged_before_today: int = 0  # mises des signaux actifs émis avant aujourd'hui
    engaged_today: int = 0         # mises des signaux actifs émis aujourd'hui
    kelly_today: float = 0.0       # Σ kelly_pct des signaux actifs émis aujourd'hui
    kelly_day_expected: float = BANKROLL_DEFAULT_DAY_KELLY
    # (match_id, market_key) → mise des signaux DÉJÀ actifs : un tick qui
    # revoit le même pari reprend sa mise figée au lieu d'en tirer une
    # seconde sur le budget du jour.
    active_stakes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def remaining_before_today(self) -> int:
        return max(0, self.bankroll - self.spent - self.engaged_before_today)

    @property
    def days_left(self) -> int:
        return days_left(self.now)

    @property
    def budget_today(self) -> float:
        return self.remaining_before_today / self.days_left

    @property
    def available_today(self) -> float:
        return max(0.0, self.budget_today - self.engaged_today)


def expected_daily_kelly(history: list[dict], now: datetime,
                         lookback_days: int = BANKROLL_PACING_LOOKBACK_D) -> float:
    """Σ kelly_pct par jour, en moyenne sur les `lookback_days` derniers jours
    (recommandés seulement, aujourd'hui exclu). Sans historique →
    BANKROLL_DEFAULT_DAY_KELLY. Un jour sans signal compte pour zéro : le
    rythme réel, pas le rythme des bons jours."""
    today = day_of(now.isoformat())
    start = day_of((now - timedelta(days=lookback_days)).isoformat())
    par_jour: dict[str, float] = {}
    for r in history:
        if r.get("is_shadow"):
            continue
        j = day_of(r.get("created_at"))
        if start <= j < today:
            par_jour[j] = par_jour.get(j, 0.0) + max(float(r.get("kelly_pct") or 0), 0.0)
    if not par_jour:
        return BANKROLL_DEFAULT_DAY_KELLY
    n_days = max(1, (datetime.fromisoformat(today) - datetime.fromisoformat(start)).days)
    return sum(par_jour.values()) / n_days


def assign_stakes(signals: list[dict], ctx: BankContext) -> list[int]:
    """Pose `stake_xof` sur chaque signal (dans l'ordre reçu) et rend la liste
    des mises. Budget du jour réparti au prorata du Kelly contre le Kelly
    quotidien attendu ; jamais au-delà de ce qui reste disponible aujourd'hui.
    Les Kelly les plus forts sont servis d'abord — quand le budget manque,
    c'est la queue qui tombe à zéro, pas la tête."""
    if not signals:
        return []
    mises = [0] * len(signals)
    nouveaux = []
    for i, s in enumerate(signals):
        cle = (s.get("match_id") or "", s.get("market_key") or "")
        if cle[0] and cle in ctx.active_stakes:
            mises[i] = int(ctx.active_stakes[cle])
            s["stake_xof"] = mises[i]
        else:
            nouveaux.append(i)
    kelly_new = sum(max(float(signals[i].get("kelly_pct") or 0), 0.0) for i in nouveaux)
    k_ref = max(ctx.kelly_day_expected, ctx.kelly_today + kelly_new, 1e-9)
    budget = ctx.budget_today
    dispo = ctx.available_today
    ordre = sorted(nouveaux,
                   key=lambda i: -max(float(signals[i].get("kelly_pct") or 0), 0.0))
    # Plancher (2026-09-09, soir) : un signal RECOMMANDÉ ne sort jamais à 0 F
    # tant que le mois a du restant. Mesuré le jour même : trois signaux aux
    # Kelly les plus forts de la journée (1,17 / 0,87 / 0,56 %) émis par des
    # ticks tardifs ont reçu 0 F parce que le budget du jour était consommé.
    # Le plancher se prend sur le restant du MOIS (jamais au-delà), et les
    # budgets des jours suivants l'absorbent — c'est l'esprit « dépensé en
    # intégralité », pas une rallonge.
    reste_mois = max(0.0, ctx.remaining_before_today - ctx.engaged_today)
    for i in ordre:
        k = max(float(signals[i].get("kelly_pct") or 0), 0.0)
        brut = budget * k / k_ref
        mise = round_stake(min(brut, dispo)) if dispo >= STAKE_MIN_XOF else 0
        mise = max(0, min(mise, int(dispo // STAKE_ROUND_XOF * STAKE_ROUND_XOF)))
        if mise == 0 and k > 0 and reste_mois >= STAKE_MIN_XOF:
            mise = STAKE_MIN_XOF
        mises[i] = mise
        signals[i]["stake_xof"] = mise
        dispo = max(0.0, dispo - mise)      # jamais négatif : le plancher est pris sur le mois
        reste_mois -= mise
    ctx.engaged_today += sum(mises[i] for i in nouveaux)
    ctx.kelly_today += kelly_new
    return mises


# ── Lecture de la base (seul accès, lecture seule) ───────────────────────

def load_context(sb, now: datetime | None = None) -> BankContext:
    """État de la bankroll du mois depuis Supabase. Tout échec de lecture est
    loggé et laisse la valeur par défaut : mieux vaut une mise cadencée sur
    un état partiel qu'un scan sans mise."""
    now = now or datetime.now(timezone.utc)
    ctx = BankContext(now=now)
    today = day_of(now.isoformat())
    debut = month_start_iso(now)
    n_days = days_in_month(now)
    try:
        led = (sb.table("ai_learning_ledger")
               .select("id,created_at,outcome,odds,kelly_pct,stake_xof,is_shadow")
               .gte("created_at", debut).execute().data) or []
        led = [r for r in led if not r.get("is_shadow")]
        ctx.spent = sum(stake_committed(r) for r in with_stakes(led, ctx.bankroll, n_days))
    except Exception as e:  # noqa: BLE001 — lecture dégradée, jamais bloquante
        ctx.notes.append(f"ledger: {e}")
        log.warning("bankroll: ledger illisible (%s) — dépensé supposé 0", e)
    try:
        act = (sb.table("signals")
               .select("id,created_at,kelly_pct,stake_xof,is_shadow,status,match_id,market_key")
               .eq("status", "active").gte("created_at", debut).execute().data) or []
        act = [r for r in with_stakes([a for a in act if not a.get("is_shadow")],
                                      ctx.bankroll, n_days)]
        for r in act:
            if r.get("match_id") and r.get("market_key"):
                ctx.active_stakes[(r["match_id"], r["market_key"])] = int(r["stake_xof"])
            if day_of(r.get("created_at")) == today:
                ctx.engaged_today += int(r["stake_xof"])
                ctx.kelly_today += max(float(r.get("kelly_pct") or 0), 0.0)
            else:
                ctx.engaged_before_today += int(r["stake_xof"])
    except Exception as e:  # noqa: BLE001
        ctx.notes.append(f"signals: {e}")
        log.warning("bankroll: signaux actifs illisibles (%s) — engagé supposé 0", e)
    try:
        since = (now - timedelta(days=BANKROLL_PACING_LOOKBACK_D + 1)).isoformat()
        hist = (sb.table("signals")
                .select("created_at,kelly_pct,is_shadow")
                .gte("created_at", since).execute().data) or []
        ctx.kelly_day_expected = expected_daily_kelly(hist, now)
    except Exception as e:  # noqa: BLE001
        ctx.notes.append(f"historique: {e}")
        log.warning("bankroll: historique illisible (%s) — Kelly quotidien par défaut", e)
    return ctx
