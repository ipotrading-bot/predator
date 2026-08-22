"""
tests/test_source_adapter.py — appariement sans les noms, et détection de
divergence.

CE QUI EST VÉRIFIÉ ICI
----------------------
Les deux propriétés dont dépend tout le multilingue :

  1. deux sources qui écrivent le même match dans deux langues différentes
     s'apparient quand même — par le coup d'envoi, la ligue et la STRUCTURE
     des cotes, jamais par le libellé ;
  2. deux chemins vers le même prix de référence qui divergent trop
     déclenchent SUSPECT_DATA, donc aucun signal.

Les valeurs de cotes utilisées ne sont pas inventées : ce sont les prix
réellement relevés le 2026-08-22 sur 赫尔城VS曼联 (Hull City–Manchester
United) chez 500.com et Polymarket. Un test bâti sur des nombres ronds
passerait aussi bien avec un seuil faux.
"""
import pytest

from core import source_adapter as sa
from core.source_adapter import Fixture

# Prix RÉELS du 2026-08-22, Hull City vs Manchester United (英超 / EPL).
PINNACLE_500   = [9.19, 5.14, 1.36]     # odds.500.com cid=1055, marge 3,87 %
BETFAIR_500    = [10.0, 5.40, 1.39]     # odds.500.com cid=18,  marge 0,46 %
BET365_500     = [9.50, 5.00, 1.33]     # odds.500.com cid=3,   marge 5,71 %
POLYMARKET     = [1 / 0.095, 1 / 0.185, 1 / 0.715]


class TestSignaturesSansLangue:
    def test_le_devig_somme_a_un(self):
        p = sa.novig_probs(PINNACLE_500)
        assert len(p) == 3
        assert sum(p) == pytest.approx(1.0)

    def test_la_marge_identifie_le_type_de_book(self):
        # C'est la signature qui a permis de cartographier les books dont
        # 500.com masque le nom : un exchange vit sous 1 %, Pinnacle autour de
        # 4 %, un book soft au-dessus de 5 %.
        assert sa.vig_pct(BETFAIR_500) < 1.0
        assert 2.0 < sa.vig_pct(PINNACLE_500) < 5.0
        assert sa.vig_pct(BET365_500) > 5.0

    def test_prix_invalides_ne_rendent_pas_une_signature_vide_trompeuse(self):
        assert sa.novig_probs([0.0, 3.0, 2.0]) == []
        assert sa.novig_probs([]) == []


class TestDivergenceEtSuspectData:
    def test_trois_hotes_independants_sont_daccord(self):
        # La mesure qui a validé la carte des books de 500.com : Pinnacle et
        # Betfair vus par 500.com tombent à ~1 point de Polymarket, un hôte
        # qui n'a rien à voir.
        assert sa.divergence_pts(PINNACLE_500, POLYMARKET) < 1.5
        assert sa.divergence_pts(BETFAIR_500, POLYMARKET) < 1.5

    def test_un_prix_perime_declenche_suspect_data(self):
        # Même match, mais un des chemins porte un prix nettement décalé :
        # c'est la signature d'un prix périmé quelque part, et on ne sait pas
        # lequel. Aucun signal ne doit sortir de là.
        perime = [6.00, 4.20, 1.62]
        ok, worst, detail = sa.cross_check(
            {"odds_api": PINNACLE_500, "odds500": perime})
        assert ok is False
        assert worst > sa.SUSPECT_DIVERGENCE_PTS
        assert set(detail["worst_pair"]) == {"odds_api", "odds500"}

    def test_sources_concordantes_passent(self):
        ok, worst, _ = sa.cross_check(
            {"odds500": PINNACLE_500, "polymarket": POLYMARKET,
             "matchbook": BETFAIR_500})
        assert ok is True
        assert 0 <= worst <= sa.SUSPECT_DIVERGENCE_PTS

    def test_un_seul_chemin_nest_pas_une_confirmation(self):
        # -1.0 et non 0.0 : « rien à croiser » ne doit jamais se lire comme
        # « accord parfait ».
        ok, worst, detail = sa.cross_check({"odds500": PINNACLE_500})
        assert ok is True
        assert worst == -1.0
        assert detail["compared"] == 0

    def test_le_seuil_relatif_du_cahier_des_charges_aurait_ete_inutilisable(self):
        # Justification écrite du choix « points absolus » plutôt que
        # « 3 % relatif » : sur l'outsider à 9,5 %, deux sources d'accord à
        # moins d'un point divergent de plus de 3 % en relatif.
        p_pin = sa.novig_probs(PINNACLE_500)
        p_pol = sa.novig_probs(POLYMARKET)
        relatif = abs(p_pin[0] - p_pol[0]) / p_pol[0] * 100
        absolu = abs(p_pin[0] - p_pol[0]) * 100
        assert relatif > 3.0            # un seuil relatif crierait au loup
        assert absolu < 1.5             # alors que l'écart réel est minuscule


