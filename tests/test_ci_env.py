"""
tests/test_ci_env.py — les secrets qui atteignent chaque job sont du CODE, testé ici.

Remplace la vérification par regex des blocs `${{ secrets.X }}` des workflows
(tests/test_workflow_secrets.py, refonte 2026-08-26) : il n'y a plus de bloc à
comparer, il y a scripts/ci_env.py et ses pools. Ces tests encodent les
invariants que les workflows portaient en commentaires :
  - tout fournisseur PRODUCTION_SAFE atteint scan / closing / settlement ;
  - GROQ_API_KEY_3 n'atteint QUE le settlement, et sous le nom GROQ_API_KEY ;
  - REPRICE ne voit aucune clé payante ; readonly ne voit aucune clé d'écriture ;
  - les sources filtrées par IP gardent leur relais dans le pool scan ;
  - le préflight échoue fort sur les pannes vécues (clé anon dans
    SUPABASE_SERVICE_KEY, secret absent, _3 = clé de scan) ;
  - la table cron → mode de scan est exactement l'ensemble des crons de scan.yml.
"""
import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from core.ai_router import PRODUCTION_SAFE, REGISTRY

RACINE = Path(__file__).resolve().parent.parent


def _module(nom):
    chemin = RACINE / "scripts" / f"{nom}.py"
    spec = importlib.util.spec_from_file_location(f"_{nom}_sous_test", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ci_env = _module("ci_env")
ci_mode = _module("ci_scan_mode")

CLES_PRODUCTION = {p.env_key for p in PRODUCTION_SAFE}
TOUTES_CLES_IA = {p.env_key for p in REGISTRY}
CLES_PAYANTES = TOUTES_CLES_IA | set(ci_env.GROQ_SCAN) | {ci_env.GROQ_SETTLEMENT_SOURCE} \
    | set(ci_env.SEARCH) | set(ci_env.ODDS_SOURCES) | set(ci_env.BETFAIR) | set(ci_env.RELAYS)


def _jwt(role):
    p = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{p}.sig"


def _secrets(**extra):
    base = {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "anon",
            "SUPABASE_SERVICE_KEY": _jwt("service_role"),
            "GROQ_API_KEY": "g1", "GROQ_API_KEY_2": "g2", "GROQ_API_KEY_3": "g3",
            "GROQ_API_KEY_4": "g4", "GROQ_API_KEY_5": "g5", "TAVILY_API_KEY": "t",
            "API_FOOTBALL_KEY": "f"}
    base.update(extra)
    return base


# ── Couverture des fournisseurs ───────────────────────────────────────

@pytest.mark.parametrize("pool", ["scan", "closing", "settlement"])
def test_tout_fournisseur_de_production_atteint_les_pools_ia(pool):
    env = ci_env.env_for(pool, {})
    manquants = sorted(CLES_PRODUCTION - set(env))
    assert not manquants, f"pool {pool} ne transmet pas {manquants} — capacité morte SANS ERREUR"


@pytest.mark.parametrize("pool", ["scan", "closing", "settlement"])
def test_cloudflare_a_son_identifiant_de_compte(pool):
    env = ci_env.env_for(pool, {})
    for jeton, compagnons in ci_env.COMPANIONS.items():
        if jeton in env:
            for c in compagnons:
                assert c in env, f"pool {pool} passe {jeton} sans {c}"


def test_liste_ia_derivee_du_registre_comme_ops_py():
    assert set(ci_env.AI_KEYS) == TOUTES_CLES_IA
    ops = _module("ops")
    assert TOUTES_CLES_IA <= set(ops._AI_SECRETS)


def test_le_pool_scan_porte_les_relais_des_sources_filtrees_par_ip():
    """odds500 et 7M sont filtrées par IP depuis les runners (2026-08-26) : sans
    FREE_SOURCES_RELAY/_TOKEN, core/net.py est INERTE et rien ne change — sans
    la moindre erreur. C'est exactement le mode de panne que ce fichier existe
    pour interdire, et CLAUDE.md en fait un invariant."""
    env = ci_env.env_for("scan", {})
    manquants = sorted(set(ci_env.RELAYS) - set(env))
    assert not manquants, f"le pool scan ne transmet pas {manquants} — odds500/7M resteraient muettes"


# ── Cloisonnements ───────────────────────────────────────────────────

def test_settlement_voit_groq_3_sous_le_nom_groq_et_rien_dautre():
    env = ci_env.env_for("settlement", _secrets())
    assert env["GROQ_API_KEY"] == "g3"
    assert not any(k.startswith("GROQ_API_KEY_") for k in env), \
        "le settlement ne doit voir aucune clé Groq numérotée"
    assert "g1" not in env.values() and "g2" not in env.values()


@pytest.mark.parametrize("pool", ["scan", "closing"])
def test_les_scans_ne_voient_jamais_groq_3(pool):
    assert ci_env.GROQ_SETTLEMENT_SOURCE not in ci_env.secret_names_for(pool)
    assert "g3" not in ci_env.env_for(pool, _secrets()).values()


def test_reprice_ne_voit_aucune_cle_payante():
    env = ci_env.env_for("reprice", _secrets())
    assert not (set(env) & CLES_PAYANTES), f"REPRICE reçoit des clés payantes : {set(env) & CLES_PAYANTES}"
    assert set(env) <= set(ci_env.SUPABASE_RW) | set(ci_env.TELEGRAM)


@pytest.mark.parametrize("pool", ["readonly"])
def test_readonly_ne_detient_aucun_jeton_decriture(pool):
    env = ci_env.env_for(pool, _secrets())
    assert "SUPABASE_SERVICE_KEY" not in env
    assert not (set(env) & CLES_PAYANTES)


def test_secrets_json_nest_jamais_transmis_au_process():
    # env_for ne connaît pas SECRETS_JSON ; main() le retire de os.environ
    # avant execvpe — vérifié ici sur le contrat de env_for, et par lecture
    # du code (un test d'exec remplacerait le process pytest).
    for pool in ci_env.POOLS:
        assert "SECRETS_JSON" not in ci_env.env_for(pool, {"SECRETS_JSON": "x"})


# ── Préflight : les pannes vécues doivent échouer FORT ───────────────

def _erreurs(pool, secrets):
    return [m for lvl, m in ci_env.check(pool, secrets) if lvl == "error"]


def test_preflight_ok_sur_secrets_sains():
    assert not _erreurs("scan", _secrets())
    assert not _erreurs("settlement", _secrets())
    assert not _erreurs("readonly", {"SUPABASE_URL": "u", "SUPABASE_KEY": "k"})


def test_preflight_secret_manquant():
    s = _secrets(); del s["SUPABASE_URL"]
    assert any("SUPABASE_URL" in e for e in _erreurs("scan", s))


@pytest.mark.parametrize("cle,role", [(_jwt("anon"), "anon"), ("sb_publishable_x", "anon"), ("zzz", "?")])
def test_preflight_refuse_une_cle_qui_nest_pas_service_role(cle, role):
    # Incident 2026-07-07 : la clé anon s'authentifie puis échoue chaque écriture en RLS 42501.
    errs = _erreurs("scan", _secrets(SUPABASE_SERVICE_KEY=cle))
    assert errs and f"role='{role}'" in errs[0]


def test_preflight_accepte_les_deux_formats_service_role():
    assert not _erreurs("scan", _secrets(SUPABASE_SERVICE_KEY="sb_secret_abc"))
    assert not _erreurs("scan", _secrets(SUPABASE_SERVICE_KEY=_jwt("service_role")))


def test_preflight_settlement_exige_groq_3_distincte():
    s = _secrets(); del s["GROQ_API_KEY_3"]
    assert any("GROQ_API_KEY_3" in e for e in _erreurs("settlement", s))
    assert any("identique" in e for e in _erreurs("settlement", _secrets(GROQ_API_KEY_3="g1")))


def test_preflight_odds_api_key_nest_plus_requise():
    # OddsAPI obsolète depuis le 2026-08-26 : la garde fermée d'origine aurait
    # planté tous les scans le jour où le secret est retiré.
    assert not _erreurs("scan", _secrets(ODDS_API_KEY=""))


def test_preflight_avertit_sur_sources_de_repli_absentes():
    s = _secrets(); del s["API_FOOTBALL_KEY"]
    assert any(lvl == "warning" and "API_FOOTBALL_KEY" in m for lvl, m in ci_env.check("scan", s))


# ── Cron → mode ───────────────────────────────────────────────────────

def _crons_de(nom):
    wf = yaml.safe_load((RACINE / ".github" / "workflows" / nom).read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)
    return {s["cron"] for s in on.get("schedule") or []}


def test_la_table_cron_mode_est_exactement_les_crons_de_scan_yml():
    assert set(ci_mode.CRON_MODES) == _crons_de("scan.yml"), \
        "un cron de scan.yml sans ligne dans CRON_MODES ferait échouer chaque run à ce tick"


def test_chaque_cron_mene_a_un_mode_connu():
    for cron, mode in ci_mode.CRON_MODES.items():
        assert ci_mode.resolve("schedule", cron, "") == mode
        assert mode in ci_mode.MODE_ENV


def test_cron_inconnu_echoue_fort():
    with pytest.raises(ValueError):
        ci_mode.resolve("schedule", "9 9 * * *", "")


def test_dispatch_prend_le_mode_demande():
    for m in ci_mode.MODES:
        assert ci_mode.resolve("workflow_dispatch", "", m) == m
    with pytest.raises(ValueError):
        ci_mode.resolve("workflow_dispatch", "", "turbo")


def test_flag_manuel_promeut_golden_seulement():
    assert ci_mode.promote("golden", True) == "guerrilla"
    assert ci_mode.promote("deep", True) == "deep"
    assert ci_mode.promote("golden", False) == "golden"


def test_env_de_mode():
    assert ci_mode.env_for("golden") == {"GOLDEN_HOUR": "1"}
    assert ci_mode.env_for("deep") == {"DEEP_SCAN": "1", "HOURS_AHEAD": "24"}
    assert ci_mode.env_for("guerrilla")["GUERRILLA"] == "1"
    assert ci_mode.env_for("standard", "12") == {"HOURS_AHEAD": "12"}
    assert ci_mode.env_for("reprice") == {}


def test_guerrilla_ne_fige_pas_son_horizon_dans_le_workflow():
    """Les 48 h de guerrilla viennent de run_engine.py, pas d'une variable :
    les poser ici créerait une seconde source de vérité pour la même valeur."""
    assert "HOURS_AHEAD" not in ci_mode.MODE_ENV["guerrilla"]


def test_closing_line_cadence_alignee_sur_refresh_min():
    """Un tick plus fréquent que CLOSING_LINE_REFRESH_MIN est un no-op garanti."""
    from core.constants import CLOSING_LINE_REFRESH_MIN
    (cron,) = _crons_de("closing_line.yml")
    minutes = [int(m) for m in cron.split()[0].split(",")]
    ecarts = [b - a for a, b in zip(minutes, minutes[1:])] + [60 - minutes[-1] + minutes[0]]
    assert min(ecarts) >= CLOSING_LINE_REFRESH_MIN, \
        f"closing_line.yml tire toutes les {min(ecarts)} min, refresh à {CLOSING_LINE_REFRESH_MIN}"


# ── Les blocs de secrets sont GÉNÉRÉS, et vérifiés à chaque exécution ──

WORKFLOWS = sorted((RACINE / ".github" / "workflows").glob("*.yml"))
# `production` (environnement GitHub) — jamais dans un bloc généré.
SECRETS_HORS_BLOC = {"VERCEL_TOKEN", "VERCEL_ORG_ID", "VERCEL_PROJECT_ID"}


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_chaque_bloc_genere_est_conforme_a_son_pool(wf):
    """Les workflows RÉÉCRIVENT les secrets, mais personne ne les tient à la main.

    La première version (2026-08-26) exposait `SECRETS_JSON: ${{ toJSON(secrets) }}`
    et filtrait à l'exécution. GitHub REFUSE un workflow qui fait ça : « GitHub
    detected that this workflow file may be malicious. It will not run until
    someone with write access approves it. » — conclusion `action_required`,
    ZÉRO job, aucun log, sur tout événement. Cinq des six workflows sont restés
    muets ainsi. La détection a raison : verser tous les secrets dans une
    variable est la signature d'une exfiltration, et cette variable était
    lisible par chaque step du job.

    Les blocs sont donc écrits — mais générés par `ci_env.py --write` depuis les
    pools, eux-mêmes dérivés du registre IA. Ce test est la moitié « un test
    compare la copie à sa source » de la parade de CLAUDE.md.
    """
    texte = wf.read_text(encoding="utf-8")
    for pool, indent, ecrit in ci_env._blocs_de(texte):
        assert ecrit == ci_env.render(pool, indent), (
            f"{wf.name} : le bloc du pool `{pool}` a divergé de sa source — "
            "lancer `python scripts/ci_env.py --write`")


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_aucun_secret_nommé_hors_dun_bloc_genere(wf):
    """Un secret ajouté à la main échappe au générateur : c'est la divergence
    que toute cette mécanique existe pour rendre impossible."""
    texte = wf.read_text(encoding="utf-8")
    dedans = set()
    for _pool, _i, bloc in ci_env._blocs_de(texte):
        dedans |= set(re.findall(r"secrets\.([A-Za-z0-9_]+)", bloc))
    dehors = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", texte)) - dedans - SECRETS_HORS_BLOC
    assert not dehors, (f"{wf.name} nomme {sorted(dehors)} hors d'un bloc généré — "
                        "l'ajouter au pool dans scripts/ci_env.py puis `--write`")


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_aucun_workflow_ne_fabrique_un_dump_de_secrets(wf):
    """`toJSON(secrets)` fait refuser le workflow par GitHub — et à raison.
    Ne jamais chercher à contourner cette détection : ce serait évader un
    contrôle de sécurité pour rétablir une pratique réellement dangereuse."""
    texte = wf.read_text(encoding="utf-8")
    fautives = [l.strip()[:70] for l in texte.splitlines()
                if "toJSON" in l.split("#", 1)[0] and "secrets" in l.split("#", 1)[0]]
    assert not fautives, f"{wf.name} fabrique un dump de secrets : {fautives}"


def test_le_step_reprice_ne_peut_mecaniquement_rien_depenser():
    """Le bloc du step REPRICE ne contient que Supabase et Telegram : la
    garantie n'est plus un filtrage à l'exécution mais ce que le YAML transmet.
    C'est plus fort, et c'est vérifiable en lisant le fichier."""
    texte = (RACINE / ".github" / "workflows" / "scan.yml").read_text(encoding="utf-8")
    blocs = {pool: b for pool, _i, b in ci_env._blocs_de(texte)}
    assert "reprice" in blocs, "scan.yml n'a plus de bloc `reprice`"
    noms = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", blocs["reprice"]))
    assert not (noms & CLES_PAYANTES), f"REPRICE reçoit des clés payantes : {noms & CLES_PAYANTES}"


def test_le_settlement_ne_recoit_que_la_cle_groq_de_reserve():
    texte = (RACINE / ".github" / "workflows" / "audit.yml").read_text(encoding="utf-8")
    blocs = {pool: b for pool, _i, b in ci_env._blocs_de(texte)}
    groq = re.findall(r"GROQ_API_KEY: \$\{\{ secrets\.([A-Za-z0-9_]+)", blocs["settlement"])
    assert groq == [ci_env.GROQ_SETTLEMENT_SOURCE], groq
    assert "secrets.GROQ_API_KEY " not in blocs["settlement"]


def test_write_est_idempotent(tmp_path):
    """`--write` relancé sur un dépôt à jour ne change rien : sinon le test
    ci-dessus deviendrait un bruit permanent."""
    for wf in WORKFLOWS:
        texte = wf.read_text(encoding="utf-8")
        assert ci_env.reecrire(texte) == texte, f"{wf.name} n'est pas à jour"
