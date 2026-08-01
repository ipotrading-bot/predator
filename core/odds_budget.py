"""
core/odds_budget.py — rationing du quota The Odds API.

POURQUOI (mesuré le 2026-08-01, pas déduit). La clé tourne sur un plan à
**500 requêtes/mois**. Les en-têtes `x-requests-used/remaining` lus dans les
logs Actions donnaient `used=193 remaining=307` à 12:04 UTC le 1er du mois,
et 46 crédits brûlés entre le run de 09:05 et celui de 12:04, soit
**~16 crédits/heure**. À cette cadence le quota du mois meurt en ~30 heures :
en juillet le mur est tombé le 21, et `core/odds_api.py` a passé les dix
derniers jours du mois à logger `quota guard — 47 remaining, stopping scan
early` sur chaque run. Le pipeline tournait à l'aveugle 28 jours sur 30.

Le garde existant (`remaining < 50`) ne rationne rien : il constate la mort
après coup. Ce module répartit le budget dans le temps AVANT de dépenser.

LE MODÈLE. Le cycle est le mois calendaire (vérifié : 49 restants le 31/07,
500 le 01/08). L'allocation du jour est donc :

    allocation_jour = crédits_restants / jours_restants_dans_le_mois

Elle se recalcule à chaque run sur le `remaining` réel renvoyé par l'API, ce
qui la rend auto-correctrice dans les deux sens : un jour sous-consommé
augmente l'allocation des suivants, et **si l'opérateur passe sur un plan
20 000/mois, l'allocation grimpe toute seule et plus rien n'est bridé** — il
n'y a aucun seuil en dur à revenir modifier.

LES PRIORITÉS. Tous les scans ne valent pas le même crédit :

  golden (T-120min) — la fenêtre où les edges sont les plus frais ET la seule
                      qui capture la ligne de clôture (core/closing_line.py).
                      Peut consommer toute l'allocation du jour.
  engine (72h)      — le scan de fond. Plafonné à 60% de l'allocation, donc
                      il ne peut pas manger la journée avant le prime-time.
  deep   (48h)      — le plus redondant avec `engine`. Plafonné à 30%.

Un run bridé n'échoue pas : `run_engine.py` retombe sur les Tiers 2/3
(Melbet direct, recherche web) exactement comme lors d'une panne OddsAPI.

CE QUE CE MODULE NE FAIT PAS. Il ne remplace pas le pré-vol gratuit de
`core/odds_api.py` (endpoints `/v4/sports` et `/v4/sports/{key}/events`, tous
deux hors quota d'après la doc). Ne pas payer pour une ligue qui n'a aucun
match dans la fenêtre est une économie sans contrepartie ; rationner, lui, a
un coût en couverture. On applique donc le gratuit d'abord.
"""
import json
import logging
from calendar import monthrange
from datetime import datetime, timezone

log = logging.getLogger("PREDATOR.odds_budget")

META_KEY = "oddsapi_budget"

# Part de l'allocation quotidienne qu'un tier a le droit de consommer.
# golden = 1.0 : la fenêtre T-120min prime sur tout le reste.
TIER_SHARE = {
    "golden": 1.00,
    "engine": 0.60,
    "deep":   0.30,
}
DEFAULT_TIER = "engine"

# Plancher absolu : en dessous, seul `golden` peut encore dépenser. Sert à
# garder de quoi couvrir les coups d'envoi du jour même quand un run de fond
# a mal estimé son coût. Volontairement bas (l'ancien garde à 50 immobilisait
# 10% du plan mensuel sans jamais le dépenser).
HARD_RESERVE = 12

# Allocation plancher : sous ce seuil un scan ne peut plus rien faire d'utile
# (une seule ligue coûte déjà 3 crédits), autant laisser passer un run complet
# de temps en temps plutôt que d'émietter le quota en scans partiels.
MIN_USEFUL_ALLOWANCE = 3


def days_left_in_cycle(now: datetime | None = None) -> int:
    """Jours restants dans le mois courant, aujourd'hui compris (≥ 1)."""
    now = now or datetime.now(timezone.utc)
    return max(1, monthrange(now.year, now.month)[1] - now.day + 1)


def daily_allowance(remaining: int, now: datetime | None = None) -> float:
    """Crédits que le pipeline peut dépenser aujourd'hui, tous tiers confondus."""
    if remaining is None or remaining <= 0:
        return 0.0
    return max(0.0, (remaining - HARD_RESERVE)) / days_left_in_cycle(now)


