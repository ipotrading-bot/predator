"""
tests/test_ledger_jumeaux.py — garde anti-jumeau inter-sources du ledger.

Incident du 2026-09-02 : le même match réel arrivant par deux sources porte
deux match_id différents, donc deux signal_id — et `ledger_signal_id_uniq`
laissait passer une seconde ligne pour le même pari réel (47 paires exactes
+ 7 floues mesurées dans le ledger d'août, archivées par
sql/migrate_v10_10). Ces doublons gonflaient le n de la couche
d'apprentissage et l'historique /performance.

La garde `_ledger_jumeau_reel` compare (match, selection, market_type) à
l'EXACT sur 6 jours — le flou par noms a été essayé et rendait des faux
positifs (U23/U19 contre seniors) — et applique « le décisif gagne ».
"""

from core.db import _ledger_jumeau_reel, log_to_ledger


class _Resultat:
    def __init__(self, data):
        self.data = data


class _Requete:
    """Chaîne PostgREST factice : enregistre les filtres, rend des lignes."""

    def __init__(self, sb, table, verbe, payload=None):
        self._sb = sb
        self._table = table
        self._verbe = verbe
        self._payload = payload
        self.filtres = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filtres[col] = val
        return self

    def gte(self, col, val):
        self.filtres[f"{col}>="] = val
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        self._sb.appels.append((self._verbe, self._table, self._payload, dict(self.filtres)))
        if self._verbe == "select":
            if isinstance(self._sb.rows_select, Exception):
                raise self._sb.rows_select
            return _Resultat(self._sb.rows_select)
        return _Resultat([])


class _FakeSb:
    def __init__(self, rows_select=None):
        self.rows_select = rows_select or []
        self.appels = []

    def table(self, name):
        sb = self

        class _Table:
            def select(self, *a, **k):
                return _Requete(sb, name, "select").select(*a, **k)

            def insert(self, payload):
                return _Requete(sb, name, "insert", payload)

            def update(self, payload):
                return _Requete(sb, name, "update", payload)

        return _Table()

    def verbes(self):
        return [a[0] for a in self.appels]


_PAYLOAD = {
    "signal_id": 9726,
    "match": "CA Excursionistas vs Argentino de Merlo",
    "selection": "CA Excursionistas",
    "market_type": "h2h",
    "outcome": "WIN",
}

_SIG = {
    "id": 9726,
    "match": "CA Excursionistas vs Argentino de Merlo",
    "selection_name": "CA Excursionistas",
    "market_key": "h2h",
}


class TestLaGardeDetecteLeJumeau:
    def test_jumeau_decisif_stocke_bloque_l_ecriture(self):
        sb = _FakeSb(rows_select=[{"id": "aaa", "signal_id": 9668, "outcome": "WIN"}])
        assert _ledger_jumeau_reel(sb, dict(_PAYLOAD)) is True
        assert "update" not in sb.verbes()   # rien à promouvoir, rien d'écrit

    def test_jumeau_non_decisif_est_promu_pas_duplique(self):
        # Ligne stockée `expired`, résultat réel qui arrive : on met à jour la
        # ligne EXISTANTE (un seul pari réel = une seule ligne), pas d'insert.
        sb = _FakeSb(rows_select=[{"id": "aaa", "signal_id": 9668, "outcome": "expired"}])
        assert _ledger_jumeau_reel(sb, dict(_PAYLOAD)) is True
        promotions = [a for a in sb.appels if a[0] == "update"]
        assert len(promotions) == 1
        assert promotions[0][2] == {"outcome": "WIN"}

    def test_entrant_non_decisif_sur_stocke_non_decisif_ne_duplique_pas(self):
        # Deux jumeaux expirés (cas Excursionistas mesuré) : pas de seconde
        # ligne, et pas de « promotion » d'un expired vers un expired.
        sb = _FakeSb(rows_select=[{"id": "aaa", "signal_id": 9668, "outcome": "expired"}])
        payload = dict(_PAYLOAD, outcome="expired")
        assert _ledger_jumeau_reel(sb, payload) is True
        assert "update" not in sb.verbes()

    def test_le_meme_signal_id_n_est_pas_un_jumeau(self):
        # L'idempotence par signal_id appartient à l'index unique et à
        # _ledger_deja_ecrit — la garde ne double pas ce chemin.
        sb = _FakeSb(rows_select=[{"id": "aaa", "signal_id": 9726, "outcome": "WIN"}])
        assert _ledger_jumeau_reel(sb, dict(_PAYLOAD)) is False


class TestLaGardeNInventePas:
    def test_sans_jumeau_l_ecriture_passe(self):
        sb = _FakeSb(rows_select=[])
        assert _ledger_jumeau_reel(sb, dict(_PAYLOAD)) is False

    def test_cle_incomplete_ne_compare_pas(self):
        # Sans selection (ou match, ou market_type), aucune comparaison :
        # bloquer sur une clé partielle supprimerait de vraies lignes.
        sb = _FakeSb(rows_select=[{"id": "aaa", "signal_id": 1, "outcome": "WIN"}])
        payload = dict(_PAYLOAD, selection=None)
        assert _ledger_jumeau_reel(sb, payload) is False
        assert sb.appels == []

    def test_panne_de_lecture_laisse_ecrire(self):
        # Fail-open : un doublon se rattrape par archivage, un résultat réel
        # perdu est définitif (règle n°9).
        sb = _FakeSb(rows_select=RuntimeError("timeout"))
        assert _ledger_jumeau_reel(sb, dict(_PAYLOAD)) is False


class TestBranchementDansLogToLedger:
    def test_log_to_ledger_saute_l_insert_sur_jumeau(self):
        sb = _FakeSb(rows_select=[{"id": "aaa", "signal_id": 9668, "outcome": "WIN"}])
        log_to_ledger(sb, dict(_SIG), clv=0.0, outcome="WIN")
        assert "insert" not in sb.verbes()

    def test_log_to_ledger_insere_sans_jumeau(self):
        sb = _FakeSb(rows_select=[])
        log_to_ledger(sb, dict(_SIG), clv=0.0, outcome="WIN")
        assert "insert" in sb.verbes()

    def test_la_fenetre_de_recherche_est_bornee(self):
        # La requête jumeau doit filtrer sur created_at récent : sans borne,
        # un match rejoué des mois plus tard serait pris pour un jumeau.
        sb = _FakeSb(rows_select=[])
        _ledger_jumeau_reel(sb, dict(_PAYLOAD))
        filtres = sb.appels[0][3]
        assert "created_at>=" in filtres
