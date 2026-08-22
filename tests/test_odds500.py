"""
tests/test_odds500.py — parseurs 500.com, carte des books, pseudo-sharp.

Les fragments HTML utilisés sont RÉELS : capturés le 2026-08-22 sur
`odds.500.com` (calendrier et page 百家欧赔 de 赫尔城VS曼联, fid 1420317),
puis réduits. Un test écrit contre du HTML inventé validerait le parseur
contre l'idée qu'on se fait de la page, pas contre la page.

Aucun réseau : `_get` est stubbé partout (voir tests/conftest.py).
"""
import pytest

from core import odds500

# ── Fragments RÉELS (compactés) ──────────────────────────────────────

CALENDRIER = (
    '<tr data-fid="1418972" data-cid="3" data-mid="39" data-pk="," data-bp="0" '
    'date-dtime="2026-08-22 17:00:00"> '
    '<td rowspan="2"><label for="fck_1418972">周六001</label></td> '
    '<td rowspan="2"><a href="//liansai.500.com/zuqiu-19895/" title="日职">日职</a></td> '
    '<td rowspan="2">08-22 17:00</td> '
    '<td rowspan="2" class="text_right no_border">'
    '<a class="team_link" href="//liansai.500.com/team/1029/" target="_blank" title="鹿岛鹿角">鹿岛鹿角</a></td> '
    '<td rowspan="2" class="no_border">VS</td> '
    '<td rowspan="2" class="text_left">'
    '<a class="team_link" href="//liansai.500.com/team/808/" target="_blank" title="福冈黄蜂">福冈黄蜂</a></td> '
    '<td class="border_r_c5">Bet365</td> <td>1.000</td></tr>'
    '<tr data-fid="1418972" data-cid="5"><td class="border_r_c5">澳门</td></tr>'
)


def _book_row(cid: str, updated: str, ouverture, actuelle, nom="P*********") -> str:
    """Reproduit EXACTEMENT la structure d'une ligne de `#datatb` : six prix
    porteurs d'un attribut `klfc` (trois d'ouverture puis trois actuels),
    noyés dans des cellules de pourcentages qui, elles, n'en portent pas."""
    def cell(klfc, val):
        return f'<td row="1" width="33.3%" klfc="{klfc}" onclick="OZ.r(this)" style="cursor:pointer" >{val}</td>'
    o1, ox, o2 = ouverture
    a1, ax, a2 = actuelle
    return (
        f'<tr class="tr2" id="{cid}" ttl="zy" data-time="{updated}" xls="row"> '
        f'<td class="td_one"><p>12</p></td> '
        f'<td row="1" class="tb_plgs" title="{nom}"><p>'
        f'<a href="https://odds.500.com/ouzhi.php?cid={cid}">'
        f'<span class="quancheng" style="display:;">{nom}</span></a></p></td> '
        f'<td><table class="pl_table_data"><tbody>'
        f'<tr class="tr_bdb td_show_cp">{cell("1.18", o1)}{cell("3.56", ox)}{cell("0.40", o2)}</tr>'
        f'<tr>{cell("75.06", a1)}{cell("9.24", ax)}{cell("0.55", a2)}</tr>'
        f'</tbody></table></td> '
        # cellules de pourcentages : SANS klfc, elles ne doivent pas être lues
        f'<td><table class="pl_table_data"><tbody><tr>'
        f'<td row="1" width="33.3%" >13.89%</td><td row="1" >20.38%</td>'
        f'<td row="1" >65.73%</td></tr></tbody></table></td></tr>'
    )


# Prix réels du 2026-08-22 sur fid 1420317.
PAGE_COTES = '<table id="datatb">' + "".join([
    _book_row("1055", "2026-08-22 11:28:08", (6.72, 4.58, 1.42), (9.19, 5.14, 1.36), "P*********"),
    _book_row("18",   "2026-08-22 09:48:39", (9.50, 5.20, 1.40), (10.0, 5.40, 1.39), "必*"),
    _book_row("3",    "2026-08-22 05:43:45", (9.00, 4.90, 1.34), (9.50, 5.00, 1.33), "B*****"),
    _book_row("280",  "2026-08-22 11:44:27", (8.00, 5.20, 1.36), (7.50, 5.00, 1.30), "*冠"),
    _book_row("5",    "2026-08-22 09:01:30", (7.00, 4.80, 1.31), (7.25, 4.90, 1.30), "*门"),
]) + "</table>"


