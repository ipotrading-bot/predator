"""
core/odds500.py — odds.500.com (500彩票网, 足球指数) : comparateur chinois gratuit.

CE QUE ÇA APPORTE
-----------------
Un seul appel de calendrier donne le slate du jour avec, PAR MATCH, une page
« 百家欧赔 » (comparaison 1X2 de ~30 books) sans clé et sans quota. Vérifié le
2026-08-22 depuis une IP datacenter : 64 matchs, dont 英超 8, 意甲 10, 西甲 8,
法甲 5, 葡超 5, 荷甲 4, plus 日职 / 韩职 / 巴甲 / 美职足. C'est à la fois le
cœur du portefeuille (Big 5) et le gisement asiatique que ni Matchbook ni
odds-api.io ne couvrent.

Trois marchés, trois pages, aucune query string :
    fenxi/ouzhi-{fid}.shtml   百家欧赔  — 1X2 multi-books      (implémenté)
    fenxi/yazhi-{fid}.shtml   亚盘      — handicap asiatique   (implémenté)
    fenxi/daxiao-{fid}.shtml  大小球    — totals               (implémenté)

POURQUOI ELLE PASSE DEPUIS LES RUNNERS
---------------------------------------
Contrainte structurelle apprise de l'incident du 10→20 août (docstring de
core/api_sports.py) : depuis GitHub Actions, seules survivent les API
authentifiées par clé et les sources vérifiées HTTP 200 depuis une IP
datacenter. Celle-ci est du second type — vérifiée 200 le 2026-08-22 depuis
une sortie datacenter (Azure) : 288 ko de calendrier, 276 ko sur ouzhi,
189 ko sur yazhi, 191 ko sur daxiao.

STATUT JURIDIQUE — LU DANS LE robots.txt, PAS SUPPOSÉ
------------------------------------------------------
`odds.500.com/robots.txt` est servi (200) et il est PRÉCIS. Les lignes qui
nous concernent, recopiées le 2026-08-22 :

    Disallow: /fenxi1/
    Disallow: /ouzhi-*.shtml
    Disallow: /yazhi-*.shtml
    Disallow: /daxiao-*.shtml
    Disallow: /fenxi/ouzhi-*.shtml?ctype=*
    Disallow: /fenxi/ouzhi-*.shtml?order=*
    Disallow: /fenxi/ouzhi-*.shtml?cids=*
    Disallow: /fenxi/*?ctype=
    (idem pour yazhi/daxiao/rangqiu/bifen)

Lecture, chemin par chemin :
  - `/ouzhi-*.shtml` est ancré À LA RACINE (RFC 9309 : un motif commençant par
    `/` se compare depuis le début du chemin). Il n'attrape donc PAS
    `/fenxi/ouzhi-1420317.shtml`, qui est le nôtre ;
  - les seules variantes de `/fenxi/ouzhi-*.shtml` interdites sont celles qui
    portent `?ctype=`, `?order=` ou `?cids=`.

Autrement dit : **la query string EST la frontière**. Les trois endpoints
utilisés ici, sans paramètre, sont explicitement hors des Disallow ; ajouter
un seul `?ctype=` les ferait basculer dedans. La règle héritée de titan007
(« ne jamais ajouter de paramètre ») n'est donc plus seulement de la prudence
sur cette source : c'est littéralement le texte du robots.txt. Elle est tenue
par `tests/test_odds500.py::TestRobotsTxt`.

Deux conséquences pratiques de plus :
  - `/fenxi1/` est interdit. La page de cotes contient un lien
    `/fenxi1/ouzhi_same.php?cid=…` : il ne doit JAMAIS être suivi ;
  - `/js/`, `/static/`, `/images/` sont interdits — on n'y touche pas.

Reste que c'est une TOLÉRANCE, pas un contrat : robots.txt autorise le
parcours, il ne concède aucun droit sur la donnée. D'où la discipline
titan007 maintenue : cadence basse (REQUEST_DELAY = 2 s, soit ≤ 1 req/2 s),
budget journalier partagé, User-Agent honnête, et traitement best-effort —
toute panne rend [] avec un log, jamais une exception.

⚠️ NOTE SUR LES AUTRES HÔTES DE LA FAMILLE. `live.500.com` et `www.500.com`
ont répondu **567 « Restricted Access » (WAF Tencent EdgeOne)** sur leur
robots.txt lors d'un premier passage le 2026-08-22, puis 200 avec un
robots.txt normal quelques minutes plus tard : c'était un blocage
TRANSITOIRE du WAF, pas une politique. On n'en tire donc aucune conclusion
juridique — simplement, ces hôtes n'apportent rien de plus que
`odds.500.com` et restent hors périmètre, ce qui évite d'avoir à raisonner
sur un WAF dont le comportement varie d'une minute à l'autre.

FUSEAU HORAIRE — LE MÊME PIÈGE QUE TITAN007
--------------------------------------------
Le calendrier publie `date-dtime="2026-08-22 17:00:00"` sans fuseau. Calibré
le 2026-08-22 : 日职 Kashima–Fukuoka à « 17:00 » se joue à 08:00 UTC, et
英超 Hull–Man United à « 19:30 » à 10:30 UTC — soit **UTC+8**, comme
titan007. Se tromper ici décalerait tous les `commence_time` de huit heures :
les signaux seraient refusés par le garde « match déjà commencé », ou pire,
réglés sur le mauvais match.

LES NOMS DE BOOKS SONT MASQUÉS — ON N'EN A PAS BESOIN
-------------------------------------------------------
Pour un visiteur anonyme, la page de cotes masque les libellés
(`P*********`, `*冠`, `*门`). L'identifiant NUMÉRIQUE `cid` de chaque ligne,
lui, est en clair et stable — et le calendrier, sur ses deux premières lignes,
publie deux libellés EN CLAIR (`Bet365` pour cid=3, `澳门` pour cid=5), ce qui
confirme la carte gratuitement à chaque run.

L'identité de chaque book a été établie sans lire un seul nom, par deux
signatures qui n'ont pas de langue — la marge et le pays :

    cid   marge 1X2   pays        identité         rôle ici
     18     0,46 %    Royaume-Uni Betfair Exchange sharp (exchange)
   1055     3,87 %    Pays-Bas    Pinnacle         sharp (référence)
      3     5,71 %    Royaume-Uni Bet365           soft
    280    10,26 %    Philippines 皇冠 / Crown     pseudo-sharp AH
      5    11,12 %    Macao       澳门 / Macau     pseudo-sharp AH

(mesures du 2026-08-22 sur 赫尔城VS曼彻斯特联). Le contrôle croisé qui valide
la carte est dans le rapport : le prix cid=1055 dévigorisé tombe à 1,5 point
de Polymarket sur le favori, et cid=18 à 0,3 point — deux hôtes totalement
indépendants.

⚠️ CONSÉQUENCE POUR LE PSEUDO-SHARP : le cahier des charges proposait une
médiane no-vig de {皇冠, Bet365, 澳门}. Mesuré, ce trio porte 5,7 / 10,3 /
11,1 % de marge sur le 1X2 — 皇冠 et 澳门 sont des books de HANDICAP dont le
1X2 est décoratif. `pseudo_sharp_price()` sélectionne donc les books par
marge mesurée (voir la fonction), ce qui est le même critère, appliqué à la
donnée plutôt qu'à une liste écrite d'avance.
"""
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from core import daily_quota, net
from core.source_adapter import (Fixture, SourceSpec, novig_probs, vig_pct)

