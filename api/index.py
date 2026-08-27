"""
api/index.py — dashboard PREDATOR (Flask, déployé sur Vercel, LECTURE SEULE).

Pages   : /            signaux actifs encore jouables
          /ledger      bilan CLV
          /audit       distribution d'alpha par sport
          /performance WIN/LOSS/PUSH (ai_learning_ledger)
          /system      calculateur de paris système
API     : /api/signals /api/health
Écriture: /api/scan       met une demande de scan dans meta (cooldown 120 s)
          /api/audit/run  déclenche audit.yml — JETON D'ADMIN REQUIS

Le numéro de version vit dans DASHBOARD_VERSION (une seule définition,
injectée dans les templates et rendue par /api/health). Cet en-tête portait
« v8.8 » et une liste de routes amputée de trois pages — il n'était mis à
jour par personne, ce qui est le sort de tout numéro recopié à la main.
Les modules de core/ portent leurs propres versions, indépendantes.
"""
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone as _tz

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

# (Allégé le 2026-08-22.) Les imports de la couche d'apprentissage
# (SPORT_DEFAULTS, load_thresholds, load_segment_thresholds,
# load_learning_summary), du disjoncteur par sport et de bucket_predictions
# ont été retirés avec les sections qu'ils alimentaient sur /performance :
# seuils appris, dernier cycle d'apprentissage, calibration de Brier. Ces
# mesures existent toujours — elles vivent dans la base et dans
# scripts/weekly_report.py — elles ne sont simplement plus rendues ici.
from core.perf_view import (filter_rows as _perf_filter_rows,
                            resolution_rate as _resolution_rate)
from core.constants import TAX_RATE as _TAX_RATE
from core.db import get_db as _get_db_client
from core.stats_utils import p_breakeven, wilson_ci

log = logging.getLogger("PREDATOR.api")

load_dotenv()

_template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
_static_dir   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, template_folder=_template_dir, static_folder=_static_dir, static_url_path="/static")

# Version du dashboard — UNE seule définition, injectée dans les templates
# ({{ version }}) et rendue par /api/health. Elle était écrite en dur à sept
# endroits : « 8.8 » dans le health-check, et v8.5/v8.6/v8.8/v9.4/v10.0/v1.0
# dans les six pieds de page. Un numéro de version qui ment fait chercher un
# bug dans le mauvais déploiement.
# 10.5 (2026-08-26) : barre de navigation mobile refondue (icones 26px,
# pastille active), /performance allegee, Wiz supprime. Le numero sert
# aussi de cache-busting au CSS (`?v=`) : sans bump, un telephone qui a
# deja predator.css en cache ne verrait AUCUN changement de style.
DASHBOARD_VERSION = "10.5"


@app.context_processor
def _inject_version():
    """`{{ version }}` dans TOUS les templates.

    Les six pieds de page portaient six versions différentes — v8.5, v8.6,
    v8.8, v9.4, v10.0, v1.0 — parce que chacune était en dur et n'était mise
    à jour que quand on touchait à cette page-là. Un numéro de version qui
    varie selon l'onglet ne sert plus à identifier un déploiement.
    """
    return {"version": DASHBOARD_VERSION}


# Duree de cache des assets statiques. Courte VOLONTAIREMENT : ce depot n'a
# pas d'etape de build, donc pas d'empreinte dans les noms de fichiers — un
# cache long garderait un CSS perime sans moyen de l'invalider. Dix minutes
# suffisent a couvrir une session de consultation (le CSS + 6 icones PWA
# etaient re-telecharges a CHAQUE page, sur mobile en 4G) tout en faisant
# apparaitre un deploiement en moins d'un quart d'heure.
_STATIC_MAX_AGE = 600


@app.after_request
def no_cache(response):
    """Pas de cache sur les PAGES, cache court sur les ASSETS.

    Le no-store s'appliquait indistinctement : les donnees doivent en effet
    etre fraiches a chaque chargement (un signal perime affiche comme actif
    est un faux pari), mais l'appliquer aussi a /static faisait
    re-telecharger predator.css et les icones a chaque navigation — couteux
    sur mobile et sans le moindre benefice de fraicheur.
    """
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = f"public, max-age={_STATIC_MAX_AGE}"
        return response
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response


def _db(write: bool = False):
    """Client Supabase du dashboard — clé de LECTURE, toujours.

    ⚠️ `write=True` N'EST PLUS UTILISÉ NULLE PART depuis C3 (2026-08-27), et
    le paramètre n'est conservé que pour ne pas casser une signature. Le
    dashboard n'écrit plus rien : `/api/scan`, sa seule écriture, passe par
    `demander_scan()`, fonction Postgres `security definer` appelable avec la
    clé anon (sql/migrate_v10_9_scan_request_rpc.sql).

    C'est ce qui permet de RETIRER `SUPABASE_SERVICE_KEY` du déploiement
    Vercel : tant qu'elle y était, une faille de la fonction publique donnait
    les pleins pouvoirs sur `signals`, `ai_learning_ledger`, `meta` et
    `app_secrets`. Rend None si rien n'est configuré, pour que les routes se
    dégradent proprement au lieu de lever."""
    return _get_db_client(write=write)


def _get_meta(sb, key: str) -> dict | None:
    try:
        res = (sb.table("meta").select("value,updated_at")
               .eq("key", key)
               .order("updated_at", desc=True)
               .limit(1).execute())
        if res.data:
            return json.loads(res.data[0]["value"])
    except Exception:
        pass
    return None


# ── Dashboard ────────────────────────────────────────────────────────

_DASH_SPORT_ORDER = {
    "basketball": 0, "hockey": 1, "americanfootball": 2, "baseball": 3,
    "esports": 4, "rugby": 5, "tennis": 6, "mma": 7, "euroleague_basketball": 0,
    "volleyball": 8, "tabletennis": 9, "handball": 10,
    "boxing": 11, "darts": 12, "cricket": 13, "soccer": 14,
}

_HIGH_QUALITY = {"HIGH_VALUE", "VALUE", "LOW_VALUE", "SUSPECT_DATA"}