@pytest.fixture
def sans_reseau(monkeypatch):
    """Stubbe `_get` : le test porte sur les parseurs, pas sur HTTP."""
    pages = {}

    def fake_get(url):
        for frag, body in pages.items():
            if frag in url:
                return body
        return None

    monkeypatch.setattr(odds500, "_get", fake_get)
    return pages


class TestCalendrier:
    def test_extrait_identifiants_numeriques_et_libelles(self, sans_reseau):
        sans_reseau["odds.500.com/"] = CALENDRIER
        fx = odds500.fetch_fixtures()
        assert len(fx) == 1
        f = fx[0]
        assert f.match_id == "1418972"
        assert (f.home, f.away) == ("鹿岛鹿角", "福冈黄蜂")
        # Les identifiants NUMÉRIQUES sont la vraie clé du dictionnaire
        # d'alias : ils n'ont pas de langue.
        assert f.team_ids == ("1029", "808")
        assert f.raw["league_id"] == "19895"
        assert f.lang == "zh"

    def test_le_fuseau_du_site_est_utc_plus_huit(self, sans_reseau):
        """Le piège qui décalerait TOUS les coups d'envoi de huit heures.

        Le calendrier publie « 17:00 » sans fuseau ; ce match se joue à 08:00
        UTC. Se tromper ici ferait régler les signaux sur le mauvais match.
        """
        sans_reseau["odds.500.com/"] = CALENDRIER
        f = odds500.fetch_fixtures()[0]
        assert f.kickoff_utc.strftime("%Y-%m-%dT%H:%MZ") == "2026-08-22T09:00Z"

    def test_page_vide_ou_panne_rend_liste_vide(self, sans_reseau):
        assert odds500.fetch_fixtures() == []          # _get rend None
        sans_reseau["odds.500.com/"] = "<html>rien</html>"
        assert odds500.fetch_fixtures() == []


class TestCarteDesBooks:
    def test_le_calendrier_confirme_la_carte_a_chaque_run(self, sans_reseau):
        # 500.com masque les noms sur la page de cotes mais les publie en
        # clair dans le calendrier : contrôle gratuit et permanent.
        sans_reseau["odds.500.com/"] = CALENDRIER
        assert odds500.verify_book_map(odds500.fetch_fixtures()) == []

    def test_les_DEUX_libelles_du_calendrier_sont_captures(self, sans_reseau):
        """Un match occupe deux lignes de tableau, une par book. Ne lire que
        la première ferait de la vérification de cid=5 un test qui ne teste
        rien — le contrôle ne porterait plus que sur cid=3."""
        sans_reseau["odds.500.com/"] = CALENDRIER
        labels = odds500.fetch_fixtures()[0].raw["labels"]
        assert labels == {3: "Bet365", 5: "澳门"}

    def test_une_renumerotation_du_second_book_est_aussi_detectee(self, sans_reseau):
        sans_reseau["odds.500.com/"] = CALENDRIER.replace(
            '<td class="border_r_c5">澳门</td>',
            '<td class="border_r_c5">威廉希尔</td>')
        problems = odds500.verify_book_map(odds500.fetch_fixtures())
        assert problems and "cid=5" in problems[0]

    def test_une_renumerotation_des_books_est_detectee(self, sans_reseau):
        # Si 500.com réattribuait cid=3 à un autre book, BOOK_MAP deviendrait
        # fausse EN SILENCE et un prix « Bet365 » pourrait être celui d'un
        # book à 13 % de marge — donc des edges massifs et faux.
        sans_reseau["odds.500.com/"] = CALENDRIER.replace(
            '<td class="border_r_c5">Bet365</td>',
            '<td class="border_r_c5">Betfair</td>')
        problems = odds500.verify_book_map(odds500.fetch_fixtures())
        assert problems and "cid=3" in problems[0]