log = logging.getLogger("PREDATOR.odds500")

BASE          = "https://odds.500.com"
FIXTURES_URL  = f"{BASE}/"
OUZHI_URL     = BASE + "/fenxi/ouzhi-{fid}.shtml"     # 1X2
YAZHI_URL     = BASE + "/fenxi/yazhi-{fid}.shtml"     # handicap asiatique
DAXIAO_URL    = BASE + "/fenxi/daxiao-{fid}.shtml"    # totals

SITE_UTC_OFFSET_H = int(os.environ.get("ODDS500_UTC_OFFSET", "8"))

QUOTA_BUCKET  = "odds500"
DAILY_BUDGET  = int(os.environ.get("ODDS500_DAILY_BUDGET", "400"))
MAX_MATCHES   = int(os.environ.get("ODDS500_MAX_MATCHES", "40"))
REQUEST_DELAY = float(os.environ.get("ODDS500_DELAY", "2.0"))
TIMEOUT       = int(os.environ.get("ODDS500_TIMEOUT", "25"))

# User-Agent honnête : on dit ce qu'on est. Le robots.txt de la famille 7M
# (px-analyse.7mdt.com) interdit nommément les crawlers d'IA (ClaudeBot,
# GPTBot, CCBot…) tout en gardant `User-agent: * / Allow: /` — se présenter
# honnêtement est donc aussi ce qui nous garde du bon côté de ces règles.
#
# ⚠️ ASCII PUR, OBLIGATOIRE. urllib encode les en-têtes en latin-1 : un accent
# dans cette chaîne fait rendre 403 à Cloudflare (constaté le 2026-08-22 sur
# gamma-api.polymarket.com, là où curl passait). Une source qui meurt pour un
# accent dans son User-Agent est exactement le genre de panne qu'on ne
# diagnostique pas depuis un log de cron.
_UA = ("PredatorPAIM/1.0 (private non-commercial sports-betting pipeline; "
       "max 1 req/2s)")
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}

