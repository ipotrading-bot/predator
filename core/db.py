"""
core/db.py — Single source of truth for Supabase client creation.

RLS policies restrict INSERT/UPDATE/DELETE on signals/ai_learning_ledger/meta
to the service_role. Every write call site in this repo used to roll its own
`os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")`
fallback — which authenticates fine on the anon key (no 401) and then fails
every single write with a Postgres 42501 (row-level security), one row at a
time, with no single clear error pointing at the actual cause. That's what
produced the 2026-07-07 incident: ~17h of "0/N signals persisted" across
every scheduled run, each showing green (success) in GitHub Actions because
nothing ever raised.

get_db(write=True) fails fast and loud instead: it resolves the key's
privilege level locally (no network call) and refuses to hand back a client
if it isn't actually service_role — so a wrong/missing key aborts the run in
its first second with an unambiguous message, not 17 hours later across a
wall of per-signal RLS errors. This covers both Supabase key formats: the
legacy JWT (role claim in the payload) and the newer opaque
sb_secret_.../sb_publishable_... keys (role is the prefix) — the first
version of this fix only handled the JWT format, which passed on GitHub
Actions but kept failing on Vercel because that deployment's key was the
newer format and decoded to role=None instead of a clear anon/service_role
verdict.
"""
import base64
import json
import logging
import os

from supabase import create_client

log = logging.getLogger("PREDATOR.db")


class MissingCredentialsError(RuntimeError):
    pass


def _key_role(token: str) -> str | None:
    """Identify the privilege level of a Supabase API key, old or new format.

    Supabase has two key formats in the wild:
    - Legacy JWT (still issued for most projects as of writing): a
      `header.payload.signature` token whose payload has a `role` claim of
      `anon` or `service_role`.
    - New-style keys (`sb_publishable_...` / `sb_secret_...`): opaque
      strings, not JWTs — the role is encoded in the prefix itself, there's
      nothing to decode. A project provisioned/migrated after Supabase's
      2025 key rotation shows these on its dashboard instead of (or beside)
      the legacy anon/service_role JWTs.

    Returns 'anon', 'service_role', or None if neither format is recognized.
    """
    if token.startswith("sb_secret_"):
        return "service_role"
    if token.startswith("sb_publishable_"):
        return "anon"
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)  # restore stripped b64 padding
        payload = json.loads(base64.urlsafe_b64decode(segment))
        return payload.get("role")
    except Exception:
        return None


def get_db(write: bool = False):
    """
    Returns a configured Supabase client, or None if the DB isn't configured
    at all (no SUPABASE_URL, or no usable key for the requested mode) —
    callers that treat "no DB" as a soft/optional condition should check for
    None.

    write=False (default): read-only usage. The anon key is correct here —
      RLS SELECT policies allow it, and reads should never require the more
      privileged service_role key.

    write=True: caller intends to INSERT/UPDATE/DELETE. Requires
      SUPABASE_SERVICE_KEY to be set AND to actually decode to
      role="service_role". Raises MissingCredentialsError immediately
      otherwise — never silently falls back to the anon key for writes.
    """
    url = os.environ.get("SUPABASE_URL")
    if not url:
        return None

    if write:
        service_key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not service_key:
            raise MissingCredentialsError(
                "SUPABASE_SERVICE_KEY is not set. Writes require the "
                "service_role key — RLS rejects INSERT/UPDATE/DELETE from "
                "the anon key. Refusing to silently fall back to SUPABASE_KEY."
            )
        role = _key_role(service_key)
        if role != "service_role":
            raise MissingCredentialsError(
                f"SUPABASE_SERVICE_KEY resolves to role={role!r}, not "
                "'service_role' — this is the anon/publishable key (or an "
                "unrecognized value), not the service_role/secret key. "
                "Writes will be rejected by RLS. Fix: Supabase dashboard -> "
                "Project Settings -> API Keys -> copy the 'service_role' "
                "(legacy JWT) or 'secret' (sb_secret_... new format) key "
                "into this GitHub/Vercel secret — not 'anon'/'publishable'."
            )
        return create_client(url, service_key)

    anon_key = os.environ.get("SUPABASE_KEY")
    if not anon_key:
        return None
    return create_client(url, anon_key)