def _parse_match_time(s: str):
    """Parse match_time to UTC-aware datetime regardless of format (T vs space, Z vs +00:00)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=_tz.utc)
        except Exception:
            pass
    return None


def _is_playable(s: dict, now) -> bool:
    """Ce signal est-il encore pariable ? (coup d'envoi pas encore passé)

    RÈGLE UNIQUE, volontairement partagée. Elle était réimplémentée à
    l'identique en trois endroits — dashboard(), la page Wiz (supprimée le
    2026-08-26) et le JS de /system — pendant que /api/signals, lui, ne
    l'appliquait PAS : au
    2026-08-22 il renvoyait 37 signaux actifs dont 23 dont le match avait
    commencé, certains depuis huit heures. Les deux consommateurs connus
    refiltraient côté client ; le troisième aurait hérité du bug.

    Un signal SANS match_time reste jouable : on ne peut pas prouver qu'il a
    commencé, et l'écarter reviendrait à masquer un pari valide.

    Ces lignes ne sont pas des déchets : la purge les garde volontairement
    48 h pour que core/audit_engine.py ait le temps de les régler
    (run_engine._purge_old_signals). Elles n'ont simplement rien à faire
    dans une liste de paris à poser.
    """
    mt = _parse_match_time(s.get("match_time") or "")
    return not s.get("match_time") or mt is None or mt > now


def _mk_dash_sort(s: dict, now) -> tuple:
    """Urgence d'abord (< 4h → tier 0 par heure ASC), sinon par sport+edge."""
    mt = _parse_match_time(s.get("match_time") or "")
    if mt:
        secs = (mt - now).total_seconds()
        if 0 < secs <= 14400:
            return (0, s.get("match_time") or "9999", 0, -(s.get("edge_pct") or 0))
    return (1, "0", _DASH_SPORT_ORDER.get(s.get("sport", ""), 9), -(s.get("edge_pct") or 0))


# Même barème que le data-quality du template : 0 = meilleur.
_QUALITY_RANK = {"HIGH_VALUE": 0, "VALUE": 1}


def _group_key(s: dict) -> tuple:
    """Clé de regroupement visuel d'un signal vers sa carte-match.

    On ne se fie pas au seul match_id : le même match réel peut arriver par
    The Odds API (uuid) et par la recherche web (id dérivé des noms d'équipes),
    donc avec deux ids différents. Le nom normalisé + la date de match est ce
    que l'opérateur perçoit comme "le même match".
    """
    return (s.get("sport") or "",
            (s.get("match") or "").lower().strip(),
            (s.get("match_time") or "")[:10])


def _group_by_match(signals: list) -> list:
    """Regrouper les signaux déjà triés en cartes-match.

    Un match génère jusqu'à 3 signaux (h2h + totals + spreads) ; affichés à
    plat, ils se lisent comme des doublons puisque la liste n'affiche que le
    nom du match. Chaque groupe garde l'index plat de ses signaux : le JS
    indexe dans SIGNALS via openModal(idx), cet index doit rester valide.
    """
    groups: dict = {}
    for idx, s in enumerate(signals):
        g = groups.get(_group_key(s))
        if g is None:
            g = {
                "match":        s.get("match") or "",
                "league":       s.get("league") or "",
                "sport":        s.get("sport") or "soccer",
                "match_time":   s.get("match_time") or "",
                "legs":         [],
                "best_edge":    0.0,
                "best_quality": 3,
                "best_flag":    s.get("risk_flag") or "LOW_VALUE",
            }
            groups[_group_key(s)] = g
        g["legs"].append({"idx": idx, "sig": s})
        g["best_edge"] = max(g["best_edge"], s.get("edge_pct") or 0.0)
        rank = _QUALITY_RANK.get(s.get("risk_flag"), 2)
        if rank < g["best_quality"]:
            # La couleur de bordure et le badge de la carte suivent sa
            # meilleure jambe — les signaux sont déjà triés par edge DESC dans
            # un rang donné, donc à rang égal la première jambe vue gagne.
            g["best_quality"] = rank
            g["best_flag"] = s.get("risk_flag") or "LOW_VALUE"
    return list(groups.values())


@app.route("/")
def dashboard():
    signals   = []
    last_scan = None
    try:
        sb = _db()
        if sb:
            res = sb.table("signals").select("*").eq("status", "active").order("created_at", desc=True).limit(200).execute()
            raw = res.data or []

            # Deduplicate: keep NEWEST signal per (match_id, market_key).
            # Signals are already ordered created_at DESC so first-seen = freshest.
            seen: dict = {}
            for s in raw:
                mid  = s.get("match_id") or s.get("match", "")
                mkey = s.get("market_key") or s.get("market", "")
                key  = (mid, mkey)
                if key not in seen:
                    seen[key] = s

            _now = datetime.now(_tz.utc)
            # Aucun match déjà commencé sur le dashboard (demande opérateur
            # 2026-07-22) : l'ancienne fenêtre de grâce de 2h après le coup
            # d'envoi gardait des signaux non jouables qui noyaient les
            # signaux encore pariables. Un signal sans match_time reste
            # affiché — on ne peut pas prouver qu'il a commencé.
            filtered = [s for s in seen.values()
                        if s.get("risk_flag") in _HIGH_QUALITY and _is_playable(s, _now)]
            signals = sorted(filtered, key=lambda s: _mk_dash_sort(s, _now))

            # (Supprimé 2026-07-22) Le fallback « moins de 3 signaux actifs →
            # compléter avec les matchs récemment settlés » remplissait le
            # dashboard de matchs déjà joués. Le filtre ci-dessus n'accepte
            # plus rien après le coup d'envoi : ces lignes étaient de toute
            # façon masquées côté client. L'historique reste sur /bilan.

            # Parse sharp_sources JSON string → dict, consensus_score → int
            for s in signals:
                ss = s.get("sharp_sources")
                if isinstance(ss, str):
                    try:
                        s["sharp_sources"] = json.loads(ss)
                    except Exception:
                        s["sharp_sources"] = {}
                elif ss is None:
                    s["sharp_sources"] = {}
                cs = s.get("consensus_score")
                try:
                    s["consensus_score"] = int(cs) if cs is not None else None
                except (TypeError, ValueError):
                    # A single malformed value here (e.g. a non-integer string)
                    # used to propagate to the outer try/except and blank the
                    # ENTIRE dashboard's signal list, not just this row.
                    s["consensus_score"] = None
            last_scan = _get_meta(sb, "last_scan")
    except Exception as e:
        log.error("Dashboard: %s", e)

    groups = _group_by_match(signals)

    from core.constants import BANKROLL_REF
    return render_template("index.html", signals=signals, groups=groups,
                           last_scan=last_scan, bankroll_ref=BANKROLL_REF,
                           sport_emoji=_SPORT_EMOJI,
                           sport_label_short=_SPORT_LABEL_SHORT,
                           sport_order=_SPORT_ORDER)