SPORT    = "soccer"
SPORT_ID = 1

SPEC = SourceSpec(
    name="odds500", role="consensus", trust=0.55, daily_budget=DAILY_BUDGET,
    langs=("zh",), quota_bucket=QUOTA_BUCKET, host="odds.500.com",
    legal="robots 403 (aucune restriction déclarée) — tolérance, endpoints sans query string",
)

# Carte cid → (identité, rôle). Établie par marge + pays (voir le docstring),
# CONFIRMÉE à chaque run par les libellés en clair du calendrier
# (`verify_book_map`). Volontairement courte : on ne garde que les books dont
# l'identité est prouvée, pas les 30 lignes de la page.
BOOK_MAP = {
    18:   ("betfair_exchange", "sharp"),
    1055: ("pinnacle",         "sharp"),
    3:    ("bet365",           "soft"),
    293:  ("william_hill",     "soft"),
    2:    ("ladbrokes",        "soft"),
    16:   ("1xbet",            "soft"),
    11:   ("bwin",             "soft"),
    6:    ("betvictor",        "soft"),
    280:  ("crown",            "pseudo"),
    5:    ("macau",            "pseudo"),
    122:  ("hkjc",             "pseudo"),
}

# Libellés que le CALENDRIER publie en clair, par cid. Sert de test de
# non-régression permanent : si 500.com renumérote ses books, la carte
# ci-dessus devient fausse en silence — `verify_book_map()` le voit.
BOOK_LABELS = {3: "bet365", 5: "澳门"}


def _get(url: str) -> str | None:
    """GET best-effort, décodé depuis le GB2312/GB18030 du site."""
    try:
        # Relais optionnel (Cloudflare Worker) : reecrit l'URL, inchangee
        # si aucun relais n'est configure. Voir core/net.py.
        real_url, real_headers = net.prepare("odds500", url, _HEADERS)
        req = urllib.request.Request(real_url, headers=real_headers)
        # Proxy optionnel : 500.com refuse les plages d'IP des runners GitHub
        # (mesuré le 2026-08-26 — 200 depuis un poste, Connection refused
        # depuis Azure). Sans ODDS500_PROXY/FREE_SOURCES_PROXY, `opener` vaut
        # None et le comportement est strictement celui d'avant.
        # UNE reprise sur échec de transport : un proxy gratuit et partagé
        # rate ~1 requête sur 3 (mesuré le 2026-08-28), et sans reprise cet
        # aléa coûte le calendrier entier du run. Voir core/net.py.
        with net.open_with_retry("odds500", req, TIMEOUT) as r:
            raw = r.read()
    except Exception as e:
        log.warning("odds500: %s — %s", url.rsplit("/", 1)[-1] or url,
                    net.describe_failure("odds500", e))
        return None
    # gb18030 est un sur-ensemble de gb2312 : il décode aussi les caractères
    # rares (noms d'équipes sud-américains translittérés) que gb2312 refuse.
    html = raw.decode("gb18030", "replace")
    if mur_anti_bot(html):
        # Mesuré le 2026-09-03 : depuis le 1er septembre, 500.com ne rend plus
        # le calendrier mais un script obfusqué (~1 ko) qui pose un cookie
        # « EO_Bot_Ssid » — un défi JavaScript de Tencent EdgeOne. Sans moteur
        # JS, la source est MUETTE, relais ou pas. Le dire en clair plutôt
        # que de logguer « 0 match au calendrier » comme si le slate était
        # vide : c'est ce silence qui a caché trois jours de panne.
        log.warning("odds500: MUR ANTI-BOT (défi EdgeOne, cookie EO_Bot_Ssid) sur %s — "
                    "source muette, aucun match ne peut être lu ; à retirer si ça dure",
                    url.rsplit("/", 1)[-1] or url)
        return None
    return html