# ── Écriture sur une ligne existante : UPDATE, et rien d'autre ───────────
#    `settlement` et `audit_engine` passaient par un DELETE+INSERT justifié
#    par un « RLS blocks UPDATE outright » devenu faux : la policy
#    `service_role_update` existe depuis migrate_v9_3. Voir la docstring de
#    `update_signal_fields` pour ce que ce détour coûtait.

def update_signal_fields(sb, signal_id, fields: dict,
                         optional_cols: frozenset = frozenset()) -> bool:
    """
    Patch `fields` onto signal `signal_id` with a real UPDATE.

    SEUL CHEMIN D'ÉCRITURE SUR UNE LIGNE EXISTANTE depuis le 2026-08-27 (B1).
    Il remplace une fonction de DELETE suivi d'INSERT, supprimée ce jour-là,
    qui portait trois dégâts — dont deux se produisaient à chaque appel
    RÉUSSI :

      · entre les deux ordres la ligne N'EXISTAIT PAS. Un processus tué au
        milieu — timeout GitHub Actions, coupure réseau — perdait le signal
        DÉFINITIVEMENT. Le chemin était si peu théorique qu'il portait son
        propre log CRITICAL, « SIGNAL %s LOST after delete » ;
      · `id` étant retiré avant le ré-INSERT, chaque appel donnait au signal
        un identifiant NEUF. Le `signal_id` déjà recopié dans
        `ai_learning_ledger` ne désignait alors plus rien ;
      · il fallait lui passer la ligne ENTIÈRE (`{**sig, **patch}`), donc
        réécrire des colonnes qu'on n'avait pas lues pour les modifier — au
        risque d'écraser une capture de closing line posée entre-temps.

    Un UPDATE n'a aucun de ces trois défauts, et la policy RLS
    `service_role_update` existe depuis `sql/migrate_v9_3_tighten_rls.sql`.
    Vérifiée EN BASE le 2026-08-27 : un UPDATE sur `signals` passe. Le
    commentaire « RLS blocks UPDATE outright » qui justifiait le détour était
    périmé.

    Dégradation chirurgicale sur schéma en retard : seules les colonnes que
    Postgres NOMME sont retirées. Un échec est ici sans conséquence — rien n'a
    été supprimé, au pire ce tour est sauté et le suivant réessaie.
    """
    if not fields:
        return True
    payload = dict(fields)
    payload.pop("id", None)
    try:
        sb.table("signals").update(payload).eq("id", signal_id).execute()
        return True
    except Exception as e:
        if not optional_cols:
            log.error("update_signal_fields [%s]: %s", signal_id, e)
            return False
        named = {c for c in optional_cols if c in str(e)}
        for dropped in ([named] if named else []) + [set(optional_cols)]:
            if not dropped:
                continue
            core = {k: v for k, v in payload.items() if k not in dropped}
            if not core:
                continue
            try:
                sb.table("signals").update(core).eq("id", signal_id).execute()
                log.warning("Signal %s updated without %s — apply the pending "
                            "migration for these columns", signal_id, sorted(dropped))
                return True
            except Exception as e2:
                e = e2
        log.error("update_signal_fields [%s] failed: %s", signal_id, e)
        return False


def is_unique_violation(err: str) -> bool:
    """Un INSERT refusé parce que la ligne existe déjà (Postgres 23505).

    Reconnu sur le CODE d'abord — PostgREST le remonte tel quel — et sur le
    libellé ensuite, parce que le client peut aplatir l'erreur en chaîne.

    POINT UNIQUE : `run_engine._save` (index unique sur `signals`) et
    `log_to_ledger` (index unique sur le ledger) s'en servent tous les deux.
    Deux copies de cette reconnaissance finiraient par diverger, et l'une des
    deux prendrait alors une collision NORMALE pour une panne d'écriture —
    perdant le signal ou faisant partir un log CRITICAL à chaque re-scan.
    """
    bas = err.lower()
    return ("23505" in err
            or "duplicate key" in bas
            or "already exists" in bas
            or "unique constraint" in bas)


_DECISIF = ("WIN", "LOSS", "PUSH")


