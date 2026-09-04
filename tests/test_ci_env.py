"""
tests/test_ci_env.py — les secrets qui atteignent chaque job sont du CODE, testé ici.

Remplace la vérification par regex des blocs `${{ secrets.X }}` des workflows
(tests/test_workflow_secrets.py, refonte 2026-08-26) : il n'y a plus de bloc à
comparer, il y a scripts/ci_env.py et ses pools. Ces tests encodent les
invariants que les workflows portaient en commentaires :
  - tout fournisseur PRODUCTION_SAFE atteint le pool scan (le seul qui
    consomme encore de l'IA — alias CJK) ;
  - AUCUN pool ne transmet une clé Groq ou Tavily (supprimées le 2026-09-02,
    settlement déterministe) ;
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
CLES_PAYANTES = TOUTES_CLES_IA \
    | set(ci_env.ODDS_SOURCES) | set(ci_env.BETFAIR) | set(ci_env.RELAYS)

# Clés SUPPRIMÉES du pipeline le 2026-09-02 (Groq/Tavily) : aucun pool ne
# doit plus jamais les transmettre — les réintroduire serait rebrancher une
# capacité retirée sur décision opérateur.
CLES_SUPPRIMEES = {"GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3",
                   "GROQ_API_KEY_4", "GROQ_API_KEY_5", "TAVILY_API_KEY",
                   # Fournisseurs IA morts retirés le 2026-09-03 (402 / 429 / 401)
                   "CEREBRAS_API_KEY", "CHUTES_API_KEY", "SAMBANOVA_API_KEY",
                   "SCALEWAY_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                   "ZHIPU_API_KEY"}


def _jwt(role):
    p = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{p}.sig"


def _secrets(**extra):
    base = {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "anon",
            "SUPABASE_SERVICE_KEY": _jwt("service_role")}
    base.update(extra)
    return base


# ── Couverture des fournisseurs ───────────────────────────────────────

def test_tout_fournisseur_de_production_atteint_le_pool_scan():
    """Le scan est le SEUL pool qui consomme encore de l'IA (alias CJK,
    analyse) depuis que le settlement est déterministe (2026-09-02)."""
    env = ci_env.env_for("scan", {})
    manquants = sorted(CLES_PRODUCTION - set(env))
    assert not manquants, f"pool scan ne transmet pas {manquants} — capacité morte SANS ERREUR"


def test_chaque_jeton_a_compagnon_voyage_avec_lui():
    """(Cloudflare, seul cas connu, a été retiré le 2026-09-03 ; la garde
    reste pour le prochain fournisseur à identifiant de compte.)"""
    env = ci_env.env_for("scan", {})
    for jeton, compagnons in ci_env.COMPANIONS.items():
        if jeton in env:
            for c in compagnons:
                assert c in env, f"pool scan passe {jeton} sans {c}"


@pytest.mark.parametrize("pool", sorted(ci_env.POOLS))
def test_aucun_pool_ne_transmet_groq_ou_tavily(pool):
    """Gardien de la suppression du 2026-09-02 : Groq et Tavily sont sortis
    du pipeline (settlement déterministe via core/score_sources). Une clé qui
    réapparaît ici est une capacité rebranchée en douce."""
    assert not (set(ci_env.env_for(pool, {})) & CLES_SUPPRIMEES), pool
    assert not (ci_env.secret_names_for(pool) & CLES_SUPPRIMEES), pool


def test_liste_ia_derivee_du_registre_comme_ops_py():
    assert set(ci_env.AI_KEYS) == TOUTES_CLES_IA
    ops = _module("ops")
    assert TOUTES_CLES_IA <= set(ops._AI_SECRETS)



def test_le_settlement_porte_les_cles_de_resultats():
    """Le score final vient d'API structurées (MLB statsapi et ESPN sans clé,
    TheSportsDB), pas d'un LLM — c'est l'UNIQUE chemin
    depuis la suppression de la recherche web (2026-09-02). Une capacité non
    câblée meurt SANS ERREUR ; c'est tout l'objet de ce fichier."""
    env = ci_env.env_for("settlement", {})
    manquants = sorted(set(ci_env.RESULTS_SOURCES) - set(env))
    assert not manquants, f"le pool settlement ne transmet pas {manquants}"

def test_le_pool_scan_porte_les_relais_des_sources_filtrees_par_ip():
    """Les sources filtrées par IP depuis les runners (2026-08-26, odds500
    à l'époque ; les sources de scores aujourd'hui) passent par core/net.py :
    sans FREE_SOURCES_PROXY/RELAY/_TOKEN dans l'env, le module est INERTE et
    rien ne change — sans la moindre erreur. C'est exactement le mode de
    panne que ce fichier existe pour interdire, et CLAUDE.md en fait un
    invariant."""
    env = ci_env.env_for("scan", {})
    manquants = sorted(set(ci_env.RELAYS) - set(env))
    assert not manquants, f"le pool scan ne transmet pas {manquants} — la sortie réseau resterait inerte"


# ── Cloisonnements ───────────────────────────────────────────────────

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


def test_toute_exigence_de_pool_est_un_nom_denv_atteignable():
    """Un `required` doit être un nom d'ENV du pool, sinon il est
    inatteignable — vécu le 2026-08-26 (run 33008750419) quand le préflight
    du settlement exigeait GROQ_API_KEY_3, un nom que son propre bloc généré
    avait fait disparaître."""
    for pool in ci_env.POOLS:
        env = ci_env.env_for(pool, _secrets())
        manquants = [k for k in ci_env.POOLS[pool]["required"] if k not in env]
        assert not manquants, f"pool {pool} exige {manquants}, absent(s) de son propre env"


def test_preflight_odds_api_key_nest_plus_requise():
    # OddsAPI obsolète depuis le 2026-08-26 : la garde fermée d'origine aurait
    # planté tous les scans le jour où le secret est retiré.
    assert not _erreurs("scan", _secrets(ODDS_API_KEY=""))


def test_aucune_cle_api_sports_dans_les_pools():
    """2026-09-03 : api-sports retirée (deux comptes gratuits suspendus). Une
    clé encore transmise ferait croire à une capacité qui n'existe plus."""
    for pool in ci_env.POOLS:
        for k in ci_env.env_for(pool, {}):
            assert not k.startswith(("API_SPORTS", "API_FOOTBALL", "API_BASKETBALL",
                                     "API_BASEBALL")), (pool, k)


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