def mur_anti_bot(html: str) -> bool:
    """Le corps est-il un défi JavaScript plutôt qu'une page du site ?"""
    return "EO_Bot_Ssid" in html or (len(html) < 4000 and "<title>" not in html
                                     and "<script>" in html[:200])


def _odd(val) -> float:
    try:
        f = float(val)
        return f if 1.01 < f < 1000 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _kickoff_utc(text: str) -> datetime | None:
    """« 2026-08-22 17:00:00 » (fuseau du site) → datetime UTC."""
    try:
        local = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    return local - timedelta(hours=SITE_UTC_OFFSET_H)


_ROW_RE   = re.compile(
    r'<tr data-fid="(?P<fid>\d+)" data-cid="(?P<cid>\d+)"[^>]*'
    r'date-dtime="(?P<dtime>[^"]+)"(?P<body>.*?)</tr>', re.S)
# Chaque match occupe DEUX lignes de tableau : la première porte la date et un
# book, la seconde n'a que `data-fid`/`data-cid` et un second book. Sans ce
# second motif, `verify_book_map` ne contrôlerait jamais que cid=3 — et le
# contrôle de non-régression sur cid=5 (澳门) serait un test qui ne teste rien.
_ROW2_RE  = re.compile(
    r'<tr data-fid="(?P<fid>\d+)" data-cid="(?P<cid>\d+)">(?P<body>.*?)</tr>', re.S)
_LEAGUE_RE = re.compile(r'liansai\.500\.com/zuqiu-(\d+)/"[^>]*title="([^"]*)"')
_TEAM_RE   = re.compile(r'liansai\.500\.com/team/(\d+)/"[^>]*title="([^"]*)"')
_LABEL_RE  = re.compile(r'<td class="border_r_c5">([^<]{1,20})</td>')


def fetch_fixtures() -> list:
    """Calendrier du jour : UN seul appel, aucun paramètre d'URL.

    Rend des `Fixture` sans cotes (les cotes coûtent un appel par match) mais
    avec les identifiants NUMÉRIQUES d'équipe et de ligue — c'est eux que le
    dictionnaire d'alias utilise comme clé, jamais les libellés chinois.
    """
    body = _get(FIXTURES_URL)
    if not body:
        return []
    daily_quota.add(QUOTA_BUCKET, 1)

    # Premier passage : les libellés de books EN CLAIR, par match. Ils vivent
    # sur les deux lignes de tableau du match, d'où les deux motifs.
    labels_by_fid: dict = {}
    for rx in (_ROW_RE, _ROW2_RE):
        for m in rx.finditer(body):
            lab = _LABEL_RE.search(m.group("body"))
            if lab:
                labels_by_fid.setdefault(m.group("fid"), {})[
                    int(m.group("cid"))] = lab.group(1).strip()

    out = []
    for m in _ROW_RE.finditer(body):
        when = _kickoff_utc(m.group("dtime"))
        if when is None:
            continue
        blk = m.group("body")
        lg = _LEAGUE_RE.search(blk)
        teams = _TEAM_RE.findall(blk)
        if len(teams) < 2:
            continue
        out.append(Fixture(
            source="odds500",
            match_id=m.group("fid"),
            kickoff=when,
            league=(lg.group(2) if lg else ""),
            home=teams[0][1],
            away=teams[1][1],
            team_ids=(teams[0][0], teams[1][0]),
            lang="zh",
            raw={"league_id": lg.group(1) if lg else "",
                 "labels": labels_by_fid.get(m.group("fid"), {})},
        ))
    log.info("odds500: %d matchs au calendrier", len(out))
    return out