# ── Ledger ───────────────────────────────────────────────────────────

# Clés = valeurs de core.odds_api.SPORT_KEYS (le `sport` écrit dans signals),
# PAS les sport_keys OddsAPI. Un sport absent d'ici s'affiche en 🎯 générique
# sur TOUTES les pages — c'est ce qui est arrivé à `aussierules` et
# `rugbyleague` entre leur mise en production et le 2026-08-22 : deux sports
# actifs, un emoji de repli, personne ne le voit passer.
# tests/test_dashboard_sports.py vérifie que tout sport actif est couvert.
# Les sports RETIRÉS (esports, tabletennis, volleyball, handball) restent
# listés : les lignes historiques du ledger doivent continuer à s'afficher.
_SPORT_EMOJI = {
    "soccer": "⚽", "basketball": "🏀", "tennis": "🎾", "hockey": "🏒",
    "mma": "🥋", "boxing": "🥊", "darts": "🎯", "cricket": "🏏",
    "esports": "🎮", "americanfootball": "🏈", "baseball": "⚾",
    "euroleague_basketball": "🏀", "college_football": "🏈",
    "rugbyleague": "🏉", "aussierules": "🏉",
    "rugby": "🏉", "volleyball": "🏐", "tabletennis": "🏓", "handball": "🤾",
}
_SPORT_LABEL = {
    "soccer": "Football", "basketball": "Basket", "tennis": "Tennis",
    "hockey": "Hockey", "mma": "MMA", "boxing": "Boxe", "darts": "Fléchettes",
    "cricket": "Cricket", "esports": "eSports", "americanfootball": "NFL",
    "euroleague_basketball": "Euroleague", "college_football": "NCAA Football",
    "rugbyleague": "Rugby XIII", "aussierules": "Foot australien",
    "baseball": "MLB", "rugby": "Rugby", "volleyball": "Volley",
    "tabletennis": "Ping-Pong", "handball": "Handball",
}

# Libellés COURTS pour les chips de filtre du dashboard (place contrainte).
# Ils vivaient en dur dans templates/index.html, en double de la table
# ci-dessus — et la copie JS avait divergé : au 2026-08-22 il lui manquait
# euroleague_basketball, rugbyleague et aussierules, trois sports ACTIFS
# affichés « 🎯 rugbyleague » sur mobile. Une table dupliquée dans un
# template ne se met jamais à jour deux fois : elle est désormais injectée
# depuis ici (index.html reçoit SPORT_EMOJI/SPORT_LABEL_SHORT/SPORT_ORDER).
_SPORT_LABEL_SHORT = {
    "soccer": "FOOT", "basketball": "BASKET", "tennis": "TENNIS",
    "hockey": "HOCKEY", "mma": "MMA", "boxing": "BOXE", "darts": "FLÉCHETTES",
    "cricket": "CRICKET", "esports": "eSPORT", "americanfootball": "NFL",
    "euroleague_basketball": "EUROLEAGUE", "college_football": "NCAAF",
    "rugbyleague": "RUGBY XIII", "aussierules": "AFL",
    "baseball": "MLB", "rugby": "RUGBY", "volleyball": "VOLLEY",
    "tabletennis": "PING", "handball": "HAND",
}

# Ordre d'affichage des groupes de sport. Un sport absent passe en fin de
# liste (valeur de repli côté JS), il n'est jamais masqué.
_SPORT_ORDER = {
    "basketball": 0, "euroleague_basketball": 1, "hockey": 2,
    "americanfootball": 3, "college_football": 4, "baseball": 5,
    "rugbyleague": 6, "aussierules": 7, "esports": 8, "rugby": 9,
    "tennis": 10, "mma": 11, "volleyball": 12, "tabletennis": 13,
    "handball": 14, "boxing": 15, "darts": 16, "cricket": 17, "soccer": 18,
}


def _clv_verdict(avg_clv: float, count: int) -> str:
    """Return BOOST / STABLE / ATTENTION / SUSPENDU based on CLV performance."""
    if count < 3:
        return "INSUFFISANT"
    if avg_clv >= 5.0:
        return "BOOST"
    if avg_clv >= 0.0:
        return "STABLE"
    if avg_clv >= -15.0:
        return "ATTENTION"
    return "SUSPENDU"