def test_flag_manuel_promeut_reprice_seulement():
    """Bouton Scan = scan STANDARD complet : un tick reprice (horaire, sans
    scan) qui trouve le flag devient un standard ; un standard l'est déjà."""
    assert ci_mode.promote("reprice", True) == "standard"
    assert ci_mode.promote("standard", True) == "standard"
    assert ci_mode.promote("reprice", False) == "reprice"


def test_env_de_mode():
    assert ci_mode.env_for("standard") == {"ODDS_API": "1"}
    assert ci_mode.env_for("standard", "12") == {"ODDS_API": "1", "HOURS_AHEAD": "12"}
    assert ci_mode.env_for("reprice") == {"REPRICE": "1"}


def test_le_tier_1_est_rallume_par_le_workflow_pas_par_le_module():
    """Rallumage OddsAPI du 2026-09-01 : le flag vit dans MODE_ENV, pour
    `standard` seulement. Le défaut du module reste 0
    (tests/test_oddsapi_obsolete.py). REPRICE reste sans, par construction."""
    assert ci_mode.TIER1_ENV == {"ODDS_API": "1"}
    assert ci_mode.env_for("standard").get("ODDS_API") == "1"
    assert "ODDS_API" not in ci_mode.env_for("reprice")


def test_il_ny_a_que_deux_modes():
    """2026-09-03, décision opérateur : golden, deep et guerrilla sont
    supprimés. Un mode réintroduit sans cron (ou un cron sans mode) échoue
    ici — la table cron → mode est la seule source de vérité (règle n°6)."""
    assert set(ci_mode.MODES) == {"standard", "reprice"}
    assert set(ci_mode.CRON_MODES.values()) == set(ci_mode.MODES)
    assert set(ci_mode.MODE_ENV) == set(ci_mode.MODES)