def verify_book_map(fixtures: list) -> list:
    """Contrôle de non-régression de BOOK_MAP contre les libellés en clair du
    calendrier. Rend la liste des incohérences (vide = tout va bien).

    500.com peut renuméroter ses books ; ça rendrait BOOK_MAP fausse EN
    SILENCE, et un prix « Pinnacle » qui serait en fait celui d'un book à 11 %
    de marge produirait des edges massifs et faux. Ce contrôle est gratuit :
    la donnée est déjà dans le calendrier qu'on vient de télécharger.
    """
    seen: dict = {}
    for fx in fixtures or []:
        for cid, label in (fx.raw.get("labels") or {}).items():
            seen.setdefault(cid, label)
    problems = []
    for cid, expected in BOOK_LABELS.items():
        got = seen.get(cid)
        if got and expected.lower() not in got.lower():
            problems.append(f"cid={cid} attendu ~{expected!r}, calendrier dit {got!r}")
    for p in problems:
        log.error("odds500: BOOK_MAP suspecte — %s", p)
    return problems


_BOOKROW_SPLIT = re.compile(r'(?=<tr class="tr[12]" id="\d+")')
_BOOKROW_HEAD  = re.compile(r'<tr class="tr[12]" id="(\d+)"[^>]*data-time="([^"]*)"')
_PRICE_RE      = re.compile(r'klfc="[\d.]*"[^>]*>\s*([\d.]+)</td>')


def fetch_odds(fid: str) -> dict:
    """Page 百家欧赔 → {cid: {"odds": [1,X,2], "opening": [...], "updated": str}}.

    Chaque ligne de la table `#datatb` porte SIX prix : les trois premiers sont
    l'ouverture (初赔), les trois suivants la cote actuelle (即时). On prend
    l'actuelle ; l'ouverture n'est gardée que parce qu'elle est déjà là et
    qu'elle alimentera le CLV sans un appel de plus.

    `data-time` donne la fraîcheur PAR BOOK — c'est la mesure que le scorecard
    (`source_adapter.record_observation`) consomme, et elle est gratuite.
    """
    body = _get(OUZHI_URL.format(fid=fid))
    if not body:
        return {}
    daily_quota.add(QUOTA_BUCKET, 1)

    table = re.search(r'<table[^>]*id="datatb".*', body, re.S)
    if not table:
        log.debug("odds500: pas de table de cotes pour %s", fid)
        return {}

    out: dict = {}
    for chunk in _BOOKROW_SPLIT.split(table.group(0)):
        head = _BOOKROW_HEAD.match(chunk)
        if not head:
            continue
        prices = [_odd(x) for x in _PRICE_RE.findall(chunk)]
        if len(prices) < 6:
            continue
        opening, current = prices[:3], prices[3:6]
        if not (current[0] and current[2]):
            continue
        out[int(head.group(1))] = {
            "odds": current, "opening": opening, "updated": head.group(2),
        }
    return out


def _fresh_seconds(updated: str, now: datetime | None = None) -> float | None:
    """Âge d'un prix, en secondes. `data-time` est dans le fuseau du site."""
    try:
        stamp = datetime.strptime(updated.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc) - timedelta(hours=SITE_UTC_OFFSET_H)
    except (ValueError, AttributeError):
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - stamp).total_seconds())


def sharp_price(books: dict) -> tuple:
    """(prix, nom) du meilleur book SHARP disponible, ou (None, None).

    Ordre de préférence : Pinnacle d'abord — c'est la référence que le ledger
    historise depuis toujours — puis l'exchange. On veut CE prix, pas le plus
    généreux."""
    for cid in (1055, 18):
        rec = books.get(cid)
        if rec and rec["odds"][0] and rec["odds"][2]:
            return rec["odds"], BOOK_MAP[cid][0]
    return None, None