@app.route("/ledger")
def ledger():
    signals    = []
    stats: dict = {}
    try:
        sb = _db()
        if sb:
            res = (sb.table("signals")
                   .select("*")
                   .in_("status", ["settled", "closed", "expired"])
                   .order("clv_pct", desc=True)
                   .limit(300)
                   .execute())
            signals = [s for s in (res.data or []) if s.get("clv_pct") is not None]

            if signals:
                clv_vals  = [s["clv_pct"] for s in signals]
                hit_count = sum(1 for c in clv_vals if c >= 0)
                stats = {
                    "total":     len(signals),
                    "hit_rate":  round(hit_count / len(clv_vals) * 100, 1),
                    "avg_clv":   round(sum(clv_vals) / len(clv_vals), 2),
                    "best_clv":  round(max(clv_vals), 2),
                    "worst_clv": round(min(clv_vals), 2),
                }

                # Real closing-line CLV — reported SEPARATELY, never merged
                # into the block above. `clv_pct` is the audit's proxy
                # (a price fetched hours after kickoff, or a re-derivation of
                # the entry edge — see core/settlement.py); `clv_pct_real` is
                # the bet's price against the actual sharp close, captured
                # before kickoff by core/closing_line.py. Averaging the two
                # together would let the proxy dilute the only number that
                # can confirm or kill a market. `real_n` is what makes the
                # figure readable: until capture has had time to accumulate,
                # a +8% on n=3 must not look like a verdict.
                real_vals = [s["clv_pct_real"] for s in signals
                             if s.get("clv_pct_real") is not None]
                stats["real_n"] = len(real_vals)
                if real_vals:
                    stats["avg_clv_real"]  = round(sum(real_vals) / len(real_vals), 2)
                    stats["hit_rate_real"] = round(
                        sum(1 for c in real_vals if c >= 0) / len(real_vals) * 100, 1)

                # Load current dynamic thresholds
                t_res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
                thresholds = {}
                for row in (t_res.data or []):
                    sport = row["key"].replace("threshold_", "")
                    thresholds[sport] = float(row["value"])

                _DEFAULT_T = {
                    "soccer": 2.5, "basketball": 2.0, "tennis": 1.8, "hockey": 2.0,
                    "mma": 2.5, "boxing": 2.5, "darts": 2.0, "cricket": 2.0,
                    "esports": 2.2, "americanfootball": 2.0, "baseball": 2.0,
                    "euroleague_basketball": 2.0,
                    "rugby": 2.0, "volleyball": 2.0, "tabletennis": 2.0, "handball": 2.0,
                }

                sports_stats = {}

                # All sports present in signals. Display only — this route
                # used to also auto-adjust and upsert thresholds into `meta`,
                # but it runs on the anon key, which RLS has blocked from
                # writing `meta` since migrate_v9_3: the upsert failed on
                # every page view (silent log.warning), and had it ever
                # succeeded it would have fought core/learning_layer.py's
                # adjustment algorithm. Threshold updates belong to the
                # audit workflow (learning_layer), not to a dashboard read.
                all_sports = sorted(set(s.get("sport", "") for s in signals if s.get("sport")))
                for sport in all_sports:
                    sv = [s["clv_pct"] for s in signals if s.get("sport") == sport]
                    if not sv:
                        continue
                    avg = round(sum(sv) / len(sv), 2)
                    hit = round(sum(1 for c in sv if c >= 0) / len(sv) * 100, 1)
                    verdict = _clv_verdict(avg, len(sv))
                    cur_t   = thresholds.get(sport, _DEFAULT_T.get(sport, 2.0))
                    def_t   = _DEFAULT_T.get(sport, 2.0)

                    # Per-sport real CLV, same separation as the global block.
                    rv = [s["clv_pct_real"] for s in signals
                          if s.get("sport") == sport and s.get("clv_pct_real") is not None]

                    sports_stats[sport] = {
                        "count":     len(sv),
                        "hit_rate":  hit,
                        "avg_clv":   avg,
                        "real_n":    len(rv),
                        "avg_clv_real": round(sum(rv) / len(rv), 2) if rv else None,
                        "threshold": cur_t,
                        "default_t": def_t,
                        "verdict":   verdict,
                        "emoji":     _SPORT_EMOJI.get(sport, "🎯"),
                        "label":     _SPORT_LABEL.get(sport, sport.capitalize()),
                    }

                stats["sports"] = sports_stats

    except Exception as e:
        log.error("Ledger: %s", e)

    return render_template("ledger.html", signals=signals, stats=stats)


# ── JSON API ─────────────────────────────────────────────────────────

@app.route("/audit")
def audit():
    audit_data: dict     = {}
    thresholds: dict     = {}
    recent_signals: list = []
    global_stats: dict   = {}
    try:
        sb = _db()
        if sb:
            # ── Active signals — always available ─────────────────────
            try:
                act_res = (sb.table("signals")
                           .select("sport,edge_pct,sharp_prob,kelly_pct,risk_flag,scanned_at,match,market,selection_name,xbet_odd,pinnacle_price")
                           .eq("status", "active")
                           .order("scanned_at", desc=True)
                           .limit(300)
                           .execute())
                active_rows = act_res.data or []
            except Exception as e:
                log.warning("Audit active query: %s", e)
                active_rows = []

            # ── CLV data — only available once settlement is implemented ──
            clv_rows: list = []
            try:
                clv_res = (sb.table("signals")
                           .select("sport,clv_pct,edge_pct,scanned_at,match,market,status")
                           .in_("status", ["settled", "closed"])
                           .not_.is_("clv_pct", "null")
                           .order("scanned_at", desc=True)
                           .limit(200)
                           .execute())
                clv_rows = clv_res.data or []
            except Exception:
                pass

            # ── Per-sport stats from active signals ───────────────────
            for sport in sorted(set(r.get("sport", "") for r in active_rows) - {""}):
                sport_rows = [r for r in active_rows if r.get("sport") == sport]
                edges = [r["edge_pct"] for r in sport_rows if r.get("edge_pct")]
                if not edges:
                    continue
                tiers = {"HIGH_VALUE": 0, "VALUE": 0, "LOW_VALUE": 0}
                for r in sport_rows:
                    t = r.get("risk_flag") or "LOW_VALUE"
                    if t in tiers:
                        tiers[t] += 1
                audit_data[sport] = {
                    "count":       len(edges),
                    "avg_edge":    round(sum(edges) / len(edges), 2),
                    "best_edge":   round(max(edges), 2),
                    "tiers":       tiers,
                    "recent_edges": edges[:10],
                }

            # ── Merge CLV data if available ───────────────────────────
            for sport in set(r.get("sport", "") for r in clv_rows):
                if not sport:
                    continue
                sv = [r["clv_pct"] for r in clv_rows if r.get("sport") == sport]
                if not sv:
                    continue
                hits = sum(1 for c in sv if c >= 0)
                entry = audit_data.setdefault(sport, {"count": 0, "avg_edge": 0, "best_edge": 0, "tiers": {}, "recent_edges": []})
                entry["clv_count"] = len(sv)
                entry["hit_rate"]  = round(hits / len(sv) * 100, 1)
                entry["avg_clv"]   = round(sum(sv) / len(sv), 2)

            # ── Thresholds (learning layer) ───────────────────────────
            try:
                t_res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
                for row in (t_res.data or []):
                    sport_key = row["key"].replace("threshold_", "")
                    thresholds[sport_key] = float(row["value"])
            except Exception:
                pass

            # ── Recent signals table ──────────────────────────────────
            recent_signals = active_rows[:20]

            # ── Global KPIs ───────────────────────────────────────────
            if active_rows:
                all_edges = [r["edge_pct"] for r in active_rows if r.get("edge_pct")]
                global_stats["total"]        = len(active_rows)
                global_stats["avg_edge"]     = round(sum(all_edges) / len(all_edges), 2) if all_edges else 0
                global_stats["high_value"]   = sum(1 for r in active_rows if r.get("risk_flag") == "HIGH_VALUE")
                global_stats["value"]        = sum(1 for r in active_rows if r.get("risk_flag") == "VALUE")
                global_stats["low_value"]    = sum(1 for r in active_rows if r.get("risk_flag") == "LOW_VALUE")
                global_stats["sports_count"] = len(audit_data)
                global_stats["has_clv"]      = bool(clv_rows)

    except Exception as e:
        log.error("Audit: %s", e)

    return render_template("audit.html",
                           audit_data=audit_data,
                           thresholds=thresholds,
                           recent_signals=recent_signals,
                           global_stats=global_stats)