class TestCotes:
    def test_lit_la_cote_ACTUELLE_pas_louverture(self, sans_reseau):
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        assert books[1055]["odds"] == [9.19, 5.14, 1.36]      # actuelle
        assert books[1055]["opening"] == [6.72, 4.58, 1.42]   # ouverture (CLV)

    def test_ignore_les_cellules_de_pourcentage(self, sans_reseau):
        # Elles n'ont pas d'attribut klfc ; les lire ferait entrer 13.89 dans
        # les cotes.
        sans_reseau["ouzhi-"] = PAGE_COTES
        for rec in odds500.fetch_odds("1420317").values():
            assert all(1.01 < o < 100 for o in rec["odds"])

    def test_fraicheur_par_book(self, sans_reseau):
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        assert books[1055]["updated"] == "2026-08-22 11:28:08"

    def test_panne_rend_dict_vide_jamais_dexception(self, sans_reseau):
        assert odds500.fetch_odds("1420317") == {}


class TestSelectionDesPrix:
    def test_pinnacle_prime_sur_lexchange(self, sans_reseau):
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        prix, nom = odds500.sharp_price(books)
        assert nom == "pinnacle"
        assert prix == [9.19, 5.14, 1.36]

    def test_lexchange_prend_le_relais_sans_pinnacle(self, sans_reseau):
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        del books[1055]
        _, nom = odds500.sharp_price(books)
        assert nom == "betfair_exchange"

    def test_pseudo_sharp_choisit_par_marge_mesuree_pas_par_liste(self, sans_reseau):
        """Le cahier des charges proposait {皇冠, Bet365, 澳门}. Mesuré sur ce
        match, 皇冠 porte 10,3 % et 澳门 11,1 % de marge sur le 1X2 : ce sont
        des books de handicap dont le 1X2 est décoratif. La sélection par
        marge les écarte d'elle-même."""
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        del books[1055], books[18]                   # plus de vrai sharp
        prix, detail = odds500.pseudo_sharp_price(books)
        assert prix is None                          # il ne reste pas 3 books crédibles
        assert "crown" not in detail["panel"]
        assert "macau" not in detail["panel"]

    def test_pseudo_sharp_applique_bien_une_penalite(self, sans_reseau):
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        prix, detail = odds500.pseudo_sharp_price(books)
        assert prix is not None
        assert detail["penalty_pct"] == odds500.PSEUDO_PENALTY_PCT
        # La pénalité GONFLE le prix de référence, donc réduit l'edge affiché.
        from core.source_adapter import vig_pct
        assert vig_pct(prix) < 0        # somme des probas < 1 après pénalité

    def test_moins_de_trois_books_credibles_rend_none(self, sans_reseau):
        sans_reseau["ouzhi-"] = PAGE_COTES
        books = odds500.fetch_odds("1420317")
        for cid in (18, 3, 280, 5):
            books.pop(cid, None)
        prix, detail = odds500.pseudo_sharp_price(books)
        assert prix is None
        assert "il en faut 3" in detail["reason"]


class TestBudget:
    def test_budget_epuise_court_circuite_le_cycle(self, monkeypatch):
        from core import daily_quota
        monkeypatch.setattr(daily_quota, "spent",
                            lambda b: odds500.DAILY_BUDGET + 1)
        appels = []
        monkeypatch.setattr(odds500, "_get", lambda u: appels.append(u))
        assert odds500.fetch_matches() == []
        assert appels == []            # pas une seule requête


# robots.txt d'odds.500.com, recopié le 2026-08-22 (extrait pertinent).
# Le garder ICI en dur est délibéré : si quelqu'un ajoute un jour un paramètre
# à un endpoint, le test doit échouer sur la RÈGLE PUBLIÉE, pas sur une
# reformulation de cette règle par le code de production.
ROBOTS_ODDS500 = """
User-agent: *
Disallow: /js/
Disallow: /static/
Disallow: /images/
Disallow: /fenxi1/
Disallow: /_index.php
Disallow: /shuju-*.shtml
Disallow: /ouzhi-*.shtml
Disallow: /yazhi-*.shtml
Disallow: /daxiao-*.shtml
Disallow: /fenxi/ouzhi-*.shtml?ctype=*
Disallow: /fenxi/yazhi-*.shtml?ctype=*
Disallow: /fenxi/daxiao-*.shtml?ctype=*
Disallow: /fenxi/ouzhi-*.shtml?order=*
Disallow: /fenxi/ouzhi-*.shtml?cids=*
Disallow: /fenxi/ouzhi.php?id=*
Disallow: /fenxi/ouzhi_same.php?cid=*
Disallow: /fenxi/*?ctype=
Disallow: /*?date=
Disallow: /*?type=
"""