class TestLangueEtLigue:
    @pytest.mark.parametrize("nom,attendu", [
        ("鹿岛鹿角", "zh"), ("曼彻斯特联", "zh"),
        ("鹿島アントラーズ", "ja"),
        ("전북현대모터스", "ko"),
        ("Manchester United", "en"), ("", "en"),
    ])
    def test_detection_de_langue(self, nom, attendu):
        assert sa.detect_lang(nom) == attendu

    def test_les_kana_tranchent_entre_japonais_et_chinois(self):
        # Du han seul reste du chinois ; un seul kana bascule en japonais.
        # Sans cette règle, tout nom japonais partirait sur le chemin de
        # résolution chinois.
        assert sa.detect_lang("鹿島鹿角") == "zh"
        assert sa.detect_lang("鹿島アントラーズ") == "ja"

    def test_les_deux_langues_pointent_la_meme_ligue(self):
        assert sa.league_key("英超") == sa.league_key("Premier League") == "epl"
        assert sa.league_key("日职") == "j_league"
        assert sa.league_key("Coupe Inconnue") == ""


def _fx(source, mid, kickoff, league, home, away, odds=None, ids=()):
    return Fixture(source=source, match_id=mid, kickoff=kickoff, league=league,
                   home=home, away=away, odds=odds or [], team_ids=ids)