def load_state(sb, now: datetime | None = None) -> dict:
    """État persistant du budget : {date, spent_today, remaining, used}.

    Remis à zéro dès que la date UTC change — la dépense du jour est un
    compteur journalier, pas cumulatif. Renvoie un état neutre (et jamais
    d'exception) si Supabase est absent ou muet : un budget illisible ne doit
    pas empêcher un scan, seulement le laisser non rationné.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    blank = {"date": today, "spent_today": 0, "remaining": None, "used": None}
    if not sb:
        return blank
    try:
        res = sb.table("meta").select("value").eq("key", META_KEY).limit(1).execute()
        rows = res.data or []
        if not rows:
            return blank
        state = json.loads(rows[0]["value"])
    except Exception as e:
        log.debug("odds_budget load: %s", e)
        return blank
    if state.get("date") != today:
        # Nouveau jour : on garde le dernier `remaining` connu (il sert à
        # calculer l'allocation avant le premier appel payant du jour), on
        # repart à zéro sur la dépense.
        return {"date": today, "spent_today": 0,
                "remaining": state.get("remaining"), "used": state.get("used")}
    state.setdefault("spent_today", 0)
    return state


def save_state(sb, state: dict) -> None:
    if not sb:
        return
    try:
        sb.table("meta").upsert({
            "key": META_KEY,
            "value": json.dumps(state),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.debug("odds_budget save: %s", e)


class Budget:
    """Compteur de dépense d'un run, adossé à l'état persistant.

    Usage dans core/odds_api.py :

        budget = Budget.open(sb, tier)
        ...
        if not budget.can_spend(cost): break
        budget.spend(cost, remaining_from_headers)
        ...
        budget.close()

    Le `remaining` des en-têtes fait autorité dès qu'il arrive : il reflète la
    consommation réelle, y compris celle d'autres runs concurrents que ce
    process n'a aucun moyen de voir.
    """

    def __init__(self, sb, tier: str, state: dict, now: datetime | None = None):
        self.sb = sb
        self.tier = tier if tier in TIER_SHARE else DEFAULT_TIER
        self.state = state
        self.now = now or datetime.now(timezone.utc)
        self.spent_this_run = 0

    @classmethod
    def open(cls, sb, tier: str = DEFAULT_TIER, now: datetime | None = None) -> "Budget":
        return cls(sb, tier, load_state(sb, now), now)

    @property
    def remaining(self):
        return self.state.get("remaining")

    def tier_cap(self) -> float:
        """Plafond de dépense pour CE tier, à CETTE heure de la journée.

        Le plafond journalier seul ne suffit pas : `golden_hour` tourne 24
        fois par jour, les trois premiers runs mangeraient toute l'allocation
        et il ne resterait rien pour le prime-time européen ou les coups
        d'envoi américains du soir. Le plafond est donc suivi au rythme de
        l'horloge — à midi, la moitié de l'allocation est débloquée. Un run
        tardif hérite de ce que les précédents n'ont pas dépensé.
        """
        allowance = daily_allowance(self.remaining, self.now)
        elapsed = (self.now.hour * 60 + self.now.minute) / (24 * 60)
        pace = min(1.0, elapsed + 1.0 / 24)      # une heure d'avance, jamais 0 à minuit
        return allowance * TIER_SHARE[self.tier] * pace

    def can_spend(self, cost: int) -> bool:
        """Ce run a-t-il encore le droit de dépenser `cost` crédits ?

        Inconnu = autorisé : tant qu'aucun en-tête n'a jamais été lu (première
        exécution, migration, colonne meta vide) on ne bride pas — le premier
        appel payant renseignera `remaining` et le rationnement démarrera au
        suivant. Bloquer par défaut transformerait une donnée manquante en
        panne totale du pipeline.
        """
        remaining = self.remaining
        if remaining is None:
            return True
        if remaining - cost <= 0:
            return False
        if remaining - cost <= HARD_RESERVE and self.tier != "golden":
            return False
        cap = self.tier_cap()
        if cap < MIN_USEFUL_ALLOWANCE:
            # Allocation trop maigre pour un scan utile : on laisse passer le
            # tier prioritaire uniquement, sinon on gèle.
            return self.tier == "golden" and remaining > HARD_RESERVE
        return (self.state.get("spent_today", 0) + self.spent_this_run + cost) <= cap

    def spend(self, cost: int, remaining_header=None) -> None:
        self.spent_this_run += cost
        if remaining_header is not None:
            try:
                self.state["remaining"] = int(remaining_header)
            except (TypeError, ValueError):
                pass
        elif self.state.get("remaining") is not None:
            self.state["remaining"] = max(0, self.state["remaining"] - cost)

    def note_headers(self, remaining_header, used_header=None) -> None:
        """Enregistre un `remaining` lu sans avoir rien dépensé nous-mêmes
        (ex. un 4xx gratuit) — il reste l'information la plus fraîche."""
        for key, raw in (("remaining", remaining_header), ("used", used_header)):
            if raw is None:
                continue
            try:
                self.state[key] = int(raw)
            except (TypeError, ValueError):
                pass

    def close(self) -> None:
        self.state["spent_today"] = self.state.get("spent_today", 0) + self.spent_this_run
        self.state["date"] = self.now.date().isoformat()
        save_state(self.sb, self.state)
        if self.remaining is not None:
            log.info("OddsAPI budget | tier=%s | dépensé %d ce run, %d aujourd'hui "
                     "| plafond tier %.0f/j | restant %d sur %d jour(s)",
                     self.tier, self.spent_this_run, self.state["spent_today"],
                     self.tier_cap(), self.remaining, days_left_in_cycle(self.now))
