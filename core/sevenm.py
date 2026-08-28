"""
core/sevenm.py — 7M (7msport.com / 7mdt.com) : source de NOMS, pas de cotes.

POURQUOI ELLE N'EST PAS UNE SOURCE DE COTES
--------------------------------------------
7M était retenue au cahier des charges comme comparateur de redondance
(« jamais deux sources critiques sur le même hôte »). Vérification live du
2026-08-22 depuis une IP datacenter : les hôtes répondent bien (HTTP 200 sur
www.7msport.com, data.7msport.com, px-analyse.7mdt.com), mais **aucun endpoint
de cotes gratuit n'existe** — `gameoddsway_en.js` ne contient pas de prix (ce
sont des statistiques historiques de résultats par book), et toutes les
variantes d'endpoints de cotes essayées répondent 500 :

    px-analyse.7mdt.com/{id}/data/gameodds_en.js   HTTP 500
    px-analyse.7mdt.com/{id}/data/gameoz_en.js     HTTP 500   (欧赔)
    px-analyse.7mdt.com/{id}/data/gameyp_en.js     HTTP 500   (亚盘)
    px-analyse.7mdt.com/{id}/data/gamedx_en.js     HTTP 500   (大小球)

La redondance de cotes attendue de 7M n'existe donc pas, et le rapport le dit.
Mais l'exploration a trouvé mieux pour un autre problème : `gameinfo_en.js`
publie le MÊME calendrier que odds.500.com **en anglais**, avec identifiants
numériques d'équipe et horodatage epoch :

    {"time":"1787407200000","taid":"649266","taname":"Broadfields United",
     "tbid":"651103","tbname":"Corinthian FC","mname":"England FA Cup"}

C'est exactement ce qui manquait au dictionnaire d'alias. 7M est donc intégrée
avec le rôle `names` : elle traduit gratuitement ce que l'IA aurait facturé.
Apparier 500.com et 7M par (coup d'envoi ± 15 min, ligue, structure) donne la
correspondance 鹿岛鹿角 → Kashima Antlers sans qu'aucun modèle n'ait à deviner
quoi que ce soit — et l'appariement, lui, ne lit aucun nom.

STATUT JURIDIQUE — À CONNAÎTRE
-------------------------------
Meilleur que celui de 500.com et de titan007, et c'est vérifié :
  - `www.7msport.com/robots.txt` = 200 et ne contient QU'une ligne `Sitemap:` —
    aucun Disallow. Les identifiants de matchs utilisés ici viennent de ce
    sitemap, c'est-à-dire du chemin que le site publie POUR être moissonné ;
  - `data.7msport.com/robots.txt` interdit les variantes de langue
    (`/*_gb.shtml`, `/*_kr.shtml`, `/*_jp.shtml`…). On n'utilise que la
    variante ANGLAISE `/goaldata/en/`, hors de ces motifs ;
  - `px-analyse.7mdt.com/robots.txt` porte un bloc Cloudflare
    « Content-Signal » : `User-agent: * / Content-Signal:
    search=yes,ai-train=no,use=reference / Allow: /`, puis un Disallow nommé
    pour les crawlers d'IA (ClaudeBot, GPTBot, CCBot, Google-Extended…).
    Ce pipeline tombe sous `User-agent: *` → autorisé, et son usage est bien
    `use=reference` : les noms servent de référence, rien n'entraîne de
    modèle. Le User-Agent est honnête et ne se fait passer pour aucun des
    agents nommés — c'est la condition pour que ce raisonnement tienne.

BUDGET
------
Pas de flux calendrier en masse : le sitemap donne les identifiants, puis
c'est UN appel par match. C'est cher pour des cotes, mais négligeable pour un
dictionnaire — un alias appris ne périme jamais. Le budget journalier est donc
volontairement bas : le dictionnaire se remplit sur plusieurs jours et n'a
plus jamais besoin d'être rempli.
"""
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from core import daily_quota, net
from core.source_adapter import Fixture, SourceSpec

log = logging.getLogger("PREDATOR.sevenm")