class TestAppariementSansLesNoms:
    def test_chinois_et_anglais_sapparient(self):
        """LE test du cahier des charges : nom chinois ↔ nom canonique.

        Aucun des deux libellés n'est comparé à l'autre — l'appariement ne
        tient qu'au coup d'envoi, à la ligue mappée et à la structure.
        """
        zh = [_fx("odds500", "1420317", "2026-08-22T19:30:00Z", "英超",
                  "赫尔城", "曼联", PINNACLE_500, ("872", "1075"))]
        en = [_fx("sevenm", "5170001", "2026-08-22T19:30:00Z", "Premier League",
                  "Hull City", "Manchester United", BETFAIR_500)]
        pairs = sa.pair_fixtures(zh, en)
        assert len(pairs) == 1
        a, b, ev = pairs[0]
        assert (a.home, b.home) == ("赫尔城", "Hull City")
        assert (a.away, b.away) == ("曼联", "Manchester United")
        assert ev["league_key"] == "epl"
        assert ev["kickoff_delta_s"] == 0

    def test_tolerance_de_quinze_minutes(self):
        zh = [_fx("odds500", "1", "2026-08-22T19:30:00Z", "英超", "赫尔城", "曼联", PINNACLE_500)]
        proche = [_fx("sevenm", "2", "2026-08-22T19:42:00Z", "Premier League",
                      "Hull City", "Man Utd", BETFAIR_500)]
        loin = [_fx("sevenm", "3", "2026-08-22T21:00:00Z", "Premier League",
                    "Hull City", "Man Utd", BETFAIR_500)]
        assert len(sa.pair_fixtures(zh, proche)) == 1
        assert sa.pair_fixtures(zh, loin) == []

    def test_deux_ligues_connues_et_differentes_ne_sapparient_jamais(self):
        zh = [_fx("odds500", "1", "2026-08-22T19:30:00Z", "英超", "赫尔城", "曼联", PINNACLE_500)]
        en = [_fx("sevenm", "2", "2026-08-22T19:30:00Z", "La Liga", "X", "Y", PINNACLE_500)]
        assert sa.pair_fixtures(zh, en) == []

    def test_structures_incompatibles_ne_sapparient_pas(self):
        """Deux matchs de la même ligue à la même minute : seule la structure
        des cotes peut les départager. Un gros favori et un match équilibré ne
        sont pas le même match."""
        zh = [_fx("odds500", "1", "2026-08-22T19:30:00Z", "英超", "赫尔城", "曼联",
                  [9.19, 5.14, 1.36])]
        en = [_fx("sevenm", "2", "2026-08-22T19:30:00Z", "Premier League",
                  "A", "B", [2.60, 3.30, 2.70])]
        assert sa.pair_fixtures(zh, en) == []

    def test_sans_cotes_la_ligue_doit_concorder_des_deux_cotes(self):
        # Sans signature de cotes il ne reste que le temps : accepter sur ce
        # seul critère rendrait interchangeables deux matchs simultanés.
        zh = [_fx("odds500", "1", "2026-08-22T19:30:00Z", "英超", "赫尔城", "曼联")]
        connue = [_fx("sevenm", "2", "2026-08-22T19:30:00Z", "Premier League", "H", "M")]
        inconnue = [_fx("sevenm", "3", "2026-08-22T19:30:00Z", "Coupe Inconnue", "H", "M")]
        assert len(sa.pair_fixtures(zh, connue)) == 1
        assert sa.pair_fixtures(zh, inconnue) == []

    def test_chaque_fixture_nest_utilisee_quune_fois(self):
        zh = [_fx("odds500", "1", "2026-08-22T19:30:00Z", "英超", "A", "B", PINNACLE_500),
              _fx("odds500", "2", "2026-08-22T19:30:00Z", "英超", "C", "D", PINNACLE_500)]
        en = [_fx("sevenm", "9", "2026-08-22T19:30:00Z", "Premier League", "E", "F", PINNACLE_500)]
        pairs = sa.pair_fixtures(zh, en)
        assert len(pairs) == 1

    def test_deux_matchs_simultanes_sans_cotes_ne_sont_pas_tranches_au_hasard(self):
        """Régression d'une FAUSSE paire réelle du 2026-08-22.

        Sur 64 fixtures 500.com × 451 fixtures 7M, l'appariement rendait 16
        paires dont une fausse : 斯旺西/谢菲联 (Swansea/Sheffield Utd) apparié
        à Wrexham/Watford. Les deux matchs sont en EFL Championship à la MÊME
        minute, et les calendriers ne portent pas de cotes — le temps et la
        ligue ne distinguent rien, la structure est indisponible, et le tri
        glouton tranchait au hasard.

        Un alias faux se propage à vie dans le dictionnaire. On préfère perdre
        les deux matchs.
        """
        zh = [_fx("odds500", "1", "2026-08-22T14:00:00Z", "英冠", "斯旺西", "谢菲联"),
              _fx("odds500", "2", "2026-08-22T14:00:00Z", "英冠", "米尔沃尔", "诺维奇")]
        en = [_fx("sevenm", "9", "2026-08-22T14:00:00Z", "EFL Championship",
                  "Wrexham A.F.C.", "Watford F.C."),
              _fx("sevenm", "8", "2026-08-22T14:00:00Z", "EFL Championship",
                  "Millwall F.C.", "Norwich City")]
        assert sa.pair_fixtures(zh, en) == []

    def test_un_seul_candidat_sans_cotes_reste_apparie(self):
        # Le garde d'ambiguïté ne doit pas casser le cas non ambigu, sinon il
        # coûterait tout le dictionnaire au lieu d'une paire douteuse.
        zh = [_fx("odds500", "1", "2026-08-22T14:00:00Z", "英冠", "西布罗姆", "伯恩利")]
        en = [_fx("sevenm", "9", "2026-08-22T14:00:00Z", "EFL Championship",
                  "West Bromwich Albion F.C.", "Burnley F.C.")]
        assert len(sa.pair_fixtures(zh, en)) == 1

    def test_les_cotes_departagent_ce_que_le_temps_ne_departage_pas(self):
        """Quand la STRUCTURE est disponible, deux matchs simultanés de la
        même ligue redeviennent distinguables — c'est tout l'intérêt du
        critère (c)."""
        zh = [_fx("odds500", "1", "2026-08-22T14:00:00Z", "英冠", "斯旺西", "谢菲联",
                  [1.50, 4.20, 6.50]),
              _fx("odds500", "2", "2026-08-22T14:00:00Z", "英冠", "米尔沃尔", "诺维奇",
                  [3.10, 3.40, 2.30])]
        en = [_fx("sevenm", "9", "2026-08-22T14:00:00Z", "EFL Championship",
                  "Millwall F.C.", "Norwich City", [3.05, 3.45, 2.32]),
              _fx("sevenm", "8", "2026-08-22T14:00:00Z", "EFL Championship",
                  "Swansea City", "Sheffield United", [1.52, 4.15, 6.40])]
        pairs = sa.pair_fixtures(zh, en)
        assert len(pairs) == 2
        appariés = {a.home: b.home for a, b, _ in pairs}
        assert appariés["斯旺西"] == "Swansea City"
        assert appariés["米尔沃尔"] == "Millwall F.C."

    def test_horodatage_illisible_ignore_la_fixture(self):
        zh = [_fx("odds500", "1", "pas une date", "英超", "A", "B", PINNACLE_500)]
        en = [_fx("sevenm", "2", "2026-08-22T19:30:00Z", "Premier League", "C", "D", PINNACLE_500)]
        assert sa.pair_fixtures(zh, en) == []


