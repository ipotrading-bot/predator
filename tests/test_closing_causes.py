"""
tests/test_closing_causes.py — POURQUOI une capture de closing line a manqué
(2026-09-21).

CE QUE CES TESTS PROTÈGENT, ET LA MESURE QUI LES A FAIT ÉCRIRE.

Mesuré sur les lignes réglées post-A6 : 105/135 en zone jouable (77,8 %) et
73/142 en fantôme (51,4 %) portent un `clv_pct_real`. Donc ~36 % des lignes
réglées n'en portent AUCUN. Or le CLV converge ~3× plus vite que le résultat
(`core/learning_layer._CLV_MIN_SAMPLES` = 15 contre `_MIN_SAMPLES` = 20) et
c'est un critère de PREMIER rang dans `_decide_threshold` : une capture perdue
coûte plus cher qu'une ligne de résultat perdue. À 9,0 lignes réglées par jour,
la puissance statistique est LE facteur limitant du dépôt — pas les idées.

`core/closing_line.py` énumérait déjà quatre causes possibles dans ses logs et
n'en attribuait AUCUNE : on ne pouvait donc pas savoir laquelle corriger. Ces
tests gardent l'instrumentation qui répond, et surtout sa DISCIPLINE :

  1. on COMPTE, on ne devine pas — et une cause inventée LÈVE plutôt que de se
     perdre dans un compteur que personne ne relira ;
  2. l'instrumentation ne change AUCUNE capture (c'est de la mesure, pas une
     décision — règle 10) ;
  3. un compteur en panne ne fait échouer ni une capture ni un scan
     (convention du dépôt : les erreurs réseau/API ne crashent jamais) ;
  4. le compteur n'entre PAS dans le contrat de fin (`core/run_contract.py`) :
     un compteur cassé ne rend pas un run rouge, et un compteur qui écrit ne
     rend pas vert un run stérile ;
  5. le reset appartient à l'APPELANT, une fois par run : les deux passes
     (`capture_from_scan` puis `capture_from_exchange`) tournent dans le même
     run et un reset par passe ferait que la seconde effacerait la première.
"""
import inspect
from datetime import datetime, timezone

import pytest

import run_engine as eng
from core import closing_line as cl

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class _MetaSB:
    """Supabase minimal sur la seule table `meta` : rend une valeur existante,
    encaisse les upserts, et peut échouer sur commande."""

    def __init__(self, existant=None, casse_lecture=False, casse_ecriture=False):
        self.existant = existant
        self.casse_lecture = casse_lecture
        self.casse_ecriture = casse_ecriture
        self.upserts = []
        self.appels = 0

    def table(self, nom):
        assert nom == "meta", f"seule la table meta est attendue, vu {nom!r}"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, _col, valeur):
        self.cle = valeur
        return self

    def maybe_single(self):
        return self

    def upsert(self, payload, **_k):
        self._payload = payload
        return self

    def execute(self):
        self.appels += 1
        if hasattr(self, "_payload"):
            if self.casse_ecriture:
                raise RuntimeError("RLS 42501")
            self.upserts.append(self._payload)
            del self._payload
            return type("R", (), {"data": None})()
        if self.casse_lecture:
            raise RuntimeError("reseau")
        data = {"value": self.existant} if self.existant else None
        return type("R", (), {"data": data})()


@pytest.fixture(autouse=True)
def _remet_les_compteurs():
    cl.reset_causes()
    yield
    cl.reset_causes()


class TestCompteur:
    def test_une_cause_inventee_leve(self):
        """Un code inventé ne doit pas atterrir en base : il n'y aurait aucun
        moyen de s'apercevoir qu'on compte une chose qui n'existe pas."""
        with pytest.raises(ValueError):
            cl.note_cause("ligne_bizarre")

    def test_les_causes_nulles_sont_omises(self):
        cl.note_cause("ligne_bougee")
        assert cl.causes_courantes() == {"ligne_bougee": 1}

    def test_le_compte_agrege_sadditionne(self):
        cl.note_cause("sans_cote_exchange", 7)
        cl.note_cause("sans_cote_exchange", 3)
        assert cl.causes_courantes()["sans_cote_exchange"] == 10

    def test_le_reset_vide_tout(self):
        cl.note_cause("ligne_illisible")
        cl.reset_causes()
        assert cl.causes_courantes() == {}

    def test_causes_courantes_est_une_copie(self):
        """Muter le retour ne doit pas corrompre le compteur du run."""
        cl.note_cause("ligne_bougee")
        cl.causes_courantes()["ligne_bougee"] = 999
        assert cl.causes_courantes() == {"ligne_bougee": 1}


