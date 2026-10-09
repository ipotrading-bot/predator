"""
tests/test_ordre_de_depense.py — QUI est payé quand le budget ne suffit pas
(2026-09-17).

Le plafond d'un scan de fond (`allocation × créneaux dus/8 ×
BACKGROUND_SHARE`) refuse tous les jours des ligues qui ont des matchs : le
scan de 19:11 UTC le 2026-09-17 a sauté EPL, Bundesliga, Serie A et Ligue 1.
C'est donc l'ORDRE de paiement qui décide où part l'argent, et il était trié
par NOMBRE DE MATCHS — MLB (9 matchs, 2 crédits) passait devant La Liga
(2 matchs, 3 crédits), donc devant le seul segment que le ledger démontre.

Deux décisions à garder :
  1. l'ordre suit la PRIORITÉ DÉCLARÉE de SPORT_KEYS, le volume ne
     départageant qu'à rang égal ;
  2. le baseball est retiré du scan payant (décision opérateur du
     2026-09-17) — sans rien effacer : ses lignes restent réglables.
"""
import run_engine
from core import odds_api, scan_windows
from core.odds_api import SPORT_KEYS, LIGUES_RETIREES, LIGUES_EN_ESSAI
from core.score_sources import sports_reglables


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"x-requests-remaining": "400", "x-requests-used": "100"}

    def json(self):
        return self._payload


def _wire(monkeypatch, events_par_ligue):
    """fetch_odds sans réseau (même montage que test_odds_api_preflight) : le
    pré-vol gratuit rend les matchs déclarés, l'appel payant note son ordre."""
    payes = []

    def fake_get(url, params=None, timeout=None):
        if url.rstrip("/").endswith("/sports"):
            return _Resp([])                    # sonde gratuite du pool de clés
        cle = url.split("/sports/")[1].split("/")[0]
        if "/events" in url:
            return _Resp([{"id": i} for i in range(events_par_ligue.get(cle, 0))])
        payes.append(cle)
        return _Resp([])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    return payes


class TestLordreDePaiement:
    def test_la_priorite_declaree_passe_avant_le_volume(self, monkeypatch):
        """Le cas réel du 2026-09-17 : une ligue à 9 matchs contre une ligue
        du Big 5 à 2 matchs. C'est le Big 5 qui doit être payé d'abord."""
        payes = _wire(monkeypatch, {"soccer_spain_la_liga": 2, "soccer_mexico_ligamx": 9})
        odds_api.fetch_odds(api_key="k", hours_ahead=24, sport_keys={
            "soccer_spain_la_liga": "soccer", "soccer_mexico_ligamx": "soccer"})
        assert payes == ["soccer_spain_la_liga", "soccer_mexico_ligamx"]

    def test_le_volume_departage_dans_la_famille(self, monkeypatch):
        """Dans une famille, la logique d'avant reste : si un 422 coupe le
        scan, il coupe sur la ligue la moins fournie."""
        payes = _wire(monkeypatch, {"soccer_epl": 1, "soccer_italy_serie_a": 3})
        odds_api.fetch_odds(api_key="k", hours_ahead=24, sport_keys={
            "soccer_epl": "soccer", "soccer_italy_serie_a": "soccer"})
        assert payes == ["soccer_italy_serie_a", "soccer_epl"]

    def test_les_familles_sont_derivees_de_lordre_declare(self):
        """Huit familles, chaque frontière existe, et le rang est croissant
        dans l'ordre de déclaration — sinon le préambule mentirait."""
        rangs = odds_api.rangs_par_famille()
        for cle in odds_api.DEBUTS_DE_FAMILLE:
            assert cle in SPORT_KEYS, cle
        assert rangs["soccer_epl"] == 0
        assert sorted(set(rangs.values())) == list(range(len(odds_api.DEBUTS_DE_FAMILLE)))
        suite = [rangs[c] for c in SPORT_KEYS]
        assert suite == sorted(suite), "une famille est déclarée en deux morceaux"

    def test_une_cle_inconnue_du_dictionnaire_passe_en_dernier(self, monkeypatch):
        """Les clés de tennis sont dynamiques (fusionnées au scan) : elles ne
        doivent pas préempter une ligue déclarée."""
        payes = _wire(monkeypatch, {"soccer_epl": 1, "tennis_atp_tokyo": 20})
        odds_api.fetch_odds(api_key="k", hours_ahead=24, sport_keys={
            "soccer_epl": "soccer", "tennis_atp_tokyo": "tennis"})
        assert payes[0] == "soccer_epl"