# ── JSON API ─────────────────────────────────────────────────────────


@app.route("/performance")
def performance():
    rows: list      = []
    history: list   = []   # sous-ensemble de `rows` affiché dans le tableau HISTORIQUE
    global_s: dict  = {}
    try:
        sb = _db()
        if sb:
            res = (sb.table("ai_learning_ledger")
                   .select("*")
                   .order("created_at", desc=True)
                   .limit(500)
                   .execute())
            # Mission 2 (2026-08-22) : sports retirés et mois archivés
            # n'apparaissent plus — filtre d'AFFICHAGE (core/perf_view.py),
            # rien n'est supprimé du ledger.
            rows = _perf_filter_rows(res.data or [])

            if rows:
                # (Retiré le 2026-08-22 — simplification demandée par
                # l'opérateur : « trop de littérature et d'informations ».)
                # Les seuils d'edge appris, le résumé du dernier cycle
                # d'apprentissage et l'état des disjoncteurs par sport ne
                # sont plus affichés ici : ce sont des rouages internes, pas
                # un résultat. Ils restent lisibles côté base
                # (meta.threshold_<sport>) et par `scripts/weekly_report.py`.
                # Quatre appels Supabase de moins par chargement de page.

                settled = [r for r in rows if r.get("outcome") in ("WIN", "LOSS", "PUSH")]
                decisive = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]
                # Le tableau HISTORIQUE ne montre que les matchs dont le
                # résultat est connu (demande opérateur) — les stats plus haut
                # continuent de se baser sur `rows` complet, y compris les
                # signaux encore non audités.
                # ANNULÉS (PUSH) MASQUÉS — demande opérateur du 2026-08-26.
                # Un push est un enjeu remboursé : il n'a ni gagné ni perdu,
                # et il encombrait l'historique sans rien apprendre. Filtre
                # d'AFFICHAGE uniquement — la ligne reste dans le ledger, et
                # `settled` (donc le compte des pushes) sert encore aux stats
                # plus haut. Les taux se calculent déjà sur `decisive`
                # (WIN|LOSS), ils sont donc inchangés par ce masquage.
                history = [r for r in settled if r.get("outcome") != "PUSH"]
                wins    = sum(1 for r in settled if r.get("outcome") == "WIN")
                losses  = sum(1 for r in settled if r.get("outcome") == "LOSS")
                pushes  = sum(1 for r in settled if r.get("outcome") == "PUSH")
                # CLV — real closing line first, entry edge only as a fallback.
                # `clv_final` is NOT closing-line value: for most rows it is a
                # re-derivation of the entry edge (see core/settlement.py), so
                # a "CLV" built on it can only ever restate what we already
                # knew when we bet. `clv_pct_real` is the genuine measurement
                # (xbet_odd vs the sharp close) written by core/closing_line.py
                # off the scan feed and, for non-OddsAPI sports, by
                # core/audit_engine.py's oracle. The two are never averaged
                # together — clv_is_real tells the template which one it is
                # looking at, so the page can't quietly label the entry edge
                # as CLV.
                clv_real = [r["clv_pct_real"] for r in rows if r.get("clv_pct_real") is not None]
                clv_all  = clv_real or [r["clv_final"] for r in rows if r.get("clv_final") is not None]
                edges    = [r["initial_edge"] for r in rows if r.get("initial_edge") is not None]

                # Task 4: never show a win rate without its Wilson 95% CI
                # and the tax-adjusted breakeven probability for the
                # segment's average odds — a bare percentage hides both
                # small-sample noise and whether it's even enough to clear
                # TAX_RATE.
                ci_lo, ci_hi = wilson_ci(wins, len(decisive))
                decisive_odds = [r["odds"] for r in decisive if r.get("odds")]
                avg_odds = sum(decisive_odds) / len(decisive_odds) if decisive_odds else None
                breakeven = p_breakeven(avg_odds, _TAX_RATE) if avg_odds else None

                global_s = {
                    "total":        len(rows),
                    "settled":      len(settled),
                    "wins":         wins,
                    "losses":       losses,
                    "pushes":       pushes,
                    "win_rate":     round(wins / max(wins + losses, 1) * 100, 1),
                    "win_rate_lo":  round(ci_lo * 100, 1),
                    "win_rate_hi":  round(ci_hi * 100, 1),
                    "p_breakeven":  round(breakeven * 100, 1) if breakeven is not None else None,
                    "above_breakeven": (breakeven is not None and ci_lo > breakeven),
                    "avg_clv":      round(sum(clv_all) / len(clv_all), 2) if clv_all else None,
                    "avg_edge":     round(sum(edges) / len(edges), 2) if edges else None,
                    "clv_hit":      round(sum(1 for c in clv_all if c >= 0) / max(len(clv_all), 1) * 100, 1) if clv_all else None,
                    "clv_is_real":  bool(clv_real),
                    "clv_n":        len(clv_all),
                }

                # TAUX DE RÉSOLUTION (B6, 2026-08-27). Tout ce qui précède ne
                # compte que les lignes RÉGLÉES : les `expired` — signaux
                # purgés avant qu'un score ait pu être trouvé — sortent de
                # chaque agrégat. La page mesurait donc les paris qu'on a
                # réussi à SUIVRE et présentait ce résultat comme celui de
                # tous les paris.
                # Le biais n'est pas neutre : le règlement échoue plus souvent
                # là où l'appariement de noms échoue, c'est-à-dire sur les
                # ligues obscures et les sources douteuses — exactement les
                # lignes dont l'edge est le plus suspect. Les écarter embellit
                # la page, dans le sens précis qui flatte le moteur.
                global_s["resolution"] = _resolution_rate(rows)

                # Per-sport win rate + Wilson CI + breakeven
                sport_perf: dict = {}
                for sport in sorted(set(r.get("sport", "") for r in decisive) - {""}):
                    sv = [r for r in decisive if r.get("sport") == sport]
                    sw = sum(1 for r in sv if r["outcome"] == "WIN")
                    slo, shi = wilson_ci(sw, len(sv))
                    sodds = [r["odds"] for r in sv if r.get("odds")]
                    savg  = sum(sodds) / len(sodds) if sodds else None
                    sbreak = p_breakeven(savg, _TAX_RATE) if savg else None
                    sport_perf[sport] = {
                        "n":              len(sv),
                        "win_rate":       round(sw / len(sv) * 100, 1),
                        "win_rate_lo":    round(slo * 100, 1),
                        "win_rate_hi":    round(shi * 100, 1),
                        "p_breakeven":    round(sbreak * 100, 1) if sbreak is not None else None,
                        "above_breakeven": (sbreak is not None and slo > sbreak),
                    }
                global_s["by_sport"] = sport_perf

                # (Retiré le 2026-08-22 : le tableau de CALIBRATION — Brier
                # par tranche de confiance — quittait la page. Le score de
                # Brier reste calculé et suivi côté pipeline, table
                # `brier_scores`, et repris dans le rapport hebdomadaire.)

                # (Retiré le 2026-08-22 : la section « PAR MOIS » quittait
                # la page. Depuis que l'époque du système est fixée à août
                # 2026 — core/perf_view.PERF_START_MONTH — il n'y a qu'UN
                # mois à montrer, et une grille de cartes pour une seule
                # carte répète simplement les chiffres déjà en haut de page.
                # Le découpage mensuel reste disponible dans le rapport
                # hebdomadaire, scripts/weekly_report.py.)

    except Exception as e:
        log.error("Performance: %s", e)

    return render_template("performance.html", rows=rows, history=history,
                           global_s=global_s,
                           sport_emoji=_SPORT_EMOJI, sport_label=_SPORT_LABEL)