class TestDisciplineDuCode:
    def test_toute_cause_notee_est_declaree(self):
        """Règle n°6 : la liste des causes vit UNE fois. Tout `note_cause("x")`
        du module doit citer un membre de CAUSES_NON_CAPTURE."""
        src = inspect.getsource(cl)
        notees = set()
        for morceau in src.split('note_cause("')[1:]:
            notees.add(morceau.split('"')[0])
        assert notees, "aucun appel à note_cause trouvé — l'instrumentation a disparu"
        inconnues = notees - set(cl.CAUSES_NON_CAPTURE)
        assert not inconnues, f"causes notées mais non déclarées : {inconnues}"

    def test_toute_cause_declaree_est_notee(self):
        """L'inverse : une cause déclarée que personne ne compte est du code
        mort qui laisserait croire qu'on mesure quelque chose."""
        src = inspect.getsource(cl)
        notees = {m.split('"')[0] for m in src.split('note_cause("')[1:]}
        jamais = set(cl.CAUSES_NON_CAPTURE) - notees
        assert not jamais, f"causes déclarées jamais comptées : {jamais}"

    def test_le_reset_appartient_a_lappelant(self):
        """Un reset dans une passe effacerait le décompte de l'autre passe du
        même run. Les deux passes doivent seulement ACCUMULER."""
        for passe in (cl.capture_from_scan, cl.capture_from_exchange):
            assert "reset_causes" not in inspect.getsource(passe), (
                f"{passe.__name__} ne doit pas remettre les compteurs à zéro")
        assert "_reset_causes_closing()" in inspect.getsource(eng.run)

    def test_la_mesure_ne_change_aucune_capture(self):
        """L'instrumentation est posée AVANT un `return None` existant, jamais
        à la place : aucune capture ne doit dépendre du compteur."""
        src = inspect.getsource(cl)
        for morceau in src.split("note_cause(")[1:]:
            suite = morceau[:400]
            assert "update_signal_fields" not in suite.split("\n\n")[0], (
                "un note_cause ne doit jamais précéder une écriture de signal")

    def test_le_compteur_nentre_pas_dans_le_contrat_de_fin(self):
        """Règle : un run qui n'a pas fait son travail sort en ÉCHEC — mais le
        compteur n'est pas « son travail ». Il ne doit pas peser sur le verdict."""
        src = inspect.getsource(eng.run)
        appel = src[src.index("verdict_de_fin("):]
        assert "causes" not in appel.split(")")[0]

    def test_la_persistance_vient_apres_le_heartbeat(self):
        """De la MESURE ne passe jamais devant une recommandation."""
        src = inspect.getsource(eng.run)
        assert (src.index("_heartbeat(sb, now, len(matches), len(recommandes))")
                < src.rindex("_persister_causes_closing(sb, now)")
                < src.rindex("verdict_de_fin("))

    def test_chaque_sortie_vidange_les_compteurs(self):
        """`run()` a plusieurs sorties, et le chemin « zéro match » peut suivre
        une capture : chaque `_terminer_run` doit être précédé d'une vidange,
        sinon le décompte de ce run est perdu en silence. La vidange est
        idempotente (elle remet à zéro après écriture), donc l'appeler à
        plusieurs endroits ne double jamais le total du jour."""
        src = inspect.getsource(eng.run)
        vidanges = [i for i in range(len(src))
                    if src.startswith("_persister_causes_closing(sb, now)", i)]
        sorties = [i for i in range(len(src))
                   if src.startswith("_terminer_run(", i)]
        assert sorties, "aucune sortie trouvée — le test ne mesure plus rien"
        for sortie in sorties:
            assert any(v < sortie for v in vidanges), (
                "une sortie de run() n'est pas précédée d'une vidange des "
                "compteurs de non-capture")