class TestLeBig5EstPremier:
    """La décision du 2026-09-17, mesurée : Big 5 recommandé+jouable post-A6
    = 25-5, +7,14 u APRÈS la taxe de 20 %, borne basse de Wilson au-dessus du
    point mort — le seul segment du livre dans ce cas. Hors Big 5 : −3,11 u."""

    def test_le_big5_precede_les_autres_familles(self):
        rangs = {cle: i for i, cle in enumerate(SPORT_KEYS)}
        for big5 in ("soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
                     "soccer_italy_serie_a", "soccer_france_ligue_one"):
            for autre in ("soccer_mexico_ligamx", "soccer_brazil_campeonato",
                          "basketball_nba", "americanfootball_nfl",
                          "mma_mixed_martial_arts"):
                assert rangs[big5] < rangs[autre], f"{big5} doit précéder {autre}"


class TestOnNePaieQueCeQuonSaitRegler:
    """L'invariant qui rend la classe de bug impossible : un sport acheté
    sans voie de règlement est un crédit perdu deux fois (on paie la cote,
    puis on ne saura jamais si le pari était bon).

    MESURÉ le 2026-09-17 : `boxing_boxing` vivait dans SPORT_KEYS alors
    qu'ESPN n'a aucun chemin de boxe (`boxing/boxing` → HTTP 400). La
    politique de dépense la refusait déjà — donc 0 crédit et 0 signal — mais
    la liste mentait, et un pré-vol gratuit partait à chaque scan. Retirée."""

    def test_chaque_sport_achete_a_une_voie_de_reglement(self):
        payes = odds_api.sports_payes()
        assert payes <= sports_reglables(), payes - sports_reglables()

    def test_la_boxe_est_sortie_et_dit_pourquoi(self):
        assert "boxing_boxing" not in SPORT_KEYS
        assert "boxing" not in odds_api.sports_payes()
        assert "boxing" not in sports_reglables(), \
            "si une source de boxe apparaît, c'est ce test qui doit tomber"
        assert LIGUES_RETIREES["boxing_boxing"].startswith("2026-")

    def test_la_liste_des_sports_payes_est_derivee(self):
        """Règle 6 : `sports_payes()` ne doit pas être une seconde liste."""
        import inspect
        src = inspect.getsource(odds_api.sports_payes)
        assert "SPORT_KEYS.values()" in src


class TestBaseballRetire:
    def test_aucune_cle_baseball_dans_le_scan_payant(self):
        assert not [k for k in SPORT_KEYS if k.startswith("baseball")]
        assert {"baseball_mlb", "baseball_kbo", "baseball_npb"} <= set(LIGUES_RETIREES)
        for motif in LIGUES_RETIREES.values():
            assert motif.startswith("2026-"), "une ligue retirée porte sa date"
        # Une ligue retirée ne peut pas être à la fois retirée et achetée.
        assert not (set(LIGUES_RETIREES) & set(SPORT_KEYS))

    def test_aucun_pari_baseball_nest_plus_recommande(self):
        """Le retrait des clés ne suffit pas : les sources soft peuvent encore
        présenter un match. Le fantôme, lui, couvre toutes les sources."""
        assert "baseball" in run_engine.SHADOW_SPORTS
        assert run_engine._shadow_reason({"sport": "baseball"}) == "shadow_sport"

    def test_les_lignes_baseball_restent_reglables(self):
        """Règle 9 : on archive, on ne supprime pas. Les 41 lignes réglées
        restent lisibles, et les signaux encore ouverts doivent pouvoir se
        régler — MLB statsapi, sans clé."""
        assert "baseball" in sports_reglables()
        assert run_engine._reglable({"sport": "baseball", "match": "Cubs vs Mets"}, {}) is True

    def test_les_fenetres_conservees_ne_sont_plus_dans_sport_keys(self):
        """Les fenêtres favorables du baseball sont gardées pour une
        réouverture : ce test empêche l'annotation de pourrir (une fenêtre
        conservée sans ligue retirée, ou l'inverse)."""
        inertes = {k for k in scan_windows._WINDOWS if k not in SPORT_KEYS}
        assert inertes == set(LIGUES_RETIREES), inertes ^ set(LIGUES_RETIREES)


