"""
core/team_aliases.py — dictionnaire persistant nom source → nom canonique.

CE N'EST PAS UN SERVICE DE TRADUCTION
-------------------------------------
odds.500.com nomme les équipes en chinois. Le portefeuille compte quelques
centaines d'équipes, et cet ensemble ne bouge qu'aux promotions/relégations.
Traduire à la volée coûterait un appel IA par match et par run ; construire un
dictionnaire coûte un appel par NOM, une seule fois dans la vie du projet.
D'où la table `team_aliases` (sql/migrate_v10_3_team_aliases.sql) : un nom
inconnu est résolu une fois, écrit, puis relu à vie.

DEUX CHEMINS DE RÉSOLUTION, LE GRATUIT D'ABORD
-----------------------------------------------
1. `sevenm` — 7M publie le MÊME calendrier avec des noms anglais et des
   identifiants numériques (core/sevenm.py). Apparier 500.com et 7M par
   (coup d'envoi, ligue, structure) donne la traduction GRATUITEMENT, sans
   aucun appel IA. C'est le chemin par défaut, et la raison pour laquelle 7M
   est intégré comme source de NOMS et non de cotes.
2. `ai` — Groq, prompt court (nom + ligue + adversaire + date), uniquement
   pour ce que 7M n'a pas couvert. Budget journalier borné : le dictionnaire
   se remplit sur plusieurs jours, ce qui est acceptable pour une donnée qui
   ne périme pas.

L'AUTO-VALIDATION EST LE VRAI GARDE-FOU
----------------------------------------
Un alias n'est pas cru parce qu'un modèle l'a produit. Il est cru quand un
appariement INDÉPENDANT — fenêtre de coup d'envoi + ligue + proximité des
cotes, qui n'utilise aucun nom — retombe sur la même paire d'équipes. Chaque
confirmation monte `confidence` ; une contradiction la met à zéro et le match
est écarté. Un faux appariement d'équipes produit un edge élevé, crédible et
entièrement imaginaire : c'est la seule erreur de ce pipeline qu'on préfère
payer en matchs perdus plutôt qu'en signaux faux.

DÉGRADATION
-----------
Sans Supabase, le dictionnaire vit en mémoire pour la durée du run et rend
None pour tout ce qu'il ne connaît pas. Une source de cotes ne doit jamais
tomber parce que son dictionnaire est muet — même contrat que
core/daily_quota.py.
"""
import logging
import os
import re
from datetime import datetime, timezone

from core import daily_quota
from core.source_adapter import detect_lang

log = logging.getLogger("PREDATOR.team_aliases")

TABLE = "team_aliases"

# Confiance de départ selon le chemin de résolution. 7M part plus haut que
# l'IA : c'est un calendrier apparié, pas une génération.
# `trusted` (2026-08-28) : appariement structurel contre le slate de confiance
# du run (api-sports/Matchbook/titan007, noms anglais) — même nature de preuve
# que 7M (temps + ligue + structure, aucun nom), donc même confiance.
CONFIDENCE_START = {"sevenm": 0.7, "trusted": 0.7, "ai": 0.4, "manual": 1.0}
CONFIDENCE_STEP  = float(os.environ.get("ALIAS_CONFIDENCE_STEP", "0.1"))

# Seuil au-dessous duquel un alias ne peut pas porter un signal. 0.6 = un
# alias 7M non encore confirmé passe ; un alias IA doit avoir été confirmé au
# moins deux fois par appariement indépendant.
MIN_CONFIDENCE = float(os.environ.get("ALIAS_MIN_CONFIDENCE", "0.6"))

QUOTA_BUCKET = "alias_ai"
AI_DAILY_BUDGET = int(os.environ.get("ALIAS_AI_DAILY_BUDGET", "40"))

# Cache mémoire du run : (source, clé) → enregistrement. Évite de relire la
# base pour un nom déjà vu dans le même scan.
_CACHE: dict = {}


def _db():
    try:
        from core.db import get_db
        return get_db(write=True)
    except Exception as e:
        log.debug("alias: pas de base (%s)", e)
        return None