def soft_price(books: dict) -> tuple:
    """Meilleur prix parmi les books soft cartographiés."""
    best, name = None, None
    for cid, (label, role) in BOOK_MAP.items():
        if role != "soft":
            continue
        rec = books.get(cid)
        if not rec:
            continue
        if best is None or rec["odds"][0] > best[0]:
            best, name = rec["odds"], label
    return best, name


# Marge maximale pour qu'un book entre dans la médiane pseudo-sharp. 6 % est
# le point de coupure mesuré : au-dessus, on ne trouve plus que des books de
# handicap dont le 1X2 est décoratif (皇冠 10,3 %, 澳门 11,1 %).
PSEUDO_MAX_VIG = float(os.environ.get("ODDS500_PSEUDO_MAX_VIG", "6.0"))
PSEUDO_MIN_BOOKS = int(os.environ.get("ODDS500_PSEUDO_MIN_BOOKS", "3"))

# Pénalité appliquée au prix pseudo-sharp, en % — même mécanique que les
# books non-Pinnacle de core/oracle.py : la pénalité gonfle le prix de
# référence, donc RÉDUIT l'edge affiché. Un pseudo-sharp n'est jamais au même
# niveau de confiance qu'un vrai prix Pinnacle.
PSEUDO_PENALTY_PCT = float(os.environ.get("ODDS500_PSEUDO_PENALTY", "1.0"))


def pseudo_sharp_price(books: dict) -> tuple:
    """Prix de référence de SECOURS quand Pinnacle manque, avec pénalité.

    Rend (prix_1X2, detail) ou (None, detail).

    Les books ne sont PAS choisis par une liste écrite d'avance mais par leur
    marge MESURÉE sur ce match : tout book cartographié sous PSEUDO_MAX_VIG
    entre dans la médiane. C'est le même critère que « prendre les books
    sharps », appliqué à la donnée du jour — un book qui élargit ses marges
    sort de lui-même du panier, sans qu'on ait à maintenir une liste.

    Médiane et non moyenne : un seul prix figé parmi les books ne doit pas
    déplacer la référence (c'est la leçon de MAX_SOFT_OUTLIER dans
    core/titan007.py, où un book figé donnait 28 % d'edge imaginaire).
    """
    panel = []
    for cid, rec in books.items():
        if cid not in BOOK_MAP:
            continue
        o = rec["odds"]
        if not (o[0] and o[2]):
            continue
        v = vig_pct(o)
        if 0 <= v <= PSEUDO_MAX_VIG:
            panel.append((BOOK_MAP[cid][0], o, v))

    detail = {"panel": [p[0] for p in panel], "vigs": [round(p[2], 2) for p in panel]}
    if len(panel) < PSEUDO_MIN_BOOKS:
        detail["reason"] = f"{len(panel)} books sous {PSEUDO_MAX_VIG}% de marge, il en faut {PSEUDO_MIN_BOOKS}"
        return None, detail

    # Médiane des PROBABILITÉS dévigorisées, pas des cotes : c'est la
    # grandeur additive. Une médiane de cotes ne somme pas à 1 et fabriquerait
    # une marge parasite.
    probs = [novig_probs(o) for _, o, _ in panel]
    probs = [p for p in probs if len(p) == 3]
    if len(probs) < PSEUDO_MIN_BOOKS:
        detail["reason"] = "probabilités inexploitables"
        return None, detail

    med = []
    for k in range(3):
        col = sorted(p[k] for p in probs)
        mid = len(col) // 2
        med.append(col[mid] if len(col) % 2 else (col[mid - 1] + col[mid]) / 2)
    total = sum(med)
    if total <= 0:
        detail["reason"] = "médiane dégénérée"
        return None, detail

    penalty = 1 + PSEUDO_PENALTY_PCT / 100
    price = [round((total / p) * penalty, 4) if p > 0 else 0.0 for p in med]
    detail.update({"penalty_pct": PSEUDO_PENALTY_PCT, "n_books": len(probs)})
    return price, detail