class TestWnbaRetiree:
    """Décision opérateur du 2026-09-24 : WNBA suspendue définitivement."""

    def test_wnba_nest_plus_achetee(self):
        assert "basketball_wnba" not in SPORT_KEYS
        assert "basketball_wnba" in LIGUES_RETIREES
        # La NBA partage le sport-type : elle, reste achetée.
        assert "basketball_nba" in SPORT_KEYS

    def test_les_lignes_wnba_restent_reglables(self):
        """Règle 9 : les signaux WNBA encore ouverts se règlent toujours."""
        assert "basketball" in sports_reglables()


class TestArgentineRetiree:
    """Décision opérateur du 2026-10-09 : la Primera División argentine,
    exclue du périmètre depuis le 2026-09-08, n'est plus achetée non plus."""

    def test_argentine_nest_plus_achetee(self):
        assert "soccer_argentina_primera_division" not in SPORT_KEYS
        assert "soccer_argentina_primera_division" in LIGUES_RETIREES
        # Le reste de l'Amérique du Sud, lui, reste acheté.
        assert "soccer_brazil_campeonato" in SPORT_KEYS

    def test_les_lignes_argentines_restent_reglables(self):
        """Règle 9 : les lignes passées se lisent et se règlent toujours."""
        assert "soccer" in sports_reglables()


class TestLiguesEnEssai:
    """Règle 13 (AUDIT.md §3bis) : une ligue n'entre qu'avec un budget chiffré,
    un critère de retrait DATÉ et un gardien. Cinq sont entrées le
    2026-09-22, le jour où la mesure a montré que 10 des 11 ligues de foot
    scannées n'avaient aucun match de la semaine (trêve internationale) ;
    sept le 2026-10-01 (voir TestSecondLotDu1erOctobre)."""

    def test_chaque_ligue_en_essai_est_reellement_achetee(self):
        """Un registre d'essai qui nomme une ligue qu'on n'achète pas est un
        mensonge — c'est exactement ce que la boxe faisait avant son retrait."""
        for cle in LIGUES_EN_ESSAI:
            assert cle in SPORT_KEYS, cle
            assert cle not in LIGUES_RETIREES, cle

    def test_chaque_essai_porte_son_budget_et_sa_date_de_retrait(self):
        for cle, motif in LIGUES_EN_ESSAI.items():
            assert motif.startswith("2026-"), f"{cle} : pas de date d'entrée"
            assert "créd/j" in motif, f"{cle} : pas de budget chiffré"
            assert "retrait le 2026-" in motif, f"{cle} : pas de critère daté"

    def test_une_ligue_en_essai_a_sa_fenetre_de_depense(self):
        """L'invariant qui casse le plus souvent (règle 6) : une clé présente
        dans SPORT_KEYS mais absente de `_WINDOWS` n'est jamais achetée qu'en
        scan de fond — donc à peu près jamais, et en silence."""
        for cle in LIGUES_EN_ESSAI:
            assert cle in scan_windows._WINDOWS, cle
            assert scan_windows._WINDOWS[cle], cle

    def test_les_essais_ne_preemptent_aucune_famille_mesuree(self):
        """Elles n'ont AUCUNE ligne au ledger : elles passent en dernier, et
        un jour saturé les refuse avant le Big 5 (2026-09-19 : 24 refus)."""
        rangs = odds_api.rangs_par_famille()
        dernier = max(rangs.values())
        for cle in LIGUES_EN_ESSAI:
            assert rangs[cle] == dernier, cle
        assert rangs["soccer_epl"] < dernier


