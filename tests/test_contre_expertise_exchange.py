"""
tests/test_contre_expertise_exchange.py — PHASE A5.

`_enrich_from_exchange` faisait `continue` dès qu'un prix Pinnacle existait :
Matchbook n'était consulté que sur les matchs SANS prix sharp. Or api-sports
sert Pinnacle sur 100 % de ses matchs de football, et 100 % des signaux sont du
football — l'exchange était donc écarté PRÉCISÉMENT sur les matchs qui portent
les signaux. Câblé en bouche-trou, il ne pouvait pas faire le seul travail qui
compte : repérer un Pinnacle PÉRIMÉ, qui est la fabrique à faux edge.

Ce qui est gardé ici :

  · l'exchange est CONSULTÉ même quand Pinnacle existe (le `continue` est mort) ;
  · un désaccord au-delà du seuil REFUSE le match ENTIER, tous marchés confondus ;
  · un accord fait entrer l'exchange au CONSENSUS, sans jamais écraser Pinnacle
    — l'invariant de `capture_from_exchange` en dépend ;
  · la divergence se mesure en POINTS de probabilité, jamais en écart relatif ;
  · le bouche-trou d'origine continue de fonctionner à l'identique.
"""
import logging

import pytest

import run_engine as eng
from core.constants import EXCHANGE_DIVERGENCE_PTS
from core.paim_engine import (_CONSENSUS_SOURCES, _CONSENSUS_WEIGHTS,
                              _DEFAULT_WEIGHTS, calculate_consensus_price)

log = logging.getLogger("test")


def _mb(o1, ox, o2, source="matchbook"):
    return {"barcelona_real madrid": {
        "home": "Barcelona", "away": "Real Madrid",
        "1": o1, "X": ox, "2": o2, "_source": source}}


def _match(pin=None, **kw):
    m = {"match": "Barcelona vs Real Madrid",
         "home": "Barcelona", "away": "Real Madrid"}
    if pin is not None:
        m["odds_pinnacle"] = pin
    m.update(kw)
    return m


class TestLaDivergenceSeMesureEnPointsDeProbabilite:
    def test_deux_carnets_identiques_ne_divergent_pas(self):
        c = {"1": 2.10, "X": 3.60, "2": 3.40}
        assert eng._sharp_divergence_pts(c, dict(c)) == 0.0

    def test_lecart_est_absolu_pas_relatif(self):
        """Un seuil relatif crie au loup sur tout outsider et reste muet sur
        les favoris — or c'est sur le favori que le point de probabilité coûte
        le plus cher. Deux carnets qui déplacent le domicile de ~1 point
        doivent rendre ~1 point, que le favori soit court ou long."""
        court = eng._sharp_divergence_pts({"1": 1.20, "X": 6.50, "2": 15.0},
                                          {"1": 1.22, "X": 6.40, "2": 14.5})
        long_ = eng._sharp_divergence_pts({"1": 5.00, "X": 4.00, "2": 1.70},
                                          {"1": 5.30, "X": 4.00, "2": 1.68})
        assert court is not None and long_ is not None
        # Les deux restent de l'ordre du point, sans explosion d'échelle.
        assert court < 3.0 and long_ < 3.0

    def test_un_carnet_a_deux_voies_est_comparable(self):
        # Hors football : pas de nul, la dévigorisation se fait sur 2 issues.
        d = eng._sharp_divergence_pts({"1": 1.80, "X": 0.0, "2": 2.10},
                                      {"1": 1.85, "X": 0.0, "2": 2.05})
        assert d is not None and d > 0

    @pytest.mark.parametrize("carnet", [
        {"1": 0, "X": 3.6, "2": 3.4},
        {"1": 2.1, "X": 3.6, "2": 1.0},
        {},
    ])
    def test_un_carnet_illisible_ne_juge_pas(self, carnet):
        """None, pas 0.0 ni l'infini : une source illisible n'est PAS une
        source en désaccord. La confondre refuserait des matchs sains."""
        assert eng._sharp_divergence_pts({"1": 2.10, "X": 3.60, "2": 3.40},
                                         carnet) is None