SITEMAP_URL  = "https://www.7msport.com/sitemap/soccer_match.xml"
GAMEINFO_URL = "https://px-analyse.7mdt.com/{gid}/data/gameinfo_en.js"

QUOTA_BUCKET  = "sevenm"
DAILY_BUDGET  = int(os.environ.get("SEVENM_DAILY_BUDGET", "80"))
MAX_MATCHES   = int(os.environ.get("SEVENM_MAX_MATCHES", "60"))
REQUEST_DELAY = float(os.environ.get("SEVENM_DELAY", "2.0"))
TIMEOUT       = int(os.environ.get("SEVENM_TIMEOUT", "20"))

# ASCII pur — voir core/odds500.py : un accent ici rend 403 chez Cloudflare.
_UA = ("PredatorPAIM/1.0 (private non-commercial sports-betting pipeline; "
       "max 1 req/2s)")
_HEADERS = {"User-Agent": _UA, "Accept": "*/*"}

SPEC = SourceSpec(
    name="sevenm", role="names", trust=0.7, daily_budget=DAILY_BUDGET,
    langs=("en",), quota_bucket=QUOTA_BUCKET, host="px-analyse.7mdt.com",
    legal="robots 200, Allow: / pour User-agent:* (Content-Signal use=reference)",
)