class TestSecondLotDu1erOctobre:
    """Sept ligues ajoutées le 2026-10-01 (décision opérateur : « si elles
    tiennent dans le quota, ajoute-les »). MESURÉ avant l'ajout : la demande
    réelle tournait à ~90 crédits/jour pour ~145 d'allocation ; rejeu de la
    vraie politique de dépense sur les coups d'envoi réels du 2 au 15/10 →
    156 crédits la semaine de matchs pour les sept, pire jour à 82 % de son
    allocation, AUCUN achat en fenêtre favorable perdu par une ligue déjà
    scannée (INCIDENTS.md, « Le pool n'était pas la contrainte »)."""

    LOT = ("soccer_efl_champ", "soccer_netherlands_eredivisie", "soccer_belgium_first_div",
           "soccer_turkey_super_league", "soccer_germany_bundesliga2",
           "soccer_france_ligue_two", "soccer_italy_serie_b")

    def test_les_sept_sont_achetees_en_essai_et_datees(self):
        for cle in self.LOT:
            assert SPORT_KEYS[cle] == "soccer", cle
            assert LIGUES_EN_ESSAI[cle].startswith("2026-10-01"), cle
            assert "retrait le 2026-11-15" in LIGUES_EN_ESSAI[cle], cle

    def test_le_budget_annonce_est_celui_du_rejeu(self):
        """Somme des « ~N créd/j » du lot : 22,7, le chiffre du rejeu. Le
        gonfler ou l'oublier ferait mentir le registre (règle 13)."""
        import re
        total = sum(float(re.search(r"~([0-9.]+) créd/j", LIGUES_EN_ESSAI[c]).group(1))
                    for c in self.LOT)
        assert abs(total - 22.7) < 0.05, total

    def test_le_portugal_n_entre_pas_sans_book_d_execution(self):
        """`ops.py books soccer_portugal_primeira_liga` le 2026-10-01 : 0/9
        match avec 1xbet. Une ligue sans book d'exécution ne s'achète pas."""
        assert "soccer_portugal_primeira_liga" not in SPORT_KEYS

    def test_les_fenetres_suivent_les_coups_d_envoi_releves(self):
        from datetime import datetime, timezone
        fav = scan_windows.is_favorable

        def t(jour, heure):                      # octobre 2026 : le 9 est un vendredi
            return datetime(2026, 10, jour, heure, 3, tzinfo=timezone.utc)
        # Championship : samedi 11h30 → le créneau de 06:03 est le seul avant T-2h30.
        assert fav("soccer_efl_champ", t(10, 6)) and fav("soccer_efl_champ", t(10, 11))
        assert not fav("soccer_efl_champ", t(10, 19))
        # 2. Bundesliga : samedi 11h → 06:03 ; 09:03 serait sous T-2h30.
        assert fav("soccer_germany_bundesliga2", t(10, 6))
        assert not fav("soccer_germany_bundesliga2", t(10, 9))
        assert fav("soccer_germany_bundesliga2", t(9, 11))        # vendredi 16h30
        # Ligue 2 : cinq matchs le vendredi à 18h → 13:03.
        assert fav("soccer_france_ligue_two", t(9, 13))
        assert not fav("soccer_france_ligue_two", t(11, 9))       # rien le dimanche
        # Turquie : 17h en semaine → 11:03 et 13:03, pas 16:03 (sous T-2h).
        assert fav("soccer_turkey_super_league", t(12, 11))
        assert not fav("soccer_turkey_super_league", t(12, 16))

    def test_le_lot_reste_derriere_toutes_les_familles_mesurees(self):
        rangs = odds_api.rangs_par_famille()
        for cle in self.LOT:
            assert rangs[cle] == max(rangs.values()), cle
            assert rangs[cle] > rangs["soccer_brazil_campeonato"] > rangs["soccer_epl"]