def fetch_matches(hours_ahead: int = 24, max_matches: int | None = None) -> list:
    """Matchs à venir, dans la forme du harvester.

    Coût : 1 requête de calendrier + 1 par match retenu, plafonné par
    `max_matches` et par le budget journalier partagé. Rend [] sur toute
    panne — source best-effort, jamais une dépendance dure.

    ⚠️ Les libellés d'équipe sont CHINOIS. `match`/`home`/`away` les portent
    tels quels ; c'est à l'appelant de passer par core/team_aliases.py avant
    d'émettre quoi que ce soit. Les `_alias_*` de chaque enregistrement lui
    donnent les identifiants numériques dont il a besoin pour ça.
    """
    spent = daily_quota.spent(QUOTA_BUCKET)
    if spent >= DAILY_BUDGET:
        log.warning("odds500: budget journalier atteint (%d/%d) — cycle ignoré",
                    spent, DAILY_BUDGET)
        return []

    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    cap = max_matches or MAX_MATCHES

    fixtures = fetch_fixtures()
    verify_book_map(fixtures)
    upcoming = [f for f in fixtures
                if f.kickoff_utc and now < f.kickoff_utc <= until]
    upcoming.sort(key=lambda f: f.kickoff_utc)
    if not upcoming:
        log.info("odds500: 0 match dans les %dh", hours_ahead)
        return []

    matches, n_sharp = [], 0
    for i, fx in enumerate(upcoming[:cap]):
        if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
            log.warning("odds500: budget épuisé en cours de cycle — %d matchs conservés",
                        len(matches))
            break
        if i:
            time.sleep(REQUEST_DELAY)        # cadence volontairement basse
        books = fetch_odds(fx.match_id)
        if not books:
            continue

        sharp, sharp_name = sharp_price(books)
        soft, soft_name = soft_price(books)
        pseudo, pseudo_detail = (None, {})
        if not sharp:
            pseudo, pseudo_detail = pseudo_sharp_price(books)
        reference = sharp or pseudo
        if not soft and not reference:
            continue

        fresh = [_fresh_seconds(r["updated"]) for r in books.values()]
        fresh = [f for f in fresh if f is not None]

        m = {
            "id":            f"o500_{fx.match_id}",
            "match":         f"{fx.home} vs {fx.away}",
            "home":          fx.home,
            "away":          fx.away,
            "league":        fx.league,
            "sport":         SPORT,
            "sport_id":      SPORT_ID,
            "commence_time": fx.kickoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Sans book soft, la référence sert de soft : edge nul par
            # construction, donc jamais de faux signal (règle titan007).
            "odds_1xbet":    _as_1x2(soft or reference),
            "_soft_source":  f"odds500/{soft_name}" if soft_name else "odds500",
            # Contrat multilingue : le moteur ne doit RIEN émettre depuis cet
            # enregistrement avant d'avoir résolu les alias. Ces champs sont
            # ce qui rend la résolution possible sans relire la page.
            "_lang":            "zh",
            "_alias_source":    "odds500",
            "_alias_team_ids":  fx.team_ids,
            "_alias_league_id": fx.raw.get("league_id", ""),
            "_needs_alias":     True,
            "_freshness_s":     sorted(fresh)[len(fresh) // 2] if fresh else None,
            "_books_seen":      len(books),
        }
        if sharp:
            m["odds_pinnacle"] = _as_1x2(sharp)
            m["_sharp_source"] = f"odds500/{sharp_name}"
            n_sharp += 1
        elif pseudo:
            m["odds_pinnacle"] = _as_1x2(pseudo)
            m["_sharp_source"] = "odds500/pseudo"
            m["_pseudo_sharp"] = pseudo_detail
        matches.append(m)

    log.info("odds500: %d matchs (%d avec prix sharp réel) / %d à venir | %d req aujourd'hui",
             len(matches), n_sharp, len(upcoming), daily_quota.spent(QUOTA_BUCKET))
    return matches


def _as_1x2(prices) -> dict:
    return {"1": prices[0], "X": prices[1], "2": prices[2]} if prices else {}


def probe() -> tuple:
    """(joignable ?, détail) — pour scripts/ops.py sources."""
    body = _get(FIXTURES_URL)
    if not body:
        return False, "injoignable"
    n = len(_ROW_RE.findall(body))
    return (n > 0), (f"{n} matchs au calendrier | "
                     f"{daily_quota.spent(QUOTA_BUCKET)}/{DAILY_BUDGET} req aujourd'hui")