def _get(url: str) -> str | None:
    try:
        real_url, real_headers = net.prepare("sevenm", url, _HEADERS)
        req = urllib.request.Request(real_url, headers=real_headers)
        # Proxy optionnel — cf. core/net.py. 7M n'a JAMAIS ete appele depuis un
        # runner (0 ligne de log au 2026-08-26) : sa joignabilite reelle depuis
        # Azure est INCONNUE, pas bonne. Si elle s'avere bloquee comme 500.com,
        # SEVENM_PROXY/FREE_SOURCES_PROXY suffit, sans redeploiement.
        # Même reprise qu'odds500 : les deux passent par le même proxy, donc
        # par la même instabilité. Voir core/net.py::open_with_retry.
        with net.open_with_retry("sevenm", req, TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("sevenm: %s — %s", url.split("/")[2],
                    net.describe_failure("sevenm", e))
        return None


_ID_RE = re.compile(r'/goaldata/en/(\d+)\.shtml')


def fetch_match_ids() -> list:
    """Identifiants de matchs publiés par le sitemap (le chemin que le site
    expose POUR être moissonné). Un seul appel."""
    body = _get(SITEMAP_URL)
    if not body:
        return []
    daily_quota.add(QUOTA_BUCKET, 1)
    ids = list(dict.fromkeys(_ID_RE.findall(body)))    # dédoublonné, ordre gardé
    log.info("sevenm: %d identifiants au sitemap", len(ids))
    return ids


_GAMEINFO_RE = re.compile(r'var\s+gameInfo\s*=\s*(\{.*?\})\s*;?\s*$', re.S)


def fetch_fixture(gid: str) -> Fixture | None:
    """Un match : noms ANGLAIS, identifiants numériques, coup d'envoi UTC.

    `time` est un epoch en millisecondes — donc déjà sans fuseau, ce qui évite
    ici le piège UTC+8 qui guette odds.500.com et titan007.
    """
    body = _get(GAMEINFO_URL.format(gid=gid))
    if not body:
        return None
    daily_quota.add(QUOTA_BUCKET, 1)
    m = _GAMEINFO_RE.search(body.strip())
    if not m:
        return None
    try:
        info = json.loads(m.group(1))
    except (ValueError, TypeError):
        log.debug("sevenm: gameInfo illisible pour %s", gid)
        return None

    try:
        kickoff = datetime.fromtimestamp(int(info["time"]) / 1000, tz=timezone.utc)
    except (KeyError, ValueError, TypeError, OSError):
        return None

    home, away = (info.get("taname") or "").strip(), (info.get("tbname") or "").strip()
    if not home or not away:
        return None
    return Fixture(
        source="sevenm", match_id=str(gid), kickoff=kickoff,
        league=(info.get("mname") or "").strip(),
        home=home, away=away,
        team_ids=(str(info.get("taid") or ""), str(info.get("tbid") or "")),
        lang="en",
        raw={"league_id": str(info.get("mid") or "")},
    )


def fetch_fixtures(hours_ahead: int = 36, max_matches: int | None = None,
                   match_ids: list | None = None, offset: int = 0,
                   past_out: list | None = None) -> list:
    """Calendrier anglais borné par le budget journalier.

    Rend [] sur toute panne — source best-effort. Les matchs hors fenêtre sont
    ignorés APRÈS l'appel (le sitemap ne porte pas les dates), ce qui est le
    coût du choix « pas de flux en masse ». Le budget est dimensionné pour
    l'absorber : ces appels servent un dictionnaire, pas un scan.

    `past_out` — si une liste est fournie, les identifiants dont le coup
    d'envoi est DÉJÀ PASSÉ y sont déposés. C'est ce qui permet à l'appelant de
    ne plus jamais les repayer : un match joué ne redeviendra pas à venir.
    Mesuré le 2026-08-26 sur 30 identifiants de tête : 0 échec de requête,
    **27 matchs déjà joués**, 3 seulement dans la fenêtre. Le sitemap n'est
    pas trié par coup d'envoi et traîne plusieurs jours de passé ; sans cette
    mémoire, 90 % du budget finance des matchs terminés, run après run.

    ⚠️ `offset` N'EST PAS COSMÉTIQUE. Le sitemap compte ~936 identifiants et
    n'est PAS trié par intérêt : ses premières entrées sont des coupes
    mineures (FA Cup amateur, coupes nationales) qui ne recoupent jamais le
    slate de 500.com. Un appelant qui prendrait toujours les `max_matches`
    PREMIERS identifiants réinterrogerait les mêmes matchs sans intérêt à
    chaque run et n'apprendrait jamais un seul alias — constaté en live le
    2026-08-22 : 30 identifiants, 0 alias appris, 25 matchs écartés.
    `core/free_sources.py` fait donc tourner un curseur persistant d'un run à
    l'autre pour balayer TOUT le sitemap.
    """
    spent = daily_quota.spent(QUOTA_BUCKET)
    if spent >= DAILY_BUDGET:
        log.warning("sevenm: budget journalier atteint (%d/%d) — cycle ignoré",
                    spent, DAILY_BUDGET)
        return []

    ids = match_ids if match_ids is not None else fetch_match_ids()
    if not ids:
        return []

    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    cap = max_matches or MAX_MATCHES

    # Fenêtre glissante, avec bouclage : le curseur balaie tout le sitemap au
    # fil des runs plutôt que de repasser sur la même tête de liste.
    if ids and offset:
        offset %= len(ids)
        ids = ids[offset:] + ids[:offset]

    out = []
    for i, gid in enumerate(ids[:cap]):
        if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
            log.warning("sevenm: budget épuisé en cours de cycle — %d fixtures", len(out))
            break
        if i:
            time.sleep(REQUEST_DELAY)
        fx = fetch_fixture(gid)
        if not (fx and fx.kickoff_utc):
            continue
        if fx.kickoff_utc <= now - timedelta(hours=3):
            # Match joué : définitif, il ne repassera jamais dans la fenêtre.
            if past_out is not None:
                past_out.append(str(gid))
            continue
        if fx.kickoff_utc <= until:
            out.append(fx)
    log.info("sevenm: %d fixtures anglaises dans la fenêtre | %d déjà joué(s) "
             "mémorisé(s) | %d req aujourd'hui",
             len(out), len(past_out or []), daily_quota.spent(QUOTA_BUCKET))
    return out


def probe() -> tuple:
    """(joignable ?, détail) — pour scripts/ops.py sources."""
    body = _get(SITEMAP_URL)
    if not body:
        return False, "injoignable"
    n = len(set(_ID_RE.findall(body)))
    return (n > 0), (f"{n} matchs au sitemap | "
                     f"{daily_quota.spent(QUOTA_BUCKET)}/{DAILY_BUDGET} req aujourd'hui")