@app.route("/system")
def system():
    return render_template("system.html")


# ── JSON API ─────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    """Signaux actifs ENCORE JOUABLES (voir _is_playable).

    `?all=1` rend la liste brute, coup d'envoi passé compris — pour le
    diagnostic uniquement. Par défaut on filtre : cette API alimente une
    interface de mise, et un match commencé n'est pas un pari.
    """
    try:
        sb = _db()
        if not sb:
            return jsonify({"error": "no db"}), 503
        res = (sb.table("signals").select("*").eq("status", "active")
               .order("created_at", desc=True).limit(200).execute())
        rows = res.data or []
        if request.args.get("all") not in ("1", "true", "yes"):
            now  = datetime.now(_tz.utc)
            rows = [s for s in rows if _is_playable(s, now)]
        return jsonify(rows[:50])
    except Exception as e:
        log.error("api_signals: %s", e)
        return jsonify({"error": "internal error"}), 500


ADMIN_TOKEN_ENV = "DASHBOARD_ADMIN_TOKEN"


def _admin_autorise() -> bool:
    """Le jeton d'administration est-il présent et correct ?

    ÉCHEC FERMÉ : sans `DASHBOARD_ADMIN_TOKEN` configuré, la réponse est
    NON. Une route d'administration qui s'ouvre faute de configuration est
    précisément le défaut qu'on corrige ici.

    Comparaison à temps constant : `==` sur une chaîne s'arrête au premier
    octet différent, ce qui laisse mesurer le préfixe correct.

    ⚠️ EN-TÊTE `X-Predator-Token` UNIQUEMENT — le jeton en QUERY STRING
    (`?token=…`) a été retiré le 2026-08-27 (C1). Il avait été admis « pour
    un curl d'opérateur », documenté comme moins sûr, et accepté quand même.
    Ce que « moins sûr » recouvre vraiment :

      · une URL n'est pas un canal privé. Elle est écrite en clair dans les
        logs d'accès de Vercel, dans les logs du proxy, dans l'historique du
        shell, dans le `Referer` envoyé à tout tiers, et dans l'historique du
        navigateur si elle y est collée une fois ;
      · ces journaux survivent au jeton : une rotation ne les efface pas ;
      · un en-tête, lui, n'apparaît dans aucun de ces endroits par défaut.

    Le retrait ne casse rien, vérifié avant : AUCUN appelant du dépôt
    n'utilisait la query string. README.md et AUDIT.md documentent déjà la
    forme avec en-tête, et aucune page du dashboard n'appelle cette route.
    Le seul usage était un test, qui exige désormais le refus.
    """
    attendu = os.environ.get(ADMIN_TOKEN_ENV, "")
    if not attendu:
        return False
    if request.args.get("token"):
        # Refus BRUYANT côté serveur, muet côté client : l'appelant reçoit le
        # même 401 que n'importe quel refus, mais l'opérateur qui lit les logs
        # comprend pourquoi son ancien `curl` ne passe plus.
        log.warning("jeton d'admin fourni en query string — REFUSÉ : une URL "
                    "est journalisée par Vercel, le proxy, le shell et le "
                    "navigateur. Utiliser l'en-tête X-Predator-Token.")
    fourni = request.headers.get("X-Predator-Token") or ""
    return hmac.compare_digest(fourni, attendu)


@app.route("/api/audit/run", methods=["POST"])
def trigger_audit():
    """Déclenche `audit.yml` — RÉSERVÉ À L'ADMINISTRATION.

    AUTHENTIFICATION AJOUTÉE LE 2026-08-22. Cette route était ouverte à
    tout Internet. Le dashboard est déployé sur une URL Vercel publique et
    aucune interface ne l'appelle — mais un POST anonyme suffisait à
    déclencher `audit.yml` : 45 minutes de runner, le settlement, et la
    consommation de la réserve IA gardée en négatif exprès. Sans cooldown
    et sans limite de débit, une boucle `curl` épuisait le quota — et
    CLAUDE.md rappelle ce que coûte un quota épuisé : dix jours sans
    signal (incident du 10→20 août 2026).

    Le cron de `audit.yml` (toutes les 6 h) et le `workflow_dispatch` depuis
    l'interface GitHub restent les chemins normaux ; cette route n'est qu'un
    raccourci d'opérateur. Elle refuse donc tant que
    `DASHBOARD_ADMIN_TOKEN` n'est pas configuré sur le déploiement.
    """
    if not _admin_autorise():
        # Volontairement muet sur la cause (jeton non configuré / mauvais
        # jeton) : la distinction n'aide que celui qui cherche à entrer.
        log.warning("audit trigger refusé (jeton absent ou invalide)")
        return jsonify({"error": "non autorisé"}), 401

    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        return jsonify({"error": "GITHUB_PAT non configuré"}), 503
    try:
        resp = requests.post(
            "https://api.github.com/repos/ipotrading-bot/predator/actions/workflows/audit.yml/dispatches",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=10,
        )
        if resp.status_code == 204:
            return jsonify({"status": "triggered"}), 200
        # La réponse de GitHub n'est PAS recopiée telle quelle : elle peut
        # nommer le dépôt, le workflow, la portée du jeton. Le détail va au
        # log du déploiement, où il sert au diagnostic sans être publié.
        log.error("audit dispatch: HTTP %s — %s", resp.status_code, resp.text[:300])
        return jsonify({"error": "le déclenchement a échoué",
                        "github_status": resp.status_code}), 502
    except Exception as e:
        log.error("audit dispatch: %s", e)
        return jsonify({"error": "le déclenchement a échoué"}), 502


@app.route("/manifest.json")
def manifest():
    return send_from_directory(_static_dir, "manifest.json", mimetype="application/manifest+json")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(_static_dir, "favicon.ico", mimetype="image/x-icon")


@app.route("/api/health")
def health():
    """Santé du DASHBOARD, pas du pipeline.

    Ne fait volontairement aucun appel réseau vers Supabase : cette route
    doit rester utilisable comme sonde de disponibilité (Vercel, uptime
    externe) même quand la base est injoignable. Elle dit donc deux choses
    honnêtes : le processus Flask répond, et les credentials nécessaires
    sont-ils présents dans cet environnement.

    Le champ `source` a été supprimé : il annonçait « harvester+ai_search »,
    figé depuis des mois, alors que la collecte passe aujourd'hui par
    OddsAPI + Matchbook + api-sports + les sources gratuites. Un champ
    d'information qui ne se met pas à jour est pire qu'absent.
    """
    return jsonify({
        "status":     "ok",
        "version":    DASHBOARD_VERSION,
        "db_configured": bool(os.environ.get("SUPABASE_URL")
                              and os.environ.get("SUPABASE_KEY")),
        "time":       datetime.now(_tz.utc).isoformat(),
    })


# (/api/odds-quota supprimé le 2026-08-22 — Mission 2, Phase 2 : le widget
# « Quota OddsAPI » de la page Sys est remplacé par une ALERTE Telegram
# backend (run_engine._alert_oddsapi_pool_levels : paliers 20 % et 5 %,
# dédupliqués 24h via meta) + une ligne de log à chaque run. La surveillance
# devient invisible mais pas muette — voir l'incident du 10→20 août 2026.)


# scan.yml lit ce flag à CHAQUE tick (36/jour) depuis la refonte du
# 2026-08-26, contre 24 auparavant : la latence tombe sous l'heure. Un tick
# `golden` est promu en scan complet ; les autres modes en sont déjà un.
_SCAN_REQUEST_COOLDOWN_S = 120

# ── Limite de débit par IP sur /api/scan (C2, 2026-08-27) ────────────────
# La route est appelée par un BOUTON PUBLIC du dashboard (templates/index.html,
# `triggerScan`). Un jeton y serait écrit dans le JavaScript servi à tout le
# monde : ce ne serait pas une protection, seulement l'illusion d'une. C'est
# donc une limite de débit — avec une dérogation par jeton pour l'opérateur,
# qui lui peut le garder secret.
_SCAN_RATE_LIMIT_N = 3            # requêtes autorisées…
_SCAN_RATE_LIMIT_WINDOW_S = 300   # …par IP et par fenêtre de 5 minutes

# {ip: [horodatages]} — mémoire du PROCESSUS.
_scan_hits: dict[str, list[float]] = {}


def _client_ip() -> str:
    """L'IP de l'appelant, telle que le proxy de déploiement la rapporte.

    ⚠️ `request.remote_addr` derrière Vercel est l'IP du PROXY, identique pour
    tout le monde : s'en servir ferait partager un seul compteur à la planète
    entière, et le premier visiteur bloquerait tous les autres.

    `x-vercel-forwarded-for` est posé par la plateforme et n'est pas
    falsifiable par le client ; `x-forwarded-for` l'est — un attaquant qui le
    fabrique obtient un compteur neuf à chaque requête. On préfère donc le
    premier, et on garde le second en repli EN SACHANT ce qu'il vaut. C'est
    précisément cette limite que C3 lève en déplaçant le comptage dans
    Postgres.
    """
    entete = (request.headers.get("x-vercel-forwarded-for")
              or request.headers.get("x-forwarded-for") or "")
    if entete:
        # Le premier élément est le client d'origine ; les suivants sont les
        # relais traversés.
        return entete.split(",")[0].strip()
    return request.remote_addr or "inconnue"


def _scan_rate_limited(ip: str, maintenant: float | None = None) -> bool:
    """Cette IP a-t-elle dépassé son quota ? Enregistre la requête si non.

    ⚠️ CE QUE CETTE LIMITE VAUT, ET CE QU'ELLE NE VAUT PAS. Le compteur vit
    dans la MÉMOIRE DU PROCESSUS. Sur Vercel, chaque instance de fonction a la
    sienne, et une instance froide démarre à zéro : la limite arrête un flot
    naïf venu d'une seule IP vers une instance chaude — le cas courant — et
    n'arrête PAS un flot distribué, ni un attaquant qui fabrique son
    `x-forwarded-for`.

    Elle a malgré tout un effet réel et immédiat : elle refuse AVANT de créer
    un client service_role et AVANT de lire `meta`. Chaque requête abusive
    coûtait jusque-là une lecture Supabase ; elle ne coûte plus rien.

    La vraie limite, partagée entre instances et non falsifiable, est l'objet
    de C3 — comptée dans Postgres, du côté où l'état est commun.
    """
    maintenant = time.time() if maintenant is None else maintenant
    debut = maintenant - _SCAN_RATE_LIMIT_WINDOW_S
    recents = [t for t in _scan_hits.get(ip, []) if t > debut]
    if len(recents) >= _SCAN_RATE_LIMIT_N:
        _scan_hits[ip] = recents
        return True
    recents.append(maintenant)
    _scan_hits[ip] = recents
    # Purge des IP dont la fenêtre est vide : sans elle, le dictionnaire
    # grandit indéfiniment sur une instance chaude longue durée.
    for autre in [k for k, v in _scan_hits.items() if not v or v[-1] <= debut]:
        _scan_hits.pop(autre, None)
    return False


@app.route("/api/scan", methods=["POST"])
def trigger_scan():
    """Demande un scan — pose `meta.scan_request`, ramassé par `scan.yml`.

    OUVERTE À TOUT INTERNET JUSQU'AU 2026-08-27 (C2). N'importe quel site
    pouvait déclencher l'upsert : le dashboard est servi depuis une URL Vercel
    publique et la route n'exigeait rien. Le cooldown global de 120 s bornait
    déjà la FRÉQUENCE d'écriture, mais chaque requête refusée coûtait quand
    même la création d'un client service_role et une lecture de `meta` — et
    rien n'empêchait de maintenir un scan perpétuellement en attente, donc de
    forcer un scan complet à chaque tick de cron.

    La limite par IP s'applique AVANT tout accès à la base. Un jeton
    d'administration valide en dispense : c'est le seul appelant qui puisse
    garder un secret, le bouton du dashboard étant, lui, servi à tout le monde.
    """
    # L'opérateur authentifié n'est pas limité : `_admin_autorise` exige
    # l'en-tête `X-Predator-Token` (C1), jamais l'URL.
    if not _admin_autorise():
        ip = _client_ip()
        if _scan_rate_limited(ip):
            log.warning("scan refusé — limite de débit atteinte pour %s "
                        "(%d requêtes / %d s)", ip, _SCAN_RATE_LIMIT_N,
                        _SCAN_RATE_LIMIT_WINDOW_S)
            return jsonify({
                "status":  "rate_limited",
                "message": "Trop de demandes — réessayez dans quelques minutes",
            }), 429

    # ── C3 (2026-08-27) : plus AUCUNE clé d'écriture dans le dashboard ────
    # Cette route était la SEULE écriture d'`api/index.py`, et elle exigeait la
    # clé service_role — donc les pleins pouvoirs sur `signals`,
    # `ai_learning_ledger`, `meta` et `app_secrets` pour une fonction servie
    # publiquement. Elle passe désormais par `demander_scan()`, une fonction
    # Postgres `security definer` appelable avec la clé de LECTURE
    # (sql/migrate_v10_9_scan_request_rpc.sql).
    #
    # Le cooldown ET la limite de débit vivent maintenant DANS la fonction.
    # C'est le point de C3 : la limite en mémoire ajoutée par C2 repart à zéro
    # à chaque instance froide de Vercel et ne se partage pas entre instances ;
    # celle-ci est unique, partagée, et l'appelant ne peut pas la contourner.
    # La limite en mémoire est CONSERVÉE en amont — elle refuse sans même
    # ouvrir une connexion, ce que le SQL ne peut pas faire par construction.
    sb = _db()
    if not sb:
        return jsonify({"error": "Base de données non configurée"}), 503
    try:
        reponse = sb.rpc("demander_scan", {"p_ip": _client_ip()}).execute()
        resultat = reponse.data if isinstance(reponse.data, dict) else {}
    except Exception as exc:
        # Le détail va au log du déploiement, pas dans la réponse : un message
        # PostgREST brut nomme la fonction, le schéma et la politique qui a
        # refusé. Utile en diagnostic, inutile à publier.
        log.error("scan queue error: %s", exc)
        return jsonify({"error": "la demande de scan a échoué"}), 500

    statut = resultat.get("status")
    if statut == "queued":
        return jsonify(resultat), 200
    if statut in ("already_queued", "rate_limited"):
        return jsonify(resultat), 429
    # `error`, ou une forme inattendue : on ne relaie pas le détail.
    log.error("demander_scan a rendu une réponse inattendue : %.200s", resultat)
    return jsonify({"error": "la demande de scan a échoué"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