class TestLaContreExpertiseARemplaceLeContinue:
    def test_lexchange_est_consulte_meme_avec_un_prix_pinnacle(self):
        m = _match(pin={"1": 2.05, "X": 3.50, "2": 3.55})
        eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log)
        assert "odds_exchange" in m, \
            "l'exchange doit être confronté à Pinnacle, plus sauté"

    def test_un_accord_fait_entrer_lexchange_au_consensus(self):
        m = _match(pin={"1": 2.05, "X": 3.50, "2": 3.55})
        assert eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log) == 0
        assert m["odds_exchange"] == {"1": 2.10, "X": 3.60, "2": 3.40}
        assert "_sharp_conflict" not in m

    def test_pinnacle_reste_la_reference_et_nest_jamais_ecrase(self):
        pin = {"1": 2.05, "X": 3.50, "2": 3.55}
        m = _match(pin=dict(pin))
        eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log)
        assert m["odds_pinnacle"] == pin

    def test_un_desaccord_marque_le_match_en_conflit(self):
        # Domicile à 2.05 contre 2.60 : bien au-delà du seuil.
        m = _match(pin={"1": 2.05, "X": 3.50, "2": 3.55})
        assert eng._enrich_from_exchange([m], _mb(2.60, 3.60, 2.90), log) == 0
        assert m["_sharp_conflict"]["pts"] > EXCHANGE_DIVERGENCE_PTS
        assert "odds_exchange" not in m
        assert m["odds_pinnacle"]["1"] == 2.05, "on refuse, on ne corrige pas"

    def test_le_seuil_est_configurable(self, monkeypatch):
        m = _match(pin={"1": 2.05, "X": 3.50, "2": 3.55})
        monkeypatch.setattr(eng, "_EXCHANGE_DIVERGENCE_PTS", 0.1)
        eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log)
        assert "_sharp_conflict" in m, \
            "à 0,1 pt, un écart de 0,89 pt doit devenir un conflit"

    def test_un_conflit_ne_compte_pas_comme_un_enrichissement(self):
        # Le compteur sert à décider si l'exchange a SERVI de source ; une
        # contre-expertise ne pose aucun prix.
        m = _match(pin={"1": 2.05, "X": 3.50, "2": 3.55})
        assert eng._enrich_from_exchange([m], _mb(2.60, 3.60, 2.90), log) == 0


class TestLeBoucheTrouFonctionneToujours:
    """Rôle 2 — inchangé. Sans lui, aucun edge n'est calculable sur les matchs
    d'odds-api.io, qui ne portent aucun prix sharp."""

    def test_un_match_sans_sharp_recoit_le_prix_de_lexchange(self):
        m = _match(odds_1xbet={"1": 2.30, "X": 3.40, "2": 3.10})
        assert eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log) == 1
        assert m["odds_pinnacle"] == {"1": 2.10, "X": 3.60, "2": 3.40}
        assert m["_exchange"] == "matchbook"
        assert "_sharp_conflict" not in m

    def test_un_prix_estime_par_lia_est_remplace_pas_contre_expertise(self):
        """Un prix `_estimated` n'est pas un avis sharp indépendant : le
        confronter reviendrait à demander à l'exchange s'il est d'accord avec
        une supposition. Il est REMPLACÉ."""
        m = _match(pin={"1": 2.00, "X": 3.30, "2": 3.80}, _estimated=True)
        assert eng._enrich_from_exchange([m], _mb(2.60, 3.60, 2.90), log) == 1
        assert m["odds_pinnacle"]["1"] == 2.60
        assert "_sharp_conflict" not in m
        assert "_estimated" not in m

    def test_un_match_inconnu_de_lexchange_est_laisse_intact(self):
        m = {"match": "Lorient vs Brest", "home": "Lorient", "away": "Brest"}
        assert eng._enrich_from_exchange([m], _mb(2.10, 3.60, 3.40), log) == 0
        assert "odds_pinnacle" not in m and "_sharp_conflict" not in m


