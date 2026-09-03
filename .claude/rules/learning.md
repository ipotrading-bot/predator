---
paths:
  - "core/learning_layer.py"
  - "core/audit_engine.py"
  - "core/settlement.py"
---

# Règles — règlement, CLV, apprentissage

- ⛔ **Règle dure n°7** : jamais un taux de réussite NU. Toute proportion se
  rend avec son intervalle de Wilson ET le point mort après taxe
  (`core/stats_utils.py::p_breakeven` — gain NET, pas payout brut). 61 % de
  réussite a déjà perdu de l'argent à cote courte.
- ⛔ **Règle dure n°10** : aucun seuil numérique d'émission ne bouge sans
  mesure sur des lignes réglées POSTÉRIEURES à la correction en cours
  (`CALIBRATION_EPOCH`, `post_correction_rows()` — et le retrait ACTIF d'un
  seuil/plafond périmé déjà posé, la couche ne faisant que des upserts).
  La refonte EV du 2026-08-22 a changé l'échelle d'`edge_pct` : rien ne se
  convertit, tout se re-mesure.
- Le settlement est 100 % DÉTERMINISTE (`core/score_sources.py` : MLB
  statsapi, ESPN ouvert, TheSportsDB en dernier — api-sports retirée le
  2026-09-03 — ZÉRO IA, gardien
  `test_no_ai_layer_involved`). Il ne devine JAMAIS : deux candidats →
  refus ; score en direct → refus ; introuvable → la ligne RESTE
  expirée et sera relancée (`core/relance_expires.py`). Un WIN/LOSS faux
  est DÉFINITIF, l'attente ne l'est pas.
- Le settlement ne s'ÉTALE pas (leçon 2026-08-28) : les budgets journaliers
  du règlement n'ont ni rythme horaire ni dépendance à l'heure ; seuls les
  SCANS sont étalés (`core/daily_quota.py`, formule UNIQUE).
- Toute analyse du ledger se conditionne sur la zone jouable 2-24 h AVANT
  de conclure ; `expired` n'est pas terminal ; jamais de suppression de
  lignes réglées (règle n°9) — archiver.
- ⛔ Jamais d'appariement FLOU pour dédoublonner ou supprimer des lignes de
  résultats : clé exacte seulement (`core/db.py::_ledger_jumeau_reel`), les
  libellés divergents relèvent du pont d'alias. Mesuré le 2026-09-02 : le
  flou appariait U23/U19 aux seniors et deux matchs colombiens différents —
  *Le même match réel pesait DOUBLE* (INCIDENTS.md). Gardien :
  `tests/test_ledger_jumeaux.py`.
- Le sub-agent `ledger-analyst` porte ces gardes pour toute question de
  performance.