class TestPersistance:
    def test_rien_a_dire_naucun_appel(self):
        """Un run sans non-capture ne doit coûter AUCUNE requête."""
        sb = _MetaSB()
        assert cl.persister_causes(sb, NOW) == {}
        assert sb.appels == 0

    def test_la_cle_est_journaliere(self):
        cl.note_cause("ligne_bougee", 2)
        sb = _MetaSB()
        cl.persister_causes(sb, NOW)
        assert sb.upserts[0]["key"] == "closing_causes_20260921"

    def test_lecriture_est_additive_pas_ecrasante(self):
        """Plusieurs runs écrivent la même clé dans la journée : le second ne
        doit pas effacer le premier."""
        cl.note_cause("ligne_bougee", 2)
        cl.note_cause("ligne_illisible", 1)
        sb = _MetaSB(existant='{"ligne_bougee": 5, "sans_cote_exchange": 9}')
        total = cl.persister_causes(sb, NOW)
        assert total == {"ligne_bougee": 7, "ligne_illisible": 1,
                         "sans_cote_exchange": 9}

    def test_une_lecture_cassee_necrase_pas_mais_necheoue_pas(self):
        cl.note_cause("ligne_bougee", 2)
        sb = _MetaSB(casse_lecture=True)
        assert cl.persister_causes(sb, NOW) == {"ligne_bougee": 2}

    def test_une_ecriture_cassee_ne_leve_jamais(self):
        """Convention du dépôt : les erreurs réseau/API ne crashent jamais."""
        cl.note_cause("ligne_bougee", 2)
        sb = _MetaSB(casse_ecriture=True)
        assert cl.persister_causes(sb, NOW) == {}

    def test_sans_base_ne_leve_jamais(self):
        cl.note_cause("ligne_bougee")
        assert cl.persister_causes(None, NOW) == {}


class TestRapportHebdo:
    """La mesure doit ARRIVER à l'opérateur. Comptée dans les logs d'un job et
    nulle part ailleurs, elle serait oubliée — c'est exactement le reproche que
    `closing_coverage` se fait à elle-même dans sa docstring (« une métrique que
    seul un humain attentif peut remarquer n'est pas surveillée »)."""

    def test_chaque_cause_a_un_libelle_francais(self):
        """Règle n°6 : le rapport est lu par l'opérateur, pas par le code. Une
        cause sans libellé s'afficherait en identifiant technique."""
        from scripts.weekly_report import _LIBELLE_CAUSE
        manquants = set(cl.CAUSES_NON_CAPTURE) - set(_LIBELLE_CAUSE)
        assert not manquants, f"causes sans libellé : {manquants}"
        en_trop = set(_LIBELLE_CAUSE) - set(cl.CAUSES_NON_CAPTURE)
        assert not en_trop, f"libellés pour des causes inexistantes : {en_trop}"

    def test_la_dominante_est_nommee_avec_sa_part(self):
        from scripts.weekly_report import _ventilation_causes
        lignes = _ventilation_causes({"ligne_bougee": 30, "ligne_illisible": 10})
        assert "dominante" in lignes[0] and "ligne bougée" in lignes[0]
        assert "75%" in lignes[0].replace(" ", "")
        assert "ligne du pari illisible 10" in lignes[1]

    def test_aucune_non_capture_naffiche_rien(self):
        """Une semaine sans refus est une bonne nouvelle, pas une section vide
        à remplir."""
        from scripts.weekly_report import _ventilation_causes
        assert _ventilation_causes({}) == []
        assert _ventilation_causes({"ligne_bougee": 0}) == []

    def test_la_cle_et_les_causes_sont_importees_pas_recopiees(self):
        """Règle n°6 : le nom de clé journalière vit dans core.closing_line."""
        import scripts.weekly_report as wr
        src = inspect.getsource(wr.causes_de_non_capture)
        assert "_CAUSES_KEY.format(" in src
        assert "closing_causes_" not in src
        assert wr._CAUSES_KEY is cl.CAUSES_KEY
        assert wr.CAUSES_NON_CAPTURE is cl.CAUSES_NON_CAPTURE

    def test_une_cause_inconnue_en_base_est_ignoree(self):
        """Une clé écrite par une version future ne doit pas polluer la somme
        ni faire échouer le rapport."""
        import scripts.weekly_report as wr
        sb = _MetaSB(existant='{"ligne_bougee": 3, "cause_du_futur": 99}')
        total = wr.causes_de_non_capture(sb, jours=1, aujourdhui=NOW)
        assert total == {"ligne_bougee": 3}

    def test_la_somme_couvre_la_fenetre(self):
        import scripts.weekly_report as wr
        sb = _MetaSB(existant='{"ligne_bougee": 2}')
        total = wr.causes_de_non_capture(sb, jours=7, aujourdhui=NOW)
        assert total == {"ligne_bougee": 14}          # 7 jours × 2

    def test_une_lecture_cassee_ne_bloque_pas_le_rapport(self):
        import scripts.weekly_report as wr
        sb = _MetaSB(casse_lecture=True)
        assert wr.causes_de_non_capture(sb, jours=3, aujourdhui=NOW) == {}

    def test_la_section_sintegre_sans_causes(self):
        """Rétrocompatibilité : l'appel historique à deux arguments doit rendre
        exactement la section d'avant."""
        from scripts.weekly_report import closing_coverage
        assert closing_coverage([], 77) == closing_coverage([], 77, None)