def _ledger_deja_ecrit(sb, signal_id, outcome: str) -> bool:
    """Le résultat de ce signal est-il DÉJÀ au ledger, et faut-il en rester là ?

    Appelée après une violation d'unicité sur `ledger_signal_id_uniq`
    (sql/migrate_v10_8). Rend True quand il n'y a plus rien à faire.

    La règle n'est pas « le premier gagne » mais « le DÉCISIF gagne ». Deux
    chemins peuvent écrire le résultat d'un même signal — `settle_signal` avec
    un vrai WIN/LOSS, `_archive_before_purge` avec un `expired` —, et laisser
    le dernier écraser le premier pourrait remplacer un résultat réel par une
    absence de résultat. On ne remplace donc que dans un sens : quand la ligne
    stockée ne porte AUCUN résultat et que celle qui arrive en porte un.

    Sur erreur de lecture, rend True : ne rien faire est le comportement sûr,
    puisque la ligne existe déjà.
    """
    try:
        res = (sb.table("ai_learning_ledger").select("id,outcome")
               .eq("signal_id", signal_id).limit(1).execute())
        rows = res.data or []
    except Exception as e:
        log.warning("Ledger déjà écrit pour le signal %s, relecture impossible "
                    "(%s) — on ne touche à rien", signal_id, str(e)[:80])
        return True
    if not rows:
        return False        # la collision venait d'ailleurs : laisser remonter
    stocke = str(rows[0].get("outcome") or "")
    if stocke in _DECISIF or outcome not in _DECISIF:
        log.info("Ledger : signal %s déjà enregistré (%s) — écriture ignorée, "
                 "le doublon fausserait le n de la couche d'apprentissage",
                 signal_id, stocke or "sans résultat")
        return True
    try:
        sb.table("ai_learning_ledger").update({"outcome": outcome}) \
          .eq("id", rows[0]["id"]).execute()
        log.info("Ledger : signal %s passe de %r à %r — un résultat réel "
                 "remplace une absence de résultat", signal_id, stocke, outcome)
    except Exception as e:
        log.error("Ledger : promotion de %s vers %s impossible : %s",
                  signal_id, outcome, str(e)[:80])
    return True


def _ledger_jumeau_reel(sb, payload: dict) -> bool:
    """Le MÊME MATCH RÉEL est-il déjà au ledger sous un AUTRE signal_id ?

    JUMEAUX INTER-SOURCES (mesuré le 2026-09-02) : un même match arrivant par
    deux sources porte deux match_id différents (uuid OddsAPI vs id dérivé des
    noms), donc deux signaux jumeaux coexistent et `ledger_signal_id_uniq` ne
    voit rien — 47 paires exactes + 7 floues dans le ledger d'août, soit ~10 %
    de lignes en double qui gonflaient le n de la couche d'apprentissage et
    l'historique /performance (archivées par sql/migrate_v10_10).

    Comparaison VOLONTAIREMENT exacte (match, selection, market_type) sur une
    fenêtre de 6 jours : l'appariement flou par noms a été essayé le même jour
    et rendait des faux positifs (U23/U19 contre seniors, « Atletico Junior »
    contre « Atletico Nacional ») — supprimer une vraie ligne de résultat est
    pire qu'un doublon, qui reste rattrapable par archivage. Les jumeaux à
    libellés différents relèvent du pont d'alias, pas d'ici.

    Même règle que `_ledger_deja_ecrit` : le DÉCISIF gagne. Ligne stockée
    décisive (ou entrant non décisif) → on n'écrit pas ; entrant décisif sur
    stocké non décisif → on PROMEUT la ligne stockée au lieu d'en créer une
    seconde. Sur panne de lecture → False : on laisse l'insert se faire,
    perdre un résultat réel serait définitif, un doublon ne l'est pas.
    """
    match = payload.get("match")
    selection = payload.get("selection")
    market_type = payload.get("market_type")
    if not match or not selection or not market_type:
        return False       # sans clé complète on ne compare pas, on n'invente pas
    from datetime import datetime, timedelta, timezone
    seuil = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    try:
        res = (sb.table("ai_learning_ledger")
               .select("id,signal_id,outcome")
               .eq("match", match).eq("selection", selection)
               .eq("market_type", market_type)
               .gte("created_at", seuil).limit(5).execute())
        rows = [r for r in (res.data or [])
                if r.get("signal_id") != payload.get("signal_id")]
    except Exception as e:
        log.warning("Ledger : recherche de jumeau impossible pour %s (%s) — "
                    "on laisse l'écriture se faire", match, str(e)[:80])
        return False
    if not rows:
        return False
    stocke = str(rows[0].get("outcome") or "")
    outcome = str(payload.get("outcome") or "")
    if stocke in _DECISIF or outcome not in _DECISIF:
        log.info("Ledger : %s | %s déjà réglé (%s) sous le signal %s — jumeau "
                 "inter-sources du signal %s ignoré, le doublon fausserait le n "
                 "de la couche d'apprentissage", match, selection,
                 stocke or "sans résultat", rows[0].get("signal_id"),
                 payload.get("signal_id"))
        return True
    try:
        sb.table("ai_learning_ledger").update({"outcome": outcome}) \
          .eq("id", rows[0]["id"]).execute()
        log.info("Ledger : jumeau %s | %s promu de %r à %r (signal %s) au lieu "
                 "d'une seconde ligne", match, selection, stocke, outcome,
                 rows[0].get("signal_id"))
    except Exception as e:
        log.error("Ledger : promotion du jumeau %s vers %s impossible : %s",
                  match, outcome, str(e)[:80])
    return True