def _key(source: str, alias: str, team_id: str | None, league: str | None) -> tuple:
    return (source, team_id or "", (alias or "").strip(), (league or "").strip())


# ── Lecture ──────────────────────────────────────────────────────────

def lookup(source: str, alias: str, team_id: str | None = None,
           league: str | None = None) -> dict | None:
    """Enregistrement d'alias, ou None. L'identifiant numérique de la source
    est essayé EN PREMIER : il n'a pas de langue et survit à un changement de
    graphie, là où le libellé ne survit pas toujours."""
    ck = _key(source, alias, team_id, league)
    if ck in _CACHE:
        return _CACHE[ck]

    sb = _db()
    if sb is None:
        return None
    row = None
    try:
        if team_id:
            r = sb.table(TABLE).select("*").eq("source", source).eq(
                "source_team_id", str(team_id)).limit(1).execute()
            row = (r.data or [None])[0]
        if row is None and alias:
            q = sb.table(TABLE).select("*").eq("source", source).eq("alias_source", alias)
            if league:
                q = q.eq("league", league)
            r = q.limit(1).execute()
            row = (r.data or [None])[0]
    except Exception as e:
        log.debug("alias: lecture impossible (%s)", e)
        return None

    _CACHE[ck] = row
    return row


def canonical(source: str, alias: str, team_id: str | None = None,
              league: str | None = None,
              min_confidence: float | None = None) -> str | None:
    """Nom canonique SI la confiance suffit, sinon None.

    Rendre None plutôt que le libellé brut est délibéré : un appelant qui
    reçoit None sait qu'il ne doit pas émettre de signal, là où un libellé
    chinois renvoyé tel quel finirait dans le ledger et casserait le
    settlement six heures plus tard.
    """
    thr = MIN_CONFIDENCE if min_confidence is None else min_confidence
    row = lookup(source, alias, team_id, league)
    if not row:
        return None
    if float(row.get("confidence") or 0) < thr:
        log.debug("alias %r: confiance %.2f < %.2f — écarté",
                  alias, float(row.get("confidence") or 0), thr)
        return None
    return row.get("canonical_name") or None


# ── Écriture ─────────────────────────────────────────────────────────

