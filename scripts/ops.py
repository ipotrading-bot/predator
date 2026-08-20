#!/usr/bin/env python3
"""
scripts/ops.py — pilotage Supabase + Vercel depuis le terminal, sans CLI.

    python scripts/ops.py doctor                      # quelles credentials sont présentes, que peut-on faire
    python scripts/ops.py status                      # santé en un écran : clés OddsAPI, dernier signal, seuils, dernier déploiement
    python scripts/ops.py sources                     # sonde CHAQUE source de cotes : vivante ? quota ? joignable depuis cette IP ?

    python scripts/ops.py supabase secrets list       # app_secrets (noms + mis à jour, jamais les valeurs)
    python scripts/ops.py supabase secrets set KEY VAL
    python scripts/ops.py supabase secrets del KEY
    python scripts/ops.py supabase meta [prefix]      # table meta (seuils, caches, horodatages)
    python scripts/ops.py supabase meta-set KEY VAL
    python scripts/ops.py supabase signals [jours]    # signaux par jour/statut (défaut 10 jours)
    python scripts/ops.py supabase sql "SELECT ..."   # SQL brut (Management API — SUPABASE_ACCESS_TOKEN)
    python scripts/ops.py supabase migrate sql/migrate_vX_Y.sql   # applique un fichier SQL (idem)

    python scripts/ops.py vercel projects
    python scripts/ops.py vercel deployments [n]
    python scripts/ops.py vercel env list
    python scripts/ops.py vercel env set KEY VAL [production,preview,development]
    python scripts/ops.py vercel env del KEY
    python scripts/ops.py vercel redeploy             # nouveau déploiement de main

Credentials (dans .env à la racine — gitignoré — ou l'environnement) :
    SUPABASE_URL              défaut https://<SUPABASE_PROJECT_REF>.supabase.co
    SUPABASE_PROJECT_REF      défaut chnyxeyqpdipeogirrpu (cf. .mcp.json)
    SUPABASE_SERVICE_KEY      service_role — lecture/écriture app_secrets, meta, signals (PostgREST)
    SUPABASE_ACCESS_TOKEN     Personal Access Token (supabase.com/dashboard/account/tokens) — SQL brut, migrations
    VERCEL_TOKEN              vercel.com/account/tokens
    VERCEL_PROJECT            nom ou id du projet (défaut : predator) — `vercel projects` pour le trouver
    VERCEL_TEAM_ID            optionnel (compte d'équipe)

Tout est en REST (requests) : aucune dépendance à npm/node/CLI. Les CLIs
officiels (`supabase`, `vercel`) restent utilisables à côté quand ils sont
installés ; ce script couvre les gestes courants du projet.
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
load_dotenv()

REF      = os.environ.get("SUPABASE_PROJECT_REF", "chnyxeyqpdipeogirrpu")
SB_URL   = (os.environ.get("SUPABASE_URL") or f"https://{REF}.supabase.co").rstrip("/")
SB_SRV   = os.environ.get("SUPABASE_SERVICE_KEY", "")
SB_PAT   = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
VC_TOKEN = os.environ.get("VERCEL_TOKEN", "")
VC_PROJ  = os.environ.get("VERCEL_PROJECT", "predator")
VC_TEAM  = os.environ.get("VERCEL_TEAM_ID", "")


def die(msg: str, code: int = 1):
    print(f"ERREUR : {msg}", file=sys.stderr)
    raise SystemExit(code)


def _p(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ── Supabase — PostgREST (service_role) ───────────────────────────────

def _rest(method: str, table: str, *, params=None, body=None, prefer=None):
    if not SB_SRV:
        die("SUPABASE_SERVICE_KEY manquant (PostgREST écriture/lecture RLS)")
    h = {"apikey": SB_SRV, "Authorization": f"Bearer {SB_SRV}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    r = requests.request(method, f"{SB_URL}/rest/v1/{table}", headers=h,
                         params=params, json=body, timeout=30)
    if r.status_code >= 300:
        die(f"PostgREST {method} {table} → HTTP {r.status_code} : {r.text[:400]}")
    return r.json() if r.text else None


def sb_secrets(args):
    sub = args[0] if args else "list"
    if sub == "list":
        rows = _rest("GET", "app_secrets", params={"select": "key,note,updated_at", "order": "key"})
        for r in rows:
            print(f"{r['key']:<22} maj={str(r.get('updated_at') or '?')[:19]}  {r.get('note') or ''}")
        if not rows:
            print("(vide)")
    elif sub == "set" and len(args) >= 3:
        key, val = args[1], args[2]
        _rest("POST", "app_secrets", params={"on_conflict": "key"},
              body={"key": key, "value": val, "note": f"ops.py {datetime.now(timezone.utc):%Y-%m-%d}"},
              prefer="resolution=merge-duplicates")
        print(f"OK — {key} posé (…{val[-4:]})")
    elif sub == "del" and len(args) >= 2:
        _rest("DELETE", "app_secrets", params={"key": f"eq.{args[1]}"})
        print(f"OK — {args[1]} supprimé")
    else:
        die("usage : supabase secrets list | set KEY VAL | del KEY")


def sb_meta(args):
    prefix = args[0] if args else ""
    params = {"select": "key,value,updated_at", "order": "key"}
    if prefix:
        params["key"] = f"like.{prefix}%"
    for r in _rest("GET", "meta", params=params):
        v = str(r.get("value") or "")
        print(f"{r['key']:<32} {v[:70]:<70} {str(r.get('updated_at') or '')[:19]}")


def sb_meta_set(args):
    if len(args) < 2:
        die("usage : supabase meta-set KEY VAL")
    _rest("POST", "meta", params={"on_conflict": "key"},
          body={"key": args[0], "value": args[1], "updated_at": datetime.now(timezone.utc).isoformat()},
          prefer="resolution=merge-duplicates")
    print(f"OK — meta.{args[0]} = {args[1][:60]}")


def sb_signals(args):
    days = int(args[0]) if args else 10
    rows = _rest("GET", "signals", params={"select": "scanned_at", "order": "scanned_at.desc", "limit": "1"})
    last = rows[0]["scanned_at"] if rows else None
    # Agrégat par jour/statut sur N jours (PostgREST ne fait pas de GROUP BY : on compte côté client)
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = _rest("GET", "signals", params={"select": "scanned_at,status,sport",
                                           "scanned_at": f"gte.{since}", "limit": "5000"})
    per_day: dict = {}
    for r in rows:
        d = str(r.get("scanned_at") or "")[:10]
        per_day.setdefault(d, {}).setdefault(r.get("status") or "?", 0)
        per_day[d][r.get("status") or "?"] += 1
    print(f"Dernier signal : {last or 'aucun sur la période'}")
    for d in sorted(per_day):
        print(f"{d}  " + "  ".join(f"{k}={v}" for k, v in sorted(per_day[d].items())))
    if not per_day:
        print(f"(aucun signal depuis {days} jours)")


# ── Supabase — Management API (PAT) ───────────────────────────────────

def sb_sql(query: str):
    if not SB_PAT:
        die("SUPABASE_ACCESS_TOKEN manquant (Management API — SQL brut / migrations)")
    r = requests.post(f"https://api.supabase.com/v1/projects/{REF}/database/query",
                      headers={"Authorization": f"Bearer {SB_PAT}", "Content-Type": "application/json"},
                      json={"query": query}, timeout=60)
    if r.status_code >= 300:
        die(f"Management API → HTTP {r.status_code} : {r.text[:400]}")
    return r.json()


def sb_migrate(path: str):
    if not os.path.exists(path):
        die(f"fichier introuvable : {path}")
    sql = open(path).read()
    print(f"Application de {path} ({len(sql)} octets) sur {REF}…")
    _p(sb_sql(sql))
    print("OK")


# ── Vercel ─────────────────────────────────────────────────────────────

def _vc(method: str, path: str, *, params=None, body=None):
    if not VC_TOKEN:
        die("VERCEL_TOKEN manquant")
    params = dict(params or {})
    if VC_TEAM:
        params["teamId"] = VC_TEAM
    r = requests.request(method, f"https://api.vercel.com{path}",
                         headers={"Authorization": f"Bearer {VC_TOKEN}"},
                         params=params, json=body, timeout=30)
    if r.status_code >= 300:
        die(f"Vercel {method} {path} → HTTP {r.status_code} : {r.text[:400]}")
    return r.json() if r.text else None


def vc_projects():
    for p in _vc("GET", "/v9/projects").get("projects", []):
        link = p.get("link") or {}
        print(f"{p['name']:<24} id={p['id']}  repo={link.get('org', '?')}/{link.get('repo', '?')}  fw={p.get('framework')}")


def vc_deployments(args):
    n = int(args[0]) if args else 5
    proj = _vc("GET", f"/v9/projects/{VC_PROJ}")
    for d in _vc("GET", "/v6/deployments", params={"projectId": proj["id"], "limit": n}).get("deployments", []):
        ts = datetime.fromtimestamp(d["created"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        meta = d.get("meta") or {}
        print(f"{ts}  {d.get('state'):<9} {d.get('target') or 'preview':<10} {d.get('url')}  "
              f"{(meta.get('githubCommitSha') or '')[:7]} {meta.get('githubCommitMessage', '')[:50]}")


def vc_env(args):
    sub = args[0] if args else "list"
    if sub == "list":
        for e in _vc("GET", f"/v9/projects/{VC_PROJ}/env").get("envs", []):
            print(f"{e['key']:<28} {','.join(e.get('target') or []):<34} {e.get('type')}")
    elif sub == "set" and len(args) >= 3:
        targets = args[3].split(",") if len(args) > 3 else ["production", "preview", "development"]
        _vc("POST", f"/v10/projects/{VC_PROJ}/env", params={"upsert": "true"},
            body={"key": args[1], "value": args[2], "type": "encrypted", "target": targets})
        print(f"OK — {args[1]} posé sur {','.join(targets)} (redeploy nécessaire pour prise en compte)")
    elif sub == "del" and len(args) >= 2:
        envs = [e for e in _vc("GET", f"/v9/projects/{VC_PROJ}/env").get("envs", []) if e["key"] == args[1]]
        for e in envs:
            _vc("DELETE", f"/v9/projects/{VC_PROJ}/env/{e['id']}")
        print(f"OK — {len(envs)} entrée(s) {args[1]} supprimée(s)")
    else:
        die("usage : vercel env list | set KEY VAL [targets] | del KEY")


def vc_redeploy():
    proj = _vc("GET", f"/v9/projects/{VC_PROJ}")
    link = proj.get("link") or {}
    if link.get("type") != "github":
        die("projet non lié à GitHub — redeploy via git push ou Deploy Hook")
    d = _vc("POST", "/v13/deployments", body={
        "name": proj["name"], "target": "production",
        "gitSource": {"type": "github", "repoId": link["repoId"], "ref": link.get("productionBranch", "main")},
    })
    print(f"Déploiement lancé : https://{d.get('url')}  (état : {d.get('readyState') or d.get('state')})")


# ── Vue d'ensemble ─────────────────────────────────────────────────────

def sources():
    """Sonde toutes les sources de cotes et dit laquelle peut réellement
    servir. Écrit pour l'incident d'août 2026 : la panne a duré dix jours
    parce que rien ne répondait à « qu'est-ce qui marche, là, maintenant ».

    Aucune sonde ne consomme de crédit facturé : OddsAPI /v4/sports est
    gratuit, api-sports /status aussi, et le LineFeed n'a pas de quota.
    """
    from core.odds_api import candidate_keys, probe_key            # noqa: E402
    from core.api_sports import PROVIDERS, _key_for                # noqa: E402
    from core.ai_search import ai_available                        # noqa: E402

    print("── Tier 1 · The Odds API (sharp Pinnacle + soft 1xbet) ──")
    keys = candidate_keys()
    if not keys:
        print("  aucune clé — `rotate_odds_key.py --add <clé>`")
    for i, k in enumerate(keys, 1):
        ok, detail = probe_key(k)
        print(f"  #{i} …{k[-4:]}  {'VIVANTE' if ok else 'MORTE  '}  {detail}")

    print("── Tier 2 · api-sports.io (clé = pas de filtrage IP) ──")
    for sport, prov in PROVIDERS.items():
        key = _key_for(sport)
        if not key:
            print(f"  {sport:<11} pas de clé ({'/'.join(prov['keys'])})")
            continue
        try:
            r = requests.get(f"https://{prov['host']}/status",
                             headers={"x-apisports-key": key}, timeout=15)
            body = r.json() if r.text else {}
            errs = body.get("errors") or {}
            resp = body.get("response") or {}
            sub  = (resp.get("subscription") or {})
            req  = (resp.get("requests") or {})
            if errs:
                print(f"  {sport:<11} REFUS {str(errs)[:70]}")
            else:
                print(f"  {sport:<11} OK  plan={sub.get('plan', '?')} "
                      f"{req.get('current', '?')}/{req.get('limit_day', '?')} req aujourd'hui")
        except Exception as e:
            print(f"  {sport:<11} ERREUR {e}")

    print("── Tier 1.5 · Matchbook Exchange (sharp, sans clé) ──")
    from core.matchbook import probe as mb_probe, fetch_matchbook_prices   # noqa: E402
    ok, detail = mb_probe()
    print(f"  joignabilité : {'OK' if ok else 'KO'} — {detail}")
    if ok:
        n = len(fetch_matchbook_prices(
            sports=["soccer", "basketball", "baseball", "hockey"], hours_ahead=24))
        print(f"  marchés sharp exploitables dans 24h : {n}")

    print("── Tier 2 · odds-api.io (books soft authentifiés) ──")
    from core.odds_api_io import probe as oai_probe                    # noqa: E402
    ok, detail = oai_probe()
    print(f"  {'OK' if ok else 'KO'} — {detail}")

    print("── Tier 2 · Titan007 (foot, ligues hors Europe) ──")
    from core.titan007 import probe as t7_probe                        # noqa: E402
    ok, detail = t7_probe()
    print(f"  {'OK' if ok else 'KO'} — {detail}")

    print("── Tier 2 bis · LineFeed 1xbet/Melbet/22bet (sans clé → filtré par IP) ──")
    from core.harvester import SOFT_BOOKS                           # noqa: E402
    for book, (tpls, referer) in SOFT_BOOKS.items():
        url = tpls[0].format(sport_id=1)
        try:
            r = requests.get(url, timeout=12, headers={
                "User-Agent": "Mozilla/5.0", "Referer": referer})
            n = len((r.json() or {}).get("Value") or []) if r.status_code == 200 else 0
            print(f"  {book:<8} HTTP {r.status_code}  {n} matchs")
        except Exception as e:
            print(f"  {book:<8} injoignable ({type(e).__name__})")

    print("── Tier 3 · recherche web (Groq/Tavily) ──")
    print(f"  Groq   : {'clé présente' if ai_available() else 'indisponible'}")
    print(f"  Tavily : {'clé présente' if os.environ.get('TAVILY_API_KEY') else 'absente'}")


def doctor():
    print(f"Supabase projet  : {REF}  ({SB_URL})")
    print(f"  SERVICE_KEY    : {'présent' if SB_SRV else 'ABSENT  → secrets/meta/signals indisponibles'}")
    print(f"  ACCESS_TOKEN   : {'présent' if SB_PAT else 'ABSENT  → sql/migrate indisponibles'}")
    print(f"Vercel projet    : {VC_PROJ}{' (team ' + VC_TEAM + ')' if VC_TEAM else ''}")
    print(f"  VERCEL_TOKEN   : {'présent' if VC_TOKEN else 'ABSENT  → deployments/env/redeploy indisponibles'}")
    if SB_SRV:
        try:
            _rest("GET", "meta", params={"select": "key", "limit": "1"})
            print("  PostgREST      : OK (service_role accepté)")
        except SystemExit:
            print("  PostgREST      : ÉCHEC (clé refusée ?)")
    if SB_PAT:
        try:
            sb_sql("select 1 as ok")
            print("  Management API : OK")
        except SystemExit:
            print("  Management API : ÉCHEC (token refusé ?)")
    if VC_TOKEN:
        try:
            _vc("GET", f"/v9/projects/{VC_PROJ}")
            print("  Vercel API     : OK")
        except SystemExit:
            print("  Vercel API     : ÉCHEC (token refusé ou projet introuvable — essayer `vercel projects`)")


def status():
    from core.odds_api import candidate_keys, probe_key  # noqa: E402
    print("── OddsAPI pool ──")
    keys = candidate_keys()
    if not keys:
        print("  aucune clé (ni app_secrets ni env)")
    for i, k in enumerate(keys, 1):
        ok, d = probe_key(k)
        print(f"  #{i} …{k[-4:]}  {'OK   ' if ok else 'MORTE'} {d}")
    if SB_SRV:
        print("── Signaux ──")
        sb_signals(["7"])
        print("── Seuils (meta.threshold_*) ──")
        sb_meta(["threshold_"])
        print("── Horodatages incident (meta.alert_* / harvest_empty_at) ──")
        sb_meta(["alert_"])
        sb_meta(["harvest_"])
    if VC_TOKEN:
        print("── Vercel ──")
        vc_deployments(["3"])


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "doctor":
        doctor()
    elif cmd == "status":
        status()
    elif cmd == "sources":
        sources()
    elif cmd == "supabase":
        sub, a = (rest[0] if rest else ""), rest[1:]
        {"secrets": lambda: sb_secrets(a), "meta": lambda: sb_meta(a),
         "meta-set": lambda: sb_meta_set(a), "signals": lambda: sb_signals(a),
         "sql": lambda: _p(sb_sql(" ".join(a))), "migrate": lambda: sb_migrate(a[0] if a else ""),
         }.get(sub, lambda: die(f"sous-commande supabase inconnue : {sub}"))()
    elif cmd == "vercel":
        sub, a = (rest[0] if rest else ""), rest[1:]
        {"projects": vc_projects, "deployments": lambda: vc_deployments(a),
         "env": lambda: vc_env(a), "redeploy": vc_redeploy,
         }.get(sub, lambda: die(f"sous-commande vercel inconnue : {sub}"))()
    else:
        die(f"commande inconnue : {cmd} (voir --help)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