def test_le_dispatch_de_scan_yml_noffre_que_les_modes_connus():
    wf = yaml.safe_load((RACINE / ".github" / "workflows" / "scan.yml").read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)
    options = on["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert list(options) == list(ci_mode.MODES)


def test_reprice_vient_de_mode_env_pas_du_yaml():
    """REPRICE=1 est posé par ci_scan_mode (table unique), plus par le step :
    un flag de mode écrit à la main dans le YAML serait une seconde table."""
    assert ci_mode.MODE_ENV["reprice"] == {"REPRICE": "1"}
    texte = (RACINE / ".github" / "workflows" / "scan.yml").read_text(encoding="utf-8")
    code = "\n".join(l for l in texte.splitlines() if not l.strip().startswith("#"))
    assert "REPRICE: '1'" not in code and "GOLDEN_HOUR" not in code


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
    for pool, indent, amorcage, ecrit in ci_env._blocs_de(texte):
        assert ecrit == ci_env.render(pool, indent, amorcage), (
            f"{wf.name} : le bloc du pool `{pool}`"
            f"{' (amorçage)' if amorcage else ''} a divergé de sa source — "
            "lancer `python scripts/ci_env.py --write`")


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_aucun_secret_nommé_hors_dun_bloc_genere(wf):
    """Un secret ajouté à la main échappe au générateur : c'est la divergence
    que toute cette mécanique existe pour rendre impossible."""
    texte = wf.read_text(encoding="utf-8")
    dedans = set()
    for _pool, _i, _amorcage, bloc in ci_env._blocs_de(texte):
        dedans |= set(re.findall(r"secrets\.([A-Za-z0-9_]+)", bloc))
    dehors = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", texte)) - dedans - SECRETS_HORS_BLOC
    assert not dehors, (f"{wf.name} nomme {sorted(dehors)} hors d'un bloc généré — "
                        "l'ajouter au pool dans scripts/ci_env.py puis `--write`")


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_aucun_workflow_nutilise_le_contexte_inputs_nu(wf):
    """`inputs.x` n'existe qu'en workflow_dispatch/workflow_call.

    Un `if:` de job est évalué AVANT la création des jobs : une référence
    irrésolvable n'y donne pas un job rouge, elle donne un workflow qui
    n'existe pas. `github.event.inputs.*` est un simple accès au payload —
    nul hors dispatch, identique pendant un dispatch.

    (Ce n'était PAS la cause de la panne du 2026-08-26 — c'était
    `toJSON(secrets)`, cf. le test suivant. Mais la forme nue reste un piège,
    et cette garde ne coûte rien.)"""
    fautifs = [l.strip()[:80] for l in wf.read_text(encoding="utf-8").splitlines()
               if re.search(r"(?<!event\.)\binputs\.[a-z_]+", l.split("#", 1)[0])]
    assert not fautifs, (f"{wf.name} utilise le contexte `inputs` nu : {fautifs} — "
                          "écrire `github.event.inputs.…`")

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
    blocs = {pool: b for pool, _i, amorcage, b in ci_env._blocs_de(texte)
             if not amorcage}
    assert "reprice" in blocs, "scan.yml n'a plus de bloc `reprice`"
    noms = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", blocs["reprice"]))
    assert not (noms & CLES_PAYANTES), f"REPRICE reçoit des clés payantes : {noms & CLES_PAYANTES}"


def test_aucun_workflow_ne_nomme_une_cle_supprimee():
    """Groq et Tavily sont sortis des workflows le 2026-09-02 avec la
    suppression de la recherche web. Un secret réapparu dans un YAML est une
    capacité rebranchée en douce."""
    for wf in WORKFLOWS:
        noms = set(re.findall(r"secrets\.([A-Za-z0-9_]+)",
                              wf.read_text(encoding="utf-8")))
        assert not (noms & CLES_SUPPRIMEES), f"{wf.name} nomme {noms & CLES_SUPPRIMEES}"


def test_write_est_idempotent(tmp_path):
    """`--write` relancé sur un dépôt à jour ne change rien : sinon le test
    ci-dessus deviendrait un bruit permanent."""
    for wf in WORKFLOWS:
        texte = wf.read_text(encoding="utf-8")
        assert ci_env.reecrire(texte) == texte, f"{wf.name} n'est pas à jour"


# ── C5 — un step qui prépare le runner n'a pas à voir les clés ───────────

class TestAmorcageSupabaseSeul:
    """L'action composite `.github/actions/setup` recevait le pool ENTIER. Or
    elle ne fait pas que le préflight : elle restaure un cache et lance
    `pip install -r requirements.txt`. Toutes les clés IA, de cotes, de
    Telegram et de Betfair étaient donc dans l'environnement d'un `pip`, qui
    exécute le code de dizaines de paquets tiers.

    C'est le reproche exact que CLAUDE.md fait au dump `toJSON(secrets)` —
    « lisible par chaque step du job, `actions/checkout` et `pip install`
    compris » — sous une autre forme, et celle-là ne déclenchait aucune
    détection de GitHub.
    """

    @pytest.mark.parametrize("pool", sorted(ci_env.POOLS))
    def test_lamorcage_ne_contient_que_du_supabase(self, pool):
        assert all(k.startswith("SUPABASE_") for k in ci_env.bootstrap_keys(pool))

    @pytest.mark.parametrize("pool", sorted(ci_env.POOLS))
    def test_lamorcage_est_DERIVE_du_pool_jamais_liste(self, pool):
        """Une liste tenue à la main finirait par diverger — c'est la panne
        la plus fréquente de ce dépôt."""
        attendu = tuple(k for k in ci_env.POOLS[pool]["passthrough"]
                        if k.startswith("SUPABASE_"))
        assert ci_env.bootstrap_keys(pool) == attendu

    def test_readonly_namorce_pas_avec_un_jeton_decriture(self):
        """Conséquence gratuite de la dérivation : le pool `readonly` n'a pas
        de SUPABASE_SERVICE_KEY, son amorçage n'en a donc pas non plus.
        L'invariant « readonly ne détient aucun jeton d'écriture » tient sans
        qu'on ait eu à y penser."""
        assert "SUPABASE_SERVICE_KEY" not in ci_env.bootstrap_keys("readonly")
        assert "SUPABASE_SERVICE_KEY" in ci_env.bootstrap_keys("scan")

    @pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
    def test_les_steps_de_preparation_ne_voient_que_supabase(self, wf):
        """Le garde qui compte : dans le YAML RÉEL, le bloc qui suit
        `uses: ./.github/actions/setup` ou « Résoudre le mode » ne doit nommer
        que des secrets Supabase."""
        lignes = wf.read_text(encoding="utf-8").split("\n")
        motifs = ("uses: ./.github/actions/setup", "name: Résoudre le mode")
        for i, l in enumerate(lignes):
            if not any(m in l for m in motifs):
                continue
            for j in range(i, min(i + 15, len(lignes))):
                if "# ▼ GÉNÉRÉ par" not in lignes[j]:
                    continue
                assert ci_env.SUFFIXE_AMORCAGE in lignes[j], (
                    f"{wf.name} ligne {j + 1} : un step de préparation porte "
                    "le pool ENTIER — `pip install` verrait toutes les clés")
                fin = next(k for k in range(j, len(lignes))
                           if "# ▲ fin du bloc" in lignes[k])
                noms = re.findall(r"secrets\.([A-Za-z0-9_]+)",
                                  "\n".join(lignes[j:fin + 1]))
                assert noms and all(n.startswith("SUPABASE_") for n in noms), \
                    f"{wf.name} : amorçage non-Supabase → {noms}"
                break


class TestLePreflightCompletTourneOuSontLesCles:
    """Réduire l'amorçage sans déplacer le préflight l'aurait rendu AVEUGLE :
    il aurait signalé « GROQ_API_KEY absente » à chaque run, sur un secret
    pourtant présent dans le step qui travaille. Un préflight qui crie au loup
    à chaque exécution n'est plus lu — et c'est ainsi qu'on perd une vraie
    alerte."""

    def test_lamorcage_ne_verifie_que_les_fondations(self):
        """Aucun avertissement sur des secrets que le step ne reçoit PAS."""
        secrets = {"SUPABASE_URL": "u", "SUPABASE_KEY": "k",
                   "SUPABASE_SERVICE_KEY": "sb_secret_x"}
        constats = ci_env.check("scan", secrets, amorcage=True)
        assert not [m for lvl, m in constats if lvl in ("warning", "error")], constats

    def test_lamorcage_attrape_quand_meme_une_mauvaise_cle_de_service(self):
        """Ce qu'il doit garder : la vérification qui a coûté 17 h le
        2026-07-07."""
        secrets = {"SUPABASE_URL": "u", "SUPABASE_KEY": "k",
                   "SUPABASE_SERVICE_KEY": "sb_publishable_x"}
        constats = ci_env.check("scan", secrets, amorcage=True)
        assert any(lvl == "error" and "service_role" in m for lvl, m in constats)

    def test_lamorcage_attrape_une_cle_supabase_manquante(self):
        constats = ci_env.check("scan", {}, amorcage=True)
        assert any(lvl == "error" for lvl, _ in constats)

    def test_le_preflight_COMPLET_lui_avertit_encore(self, monkeypatch):
        """Sans quoi le déplacement aurait perdu les alertes de sources de
        repli. Plus aucun pool n'en déclare depuis le retrait d'api-sports
        (2026-09-03) : on vérifie le MÉCANISME sur un pool augmenté."""
        secrets = {"SUPABASE_URL": "u", "SUPABASE_KEY": "k",
                   "SUPABASE_SERVICE_KEY": "sb_secret_x"}
        assert not [m for lvl, m in ci_env.check("scan", secrets, amorcage=False)
                    if lvl in ("warning", "error")]
        monkeypatch.setitem(ci_env.POOLS, "scan",
                            {**ci_env.POOLS["scan"], "warn_missing": ("REPLI_KEY",)})
        assert any(lvl == "warning" and "REPLI_KEY" in m
                   for lvl, m in ci_env.check("scan", secrets, amorcage=False))
        assert not [m for lvl, m in ci_env.check("scan", secrets, amorcage=True)
                    if lvl in ("warning", "error")], "l'amorçage reste aveugle aux replis"

    @pytest.mark.parametrize("wf,pool", [("scan.yml", "scan"),
                                         ("audit.yml", "settlement")])
    def test_le_step_de_travail_lance_le_preflight_complet(self, wf, pool):
        """Seuls ces deux pools portent des contrôles au-delà des fondations
        (empreinte Groq, pool Groq, sources de repli). Ils doivent donc les
        exécuter là où les clés sont réellement présentes."""
        texte = (RACINE / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert f"ci_env.py --pool {pool} --check" in texte, \
            f"{wf} ne lance plus le préflight complet du pool `{pool}`"
        assert f"--pool {pool} --check --amorcage" not in texte

    def test_seuls_scan_et_settlement_ont_des_controles_au_dela_des_fondations(self):
        """Si un autre pool en gagnait un, il faudrait lui aussi déplacer son
        préflight — ce test le signalerait."""
        riches = {p for p, spec in ci_env.POOLS.items()
                  if spec.get("warn_missing")}
        # Vide depuis le retrait d'api-sports (2026-09-03) : la seule source
        # de repli déclarée était API_FOOTBALL_KEY. L'invariant reste : rien
        # en dehors de scan/settlement.
        assert riches <= {"scan", "settlement"}, riches


class TestLactionComposite:
    def test_le_preflight_de_lation_est_en_mode_amorcage(self):
        action = (RACINE / ".github" / "actions" / "setup" / "action.yml").read_text(
            encoding="utf-8")
        assert "--check --amorcage" in action, \
            "l'action lancerait le préflight complet sans en avoir les clés"
