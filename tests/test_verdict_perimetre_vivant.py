"""
tests/test_verdict_perimetre_vivant.py — le verdict par sport juge le
périmètre que le scan émet AUJOURD'HUI (2026-11-17).

Mesuré ce jour-là sur le ledger (zone 2-24 h, post-époque, mise plate) : le
verdict « basketball » disait 94 réglés, réussite 50,0 % [IC95 40–60] pour un
point mort à 51,9 %, −2,78 u. 17 lignes de présaison NBA (coupée le 10-09), 5
de WNBA (retirée le 09-24) et 1 de NCAA en faisaient partie ; sans elles : 72
réglés, 54,2 % [43–65] pour 51,7 %, +3,14 u. L'opérateur a lu « la NBA perd ».
Gardés ici :
  1. une seule définition du périmètre par libellé, lue par le scan ET par le
     verdict (règle 6) ;
  2. ce qui est DIT sort ces lignes ; ce qui est APPLIQUÉ (plafonds, seuils,
     ordre des sports) les garde — sans quoi le plafond d'edge du basket
     tomberait sans mesure (règle 10) ;
  3. le rapport hebdo coupe au même endroit.
"""
import inspect

import run_engine as eng
from core import learning_layer as ll
from core import source_adapter as sa
from scripts import weekly_report as wr

MOTIFS = ("primera lpf", "wnba", "ncaa")


class TestPerimetreParLibelle:
    def test_la_presaison_nba_sort(self):
        assert sa.hors_perimetre_vivant("basketball", "USA - NBA Preseason", ()) == "présaison NBA"
        assert sa.hors_perimetre_vivant("basketball", "NBA Pre-Season", ()) == "présaison NBA"

    def test_la_saison_reguliere_reste(self):
        for lib in ("NBA", "USA - NBA"):
            assert sa.hors_perimetre_vivant("basketball", lib, MOTIFS) == ""

    def test_wnba_nest_pas_le_mot_nba(self):
        """Le motif « wnba » ne sort pas la NBA, et la WNBA ne sort que par
        son motif opérateur."""
        assert sa.hors_perimetre_vivant("basketball", "WNBA", ()) == ""
        assert "wnba" in sa.hors_perimetre_vivant("basketball", "WNBA", MOTIFS)
        assert not sa.ligue_nba("basketball", "WNBA")

    def test_la_presaison_nfl_reste(self):
        """Décision opérateur du 2026-10-06 : la présaison NFL est acceptée."""
        assert sa.hors_perimetre_vivant("americanfootball", "NFL Preseason", MOTIFS) == ""

    def test_un_libelle_vide_ne_sort_pas(self):
        assert sa.hors_perimetre_vivant("basketball", None, MOTIFS) == ""
        assert sa.hors_perimetre_vivant("soccer", "", MOTIFS) == ""

    def test_les_motifs_se_lisent_comme_au_scan(self):
        assert sa.motifs_ligues("primera lpf; wnba\nncaa;;") == MOTIFS
        assert sa.motifs_ligues(None) == () and sa.motifs_ligues("") == ()

    def test_une_seule_definition(self):
        assert eng.LIGUES_EXCLUES_META_KEY is sa.LIGUES_EXCLUES_META_KEY
        src = inspect.getsource(eng._nba_hors_saison)
        assert "_presaison_nba_par_libelle(" in src and "re.compile" not in src
        assert "_motifs_ligues(" in inspect.getsource(eng._ligues_exclues)
        assert not hasattr(eng, "_LIBELLE_PRESAISON")


def _l(league, outcome="WIN"):
    return {"sport": "basketball", "league": league, "outcome": outcome, "odds": 1.93,
            "kelly_pct": 0.5, "sharp_prob": 0.53, "time_to_match_minutes": 240}


class TestCeQuiEstDit:
    def test_les_lignes_hors_perimetre_sortent(self):
        rows = [_l("NBA"), _l("USA - NBA"), _l("USA - NBA Preseason", "LOSS"),
                _l("WNBA", "LOSS"), _l("USA - NCAA", "LOSS"), _l(None)]
        gardees = ll.lignes_du_perimetre(rows, MOTIFS)
        assert [r["league"] for r in gardees] == ["NBA", "USA - NBA", None]
        assert ll._sport_stats(gardees)["hit_rate"] == 1.0

    def test_sans_motif_seule_la_presaison_sort(self):
        rows = [_l("NBA"), _l("USA - NBA Preseason"), _l("WNBA")]
        assert len(ll.lignes_du_perimetre(rows, ())) == 2

    def test_le_ledger_est_lu_avec_sa_ligue(self):
        assert "league" in ll._LEDGER_SELECT

    def test_motifs_illisibles_rien_nest_exclu(self):
        class _SB:
            def table(self, _t):
                raise RuntimeError("réseau")
        assert ll.lire_motifs_exclus(_SB()) == ()

    def test_les_motifs_viennent_de_la_cle_du_scan(self):
        vu = {}

        class _Q:
            def select(self, *_a): return self
            def eq(self, col, val): vu[col] = val; return self
            def limit(self, *_a): return self
            def execute(self): return type("R", (), {"data": [{"value": "wnba;ncaa"}]})()

        class _SB:
            def table(self, t): vu["table"] = t; return _Q()
        assert ll.lire_motifs_exclus(_SB()) == ("wnba", "ncaa")
        assert vu == {"table": "meta", "key": sa.LIGUES_EXCLUES_META_KEY}


class TestCeQuiEstAppliqueNeBougePas:
    """Règle 10 : restreindre aussi les plafonds ferait tomber celui du basket
    (5 lignes de saison régulière au-dessus de 4 %, 10 exigées) — un changement
    d'émission sans mesure. Les refus du plafond sont mesurés en fantôme
    (tests/test_plafond_en_fantome.py) : ce sont eux qui le jugeront."""
    SRC = inspect.getsource(ll.compute_and_save)

    def test_le_verdict_lit_le_perimetre_vivant(self):
        assert "dits = lignes_du_perimetre(appliquables, motifs_exclus)" in self.SRC
        assert "verdict_stats[sport] = _sport_stats(dits)" in self.SRC
        assert "_save_sport_verdicts(sb, verdict_stats, clv_by_sport" in self.SRC
        assert "calibration_par_tranche(dits)" in self.SRC

    def test_plafonds_seuils_et_classement_gardent_toutes_les_lignes(self):
        assert "_top_band_verdict(appliquables)" in self.SRC
        assert "_odds_band_verdict(appliquables)" in self.SRC
        assert "_verdict_seuil(appliquables, fantomes, derive_ref)" in self.SRC
        assert "ranking_stats[sport] = stats" in self.SRC
        assert "_save_sport_ranking(sb, ranking_stats, now)" in self.SRC

    def test_le_rapport_hebdo_coupe_au_meme_endroit(self):
        src = inspect.getsource(wr.main)
        assert "lignes_du_perimetre(res.data or [], motifs_exclus)" in src
        assert "lire_motifs_exclus(sb)" in src