class TestLeRefusPorteSurLeMatchEntier:
    """Le prix de RÉFÉRENCE est suspect : h2h, totals et spreads le sont donc
    tous les trois. Le filtre vit dans la boucle de dispatch, en un seul
    endroit, pour qu'aucun marché futur ne puisse s'y soustraire."""

    def test_la_boucle_de_dispatch_saute_un_match_en_conflit(self):
        import inspect
        src = inspect.getsource(eng.run)
        garde = src.index('_sharp_conflict')
        for marche in ("_process_h2h(", "_process_totals(", "_process_spreads("):
            assert garde < src.index(marche), \
                f"{marche} est évalué avant le garde de conflit sharp"

    def test_le_garde_precede_tous_les_marches_sans_exception(self):
        import inspect
        src = inspect.getsource(eng.run)
        # Un seul point de filtrage : s'il y en avait deux, l'un des deux
        # finirait par diverger.
        assert src.count('m.get("_sharp_conflict")') == 1


class TestLexchangeEstUneVraieSourceDeConsensus:
    """Avant A5 le « consensus » n'avait qu'UNE source active : `circa` et
    `cris` ne sont posés que par `core/odds_api.py`, obsolète, et `isn` n'est
    écrit nulle part. `calculate_consensus_price` recevait `{"pinnacle": prix}`
    et rendait ce prix inchangé."""

    def test_exchange_est_une_source_reconnue(self):
        assert "exchange" in _CONSENSUS_SOURCES
        assert "exchange" in _DEFAULT_WEIGHTS
        for sport, poids in _CONSENSUS_WEIGHTS.items():
            assert "exchange" in poids, sport

    def test_deux_sources_produisent_un_prix_intermediaire(self):
        prix, sources, volatile, score = calculate_consensus_price(
            {"pinnacle": 2.00, "exchange": 2.02}, "soccer")
        assert not volatile
        assert 2.00 < prix < 2.02
        assert sources["pinnacle"] and sources["exchange"]

    def test_pinnacle_pese_plus_que_lexchange(self):
        for sport, poids in _CONSENSUS_WEIGHTS.items():
            if sport == "basketball" or sport == "euroleague_basketball" \
                    or sport == "baseball":
                continue      # circa domine sur ces sports, par conception
            assert poids["pinnacle"] > poids["exchange"], sport

    def test_lexchange_ne_declenche_pas_le_garde_volatile(self):
        """Mesuré le 2026-08-27 : opposer Pinnacle à Matchbook dépasse la
        limite de CV dès 0,46 point de probabilité d'écart, c'est-à-dire sur
        presque tous les matchs. Si le CV jugeait cette paire, tout ce que la
        contre-expertise accepte serait aussitôt rejeté en VOLATILE — A5
        n'émettrait plus rien, et pour un motif qui nomme mal la cause."""
        for exch in (2.005, 2.02, 2.05, 2.10):
            _p, _s, volatile, _sc = calculate_consensus_price(
                {"pinnacle": 2.00, "exchange": exch}, "soccer")
            assert not volatile, exch

    def test_le_garde_volatile_mord_toujours_entre_bookmakers(self):
        """L'exemption ne vaut QUE pour l'exchange : trois books qui se
        contredisent restent un carnet aberrant."""
        _p, _s, volatile, _sc = calculate_consensus_price(
            {"pinnacle": 1.50, "circa": 1.80, "cris": 2.10}, "soccer")
        assert volatile

    def test_lexchange_est_exclu_du_juge_pas_du_vote(self):
        """Il ne participe pas au contrôle de divergence, mais il pèse bien
        dans le prix rendu — sinon l'ajouter n'aurait aucun effet."""
        sans, _s, _v, _sc = calculate_consensus_price({"pinnacle": 2.00}, "soccer")
        avec, _s, _v, _sc = calculate_consensus_price(
            {"pinnacle": 2.00, "exchange": 2.20}, "soccer")
        assert avec > sans

    def test_une_source_seule_rend_son_propre_prix(self):
        # Non-régression : le cas d'aujourd'hui, quand l'exchange n'apparie pas.
        prix, _s, volatile, score = calculate_consensus_price(
            {"pinnacle": 2.00}, "soccer")
        assert prix == 2.00 and not volatile and score == 100