def remember(source: str, alias: str, canonical_name: str,
             team_id: str | None = None, league: str | None = None,
             resolved_by: str = "sevenm", lang: str | None = None) -> dict | None:
    """Écrit (ou rafraîchit) un alias. Idempotent."""
    if not alias or not canonical_name:
        return None
    rec = {
        "source": source,
        "alias_source": alias.strip(),
        "source_team_id": str(team_id) if team_id else None,
        "lang": lang or detect_lang(alias),
        "canonical_name": canonical_name.strip(),
        "league": (league or "").strip() or None,
        "confidence": CONFIDENCE_START.get(resolved_by, 0.4),
        "resolved_by": resolved_by,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sb = _db()
    if sb is None:
        _CACHE[_key(source, alias, team_id, league)] = rec
        return rec
    try:
        existing = lookup(source, alias, team_id, league)
        if existing:
            # Ne jamais écraser une confiance acquise par des appariements.
            sb.table(TABLE).update({
                "canonical_name": rec["canonical_name"],
                "source_team_id": rec["source_team_id"] or existing.get("source_team_id"),
                "updated_at": rec["updated_at"],
            }).eq("id", existing["id"]).execute()
            _CACHE.pop(_key(source, alias, team_id, league), None)
            return {**existing, **rec, "confidence": existing.get("confidence")}
        sb.table(TABLE).insert(rec).execute()
        log.info("alias appris [%s] %r → %r (%s)", source, alias,
                 rec["canonical_name"], resolved_by)
    except Exception as e:
        log.debug("alias: écriture impossible (%s)", e)
    _CACHE.pop(_key(source, alias, team_id, league), None)
    return rec


def confirm(source: str, alias: str, team_id: str | None = None,
            league: str | None = None) -> float | None:
    """Un appariement indépendant a confirmé cet alias : confiance +STEP,
    plafonnée à 1.0. Rend la nouvelle confiance."""
    row = lookup(source, alias, team_id, league)
    if not row or not row.get("id"):
        return None
    new = min(1.0, float(row.get("confidence") or 0) + CONFIDENCE_STEP)
    sb = _db()
    if sb is None:
        return new
    try:
        sb.table(TABLE).update({
            "confidence": new,
            "hits": int(row.get("hits") or 0) + 1,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()
    except Exception as e:
        log.debug("alias: confirmation impossible (%s)", e)
    _CACHE.pop(_key(source, alias, team_id, league), None)
    return new


def invalidate(source: str, alias: str, team_id: str | None = None,
               league: str | None = None, reason: str = "") -> None:
    """Un appariement a CONTREDIT cet alias : confiance à zéro, immédiatement.

    Asymétrie voulue — il faut plusieurs confirmations pour monter, une seule
    contradiction pour tomber. Le coût d'un alias faux (un signal sur le
    mauvais match) est sans commune mesure avec celui d'un alias écarté à
    tort (un match sauté)."""
    row = lookup(source, alias, team_id, league)
    log.warning("alias INVALIDÉ [%s] %r — %s", source, alias, reason or "contradiction")
    if not row or not row.get("id"):
        return
    sb = _db()
    if sb is None:
        return
    try:
        sb.table(TABLE).update({
            "confidence": 0.0,
            "contradictions": int(row.get("contradictions") or 0) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()
    except Exception as e:
        log.debug("alias: invalidation impossible (%s)", e)
    _CACHE.pop(_key(source, alias, team_id, league), None)


# ── Auto-validation à partir d'un appariement ────────────────────────

def apply_pairing(source: str, pairs: list, canonical_source: str = "sevenm") -> dict:
    """Consomme la sortie de `source_adapter.pair_fixtures` pour ALIMENTER et
    VALIDER le dictionnaire.

    `pairs` : [(fixture_source, fixture_canonique, evidence), …] où la fixture
    de droite porte des noms déjà anglais. Pour chaque paire, les deux équipes
    sont apprises dans l'ordre domicile/extérieur — c'est l'appariement
    (temps + ligue + structure) qui garantit l'ordre, jamais le nom.

    Rend un compte {appris, confirmés, contredits}.
    """
    learned = confirmed = contradicted = 0
    for src_fx, canon_fx, evidence in pairs or []:
        sides = ((src_fx.home, canon_fx.home, 0), (src_fx.away, canon_fx.away, 1))
        for raw, canon_name, idx in sides:
            if not raw or not canon_name:
                continue
            team_id = src_fx.team_ids[idx] if len(src_fx.team_ids) > idx else None
            existing = lookup(source, raw, team_id, src_fx.league)
            if existing is None:
                remember(source, raw, canon_name, team_id, src_fx.league,
                         resolved_by=canonical_source)
                learned += 1
            elif _same_team(existing.get("canonical_name"), canon_name):
                confirm(source, raw, team_id, src_fx.league)
                confirmed += 1
            else:
                invalidate(source, raw, team_id, src_fx.league,
                           reason=f"appariement donne {canon_name!r}, "
                                  f"dictionnaire dit {existing.get('canonical_name')!r}")
                contradicted += 1
    if learned or confirmed or contradicted:
        log.info("alias[%s] : %d appris, %d confirmés, %d contredits",
                 source, learned, confirmed, contradicted)
    return {"appris": learned, "confirmés": confirmed, "contredits": contradicted}


_STRIP = re.compile(r"\b(fc|afc|cf|sc|ac|club|de|the|city|united)\b|[^a-z0-9 ]")


def _same_team(a: str | None, b: str | None) -> bool:
    """Deux noms canoniques désignent-ils la même équipe ? Comparaison
    normalisée : « Manchester United FC » et « Manchester Utd » doivent
    concorder, sinon chaque source d'alias contredirait l'autre sur des
    variantes de suffixe et le dictionnaire s'auto-détruirait."""
    if not a or not b:
        return False
    na = _STRIP.sub(" ", a.lower()).split()
    nb = _STRIP.sub(" ", b.lower()).split()
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb_ = set(na), set(nb)
    return len(sa & sb_) >= max(1, min(len(sa), len(sb_)))


# ── Repli IA (payant, borné) ─────────────────────────────────────────

_JSON_NAME = re.compile(r'"(?:name|canonical|english)"\s*:\s*"([^"]{2,60})"')


def resolve_with_ai(source: str, alias: str, league: str = "",
                    opponent: str = "", match_date: str = "",
                    team_id: str | None = None) -> str | None:
    """UNE résolution IA pour UN nom inconnu, puis écriture définitive.

    N'est appelé que pour ce que 7M n'a pas couvert. Budget journalier partagé
    (`meta.quota_alias_ai_<date>`) : le dictionnaire se remplit sur plusieurs
    jours, ce qui est sans conséquence pour une donnée qui ne périme pas —
    alors qu'épuiser le TPD Groq casserait le settlement le jour même
    (incident du 2026-08-02, voir core/harvester.py).
    """
    if not alias:
        return None
    cached = canonical(source, alias, team_id, league, min_confidence=0.0)
    if cached:
        return cached

    spent = daily_quota.spent(QUOTA_BUCKET)
    if spent >= AI_DAILY_BUDGET:
        log.warning("alias: budget IA atteint (%d/%d) — %r reste inconnu ce jour",
                    spent, AI_DAILY_BUDGET, alias)
        return None

    from core.ai_search import ai_available, ai_complete
    if not ai_available():
        return None

    prompt = (
        "Identify the football/basketball club written below in a non-Latin "
        "script. Use the league, the opponent and the date as context.\n"
        f"Name: {alias}\n"
        f"League: {league or 'unknown'}\n"
        f"Opponent: {opponent or 'unknown'}\n"
        f"Date: {match_date or 'unknown'}\n"
        'Return ONLY valid JSON: {"name":"Kashima Antlers"}\n'
        'If you cannot identify it with confidence: {"name":null}'
    )
    # tier light : c'est une correspondance de connaissance, pas du
    # raisonnement — le 8b suffit et laisse le 70b au settlement.
    # Lane TRANSLATE_CJK : les modèles chinois (GLM-Flash, Qwen) résolvent un
    # nom d'équipe CJK bien mieux qu'un Llama généraliste, et pour rien. Grâce
    # au dictionnaire, le volume de cette lane est minuscule par construction.
    text = ai_complete(prompt, label="Alias", max_tokens=80,
                       temperature=0.0, timeout=30, tier="light",
                       lane="translate_cjk")
    daily_quota.add(QUOTA_BUCKET, 1)
    if not text:
        return None
    m = _JSON_NAME.search(text)
    name = m.group(1).strip() if m else None
    if not name or name.lower() in ("null", "none", "unknown"):
        log.info("alias: IA n'a pas identifié %r (%s)", alias, league)
        return None
    remember(source, alias, name, team_id, league, resolved_by="ai")
    return name


def stats() -> dict:
    """Taille et santé du dictionnaire — pour scripts/ops.py et le rapport."""
    sb = _db()
    if sb is None:
        return {"total": 0, "base": False}
    try:
        rows = sb.table(TABLE).select("source,confidence,resolved_by").execute().data or []
    except Exception as e:
        log.debug("alias: stats impossibles (%s)", e)
        return {"total": 0, "base": False}
    usable = [r for r in rows if float(r.get("confidence") or 0) >= MIN_CONFIDENCE]
    by_src: dict = {}
    for r in rows:
        by_src[r.get("source")] = by_src.get(r.get("source"), 0) + 1
    return {"total": len(rows), "utilisables": len(usable), "base": True,
            "par_source": by_src,
            "par_chemin": {k: sum(1 for r in rows if r.get("resolved_by") == k)
                           for k in ("sevenm", "ai", "manual")}}