class TestScorecardEtModeOmbre:
    def _card(self, **kw):
        base = {"source": "odds500", "matched": 0, "errors": 0, "requests": 0,
                "divergence_samples": [], "median_divergence_pts": None,
                "shadow": True}
        base.update(kw)
        return base

    def test_la_mediane_ignore_un_prix_aberrant_isole(self):
        card = self._card()
        for d in [0.4, 0.5, 0.6, 0.7, 42.0]:
            card = sa.record_observation(card, divergence_pts_value=d, matched=1)
        assert card["median_divergence_pts"] == 0.6
        assert card["matched"] == 5

    def test_promotion_exige_cent_matchs_ET_faible_divergence(self):
        spec = sa.SourceSpec(name="odds500", role="consensus", trust=0.55,
                             daily_budget=400)
        peu = self._card(matched=40, median_divergence_pts=0.5)
        shadow, verdict, _ = sa.evaluate_promotion(peu, spec)
        assert shadow is True and verdict == "en ombre"

        divergente = self._card(matched=500, median_divergence_pts=5.0)
        shadow, verdict, _ = sa.evaluate_promotion(divergente, spec)
        assert shadow is True and verdict == "en ombre"

        bonne = self._card(matched=120, median_divergence_pts=0.9)
        shadow, verdict, detail = sa.evaluate_promotion(bonne, spec)
        assert shadow is False and verdict == "promue"
        assert "120" in detail["reason"]

    def test_une_source_promue_qui_derive_est_retrogradee(self):
        # Asymétrie voulue : 100 matchs pour monter, une dérive pour tomber.
        spec = sa.SourceSpec(name="odds500", role="consensus", trust=0.55,
                             daily_budget=400)
        derive = self._card(matched=500, median_divergence_pts=6.0, shadow=False)
        shadow, verdict, _ = sa.evaluate_promotion(derive, spec)
        assert shadow is True and verdict == "rétrogradée"

    def test_sans_mesure_aucune_promotion(self):
        spec = sa.SourceSpec(name="x", role="consensus", trust=0.5, daily_budget=1)
        shadow, verdict, _ = sa.evaluate_promotion(
            self._card(matched=9999), spec)
        assert shadow is True and verdict == "insuffisant"

    def test_une_source_en_ombre_pese_moitie_moins(self):
        spec = sa.SourceSpec(name="odds500", role="consensus", trust=0.6,
                             daily_budget=400)
        assert sa.effective_trust(self._card(shadow=True), spec) == 0.3
        assert sa.effective_trust(self._card(shadow=False), spec) == 0.6


class TestSourceSpec:
    def test_un_role_inconnu_est_refuse_a_la_construction(self):
        with pytest.raises(ValueError):
            sa.SourceSpec(name="x", role="magique", trust=0.5, daily_budget=1)

    def test_une_confiance_hors_bornes_est_refusee(self):
        with pytest.raises(ValueError):
            sa.SourceSpec(name="x", role="sharp", trust=1.5, daily_budget=1)