def log_to_ledger(sb, sig: dict, clv: float, outcome: str) -> None:
    """Insert one row into ai_learning_ledger for a settled/closed/expired
    signal. Failure here is logged CRITICAL (not swallowed as routine) —
    it means /performance and the learning layer silently never see this
    outcome, most commonly because
    sql/migrate_v9_4_ledger_display_fields.sql hasn't been applied yet.

    `clv` is CLV only when the caller genuinely re-fetched a price at a
    later point in time (core/audit_engine.py's oracle pass); when the
    caller is core/settlement.py's settle_signal(), it is a re-derivation
    of the entry edge (identical to `initial_edge` below) and must never be
    used as a real closing-line signal — see core/settlement.py for why.
    core/learning_layer.py must key its threshold adjustments off `outcome`
    (real WIN/LOSS), never off this field.

    `kelly_pct` carries the Kelly stake sizing (% of bankroll) recorded on
    the signal at scan time, so a real ROI can later be computed as a
    stake-weighted average — Σ(kelly_pct·(odds-1)) if WIN else -kelly_pct,
    / Σ(kelly_pct) — instead of a flat per-bet average.

    `closing_pinnacle_price`/`clv_pct_real` carry core/audit_engine.py's
    genuine closing-line capture (Task 3, run_closing_line.py's hourly
    job) forward past this signal's eventual purge from `signals` — this
    is the only clv_pct_real* field that IS real CLV; see above for why
    `clv` isn't.

    `sharp_prob` carries the model's own predicted win probability at scan
    time — needed for a Brier score (core/stats_utils.py), which a bare
    win rate can't provide since it says nothing about whether the STATED
    confidence was trustworthy.

    All of the above require sql/migrate_v9_5_learning_integrity.sql,
    sql/migrate_v9_6_closing_line.sql, and sql/migrate_v9_7_ledger_brier.sql;
    until applied, the insert retries once with those columns stripped
    (same optional_cols pattern as update_signal_fields)."""
    match_time = sig.get("match_time")
    scanned_at = sig.get("scanned_at")
    ttm = None
    if match_time and scanned_at:
        try:
            from datetime import datetime as _dt
            mt = _dt.fromisoformat(match_time.replace("Z", "+00:00"))
            sc = _dt.fromisoformat(scanned_at.replace("Z", "+00:00"))
            ttm = int((mt - sc).total_seconds() / 60)
        except Exception:
            log.debug("_ttm parse failed for match_time=%s scanned_at=%s", match_time, scanned_at)
    # signals.sharp_sources is TEXT holding a JSON string (see _emit in
    # run_engine.py), but ai_learning_ledger.sharp_sources is jsonb
    # (sql/migrate_v9_10_ledger_consensus.sql). Inserting the raw string would
    # store a JSON *scalar*, so `sharp_sources->>'circa'` would return NULL —
    # exactly the query this column exists to serve. Decode it to a real object.
    sharp_sources = sig.get("sharp_sources")
    if isinstance(sharp_sources, str):
        try:
            sharp_sources = json.loads(sharp_sources)
        except (ValueError, TypeError):
            log.debug("sharp_sources not decodable, dropping: %.60s", sharp_sources)
            sharp_sources = None
    _optional = {
        "kelly_pct":              sig.get("kelly_pct"),
        "closing_pinnacle_price": sig.get("closing_pinnacle_price"),
        "clv_pct_real":           sig.get("clv_pct_real"),
        "closing_captured_at":    sig.get("closing_captured_at"),
        # 'oddsapi' (exact, per-market, from the scan feed — core/closing_line.py)
        # or 'oracle' (web-search estimate of the ML/DNB favourite). Carried
        # into the ledger so an analysis can weight or filter on provenance
        # instead of treating both as the same measurement.
        "closing_source":         sig.get("closing_source"),
        "sharp_prob":             sig.get("sharp_prob"),
        "sharp_sources":          sharp_sources,
        "consensus_score":        sig.get("consensus_score"),
    }
    payload = {
        "signal_id":             sig.get("id"),
        "match":                 sig["match"],
        "sport":                 sig.get("sport"),
        "league":                sig.get("league"),
        "market_type":           sig.get("market_key"),
        "market":                sig.get("market"),
        "selection":             sig.get("selection_name"),
        "odds":                  sig.get("xbet_odd"),
        "time_to_match_minutes": ttm,
        "initial_edge":          sig.get("edge_pct"),
        "sharp_divergence_std":  None,
        "clv_final":             clv,
        "was_clv_positive":      clv > 0,
        "outcome":               outcome,
        **_optional,
    }
    # Jumeau inter-sources : même match réel déjà réglé sous un autre
    # signal_id → pas de seconde ligne (voir _ledger_jumeau_reel).
    if _ledger_jumeau_reel(sb, payload):
        return
    try:
        sb.table("ai_learning_ledger").insert(payload).execute()
        return
    except Exception as e:
        # IDEMPOTENCE (B3, 2026-08-27). L'index `ledger_signal_id_uniq`
        # refuse une seconde ligne pour le même signal. Ce refus n'est PAS une
        # panne : c'est un audit rejoué, un règlement retenté après timeout,
        # deux workflows qui se croisent. Le traiter comme une erreur ferait
        # partir un log CRITICAL sur un fonctionnement normal ; l'ignorer
        # aurait laissé le doublon gonfler le `n` de `learning_layer`, qui
        # aurait alors resserré ses intervalles de Wilson SANS information
        # nouvelle — la façon la plus discrète de se croire sûr de soi.
        if payload.get("signal_id") is not None and is_unique_violation(str(e)):
            if _ledger_deja_ecrit(sb, payload["signal_id"], outcome):
                return
        # Strip only the columns the error actually names — dropping the whole
        # optional set for one missing column threw away kelly_pct/sharp_prob
        # from rows that could have kept them (same reasoning as
        # update_signal_fields). Wholesale strip stays as the last resort.
        named = {c for c in _optional if c in str(e)}
        for dropped in ([named] if named else []) + [set(_optional)]:
            try:
                core = {k: v for k, v in payload.items() if k not in dropped}
                sb.table("ai_learning_ledger").insert(core).execute()
                log.warning("Ledger row for %s written without %s — apply the "
                            "pending migration for these columns",
                            sig.get("match"), sorted(dropped))
                return
            except Exception as e2:
                e = e2
        log.critical("ai_learning_ledger INSERT FAILED [%s] — check migrations "
                      "sql/migrate_v9_4_ledger_display_fields.sql, "
                      "sql/migrate_v9_5_learning_integrity.sql, "
                      "sql/migrate_v9_6_closing_line.sql, "
                      "sql/migrate_v9_7_ledger_brier.sql, "
                      "sql/migrate_v9_10_ledger_consensus.sql, and "
                      "sql/migrate_v9_11_closing_captured_at.sql are applied: %s",
                      sig.get("match"), e)