def _disallow_rules(robots: str) -> list:
    return [ln.split(":", 1)[1].strip()
            for ln in robots.splitlines()
            if ln.lower().startswith("disallow:") and ln.split(":", 1)[1].strip()]


def _matches(rule: str, path: str) -> bool:
    """Comparaison RFC 9309 : le motif est ancré au DÉBUT du chemin, `*` est
    un joker. C'est cette ancre qui fait que `/ouzhi-*.shtml` n'attrape pas
    `/fenxi/ouzhi-1420317.shtml`."""
    import re as _re
    pattern = "".join(".*" if c == "*" else _re.escape(c) for c in rule)
    return _re.match(pattern, path) is not None


def _allowed(url: str) -> bool:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return not any(_matches(r, path) for r in _disallow_rules(ROBOTS_ODDS500))


class TestRobotsTxt:
    """Conformité au robots.txt PUBLIÉ, pas à l'idée qu'on s'en fait."""

    def test_les_endpoints_utilises_sont_autorises(self):
        for url in (odds500.FIXTURES_URL,
                    odds500.OUZHI_URL.format(fid=1420317),
                    odds500.YAZHI_URL.format(fid=1420317),
                    odds500.DAXIAO_URL.format(fid=1420317)):
            assert _allowed(url), url

    def test_la_query_string_est_la_frontiere(self):
        """Le cœur de la doctrine titan007, vérifié sur le texte réel : le
        même chemin bascule sous un Disallow dès qu'on lui ajoute un
        paramètre. C'est ce qui interdit d'« optimiser » un endpoint en lui
        passant un filtre."""
        nu = odds500.OUZHI_URL.format(fid=1420317)
        assert _allowed(nu)
        for param in ("?ctype=1", "?order=1", "?cids=3"):
            assert not _allowed(nu + param), param

    def test_le_motif_racine_nattrape_pas_le_chemin_fenxi(self):
        # `/ouzhi-*.shtml` est ancré à la racine : il vise les vieilles URL
        # `odds.500.com/ouzhi-123.shtml`, pas `/fenxi/ouzhi-123.shtml`.
        assert not _allowed("https://odds.500.com/ouzhi-1420317.shtml")
        assert _allowed("https://odds.500.com/fenxi/ouzhi-1420317.shtml")

    def test_le_lien_fenxi1_de_la_page_ne_doit_jamais_etre_suivi(self):
        # Présent dans chaque ligne de la table de cotes, et interdit.
        assert not _allowed(
            "https://odds.500.com/fenxi1/ouzhi_same.php?cid=1055&fixtureid=1420317")

    def test_aucun_endpoint_du_module_ne_porte_de_query_string(self):
        for url in (odds500.FIXTURES_URL, odds500.OUZHI_URL,
                    odds500.YAZHI_URL, odds500.DAXIAO_URL):
            assert "?" not in url and "&" not in url


class TestDoctrineDesEndpoints:
    def test_seul_lhote_odds_est_utilise(self):
        """Un seul hôte = un seul robots.txt à respecter et un seul
        comportement de WAF à connaître. live.500.com et www.500.com
        n'apportent rien de plus et sortent du périmètre."""
        for url in (odds500.FIXTURES_URL, odds500.OUZHI_URL,
                    odds500.YAZHI_URL, odds500.DAXIAO_URL):
            assert url.startswith("https://odds.500.com")

    def test_user_agent_ascii_pur(self):
        """urllib encode les en-têtes en latin-1 : un accent dans le
        User-Agent fait rendre 403 à Cloudflare (constaté sur Polymarket)."""
        odds500._UA.encode("ascii")

    def test_cadence_conforme(self):
        """≤ 1 requête / 2 s, comme annoncé dans le User-Agent."""
        assert odds500.REQUEST_DELAY >= 2.0
