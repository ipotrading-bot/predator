"""
tests/test_clv_honnete.py — le CLV vient de la CLÔTURE, ou de rien.

POURQUOI CE FICHIER (2026-09-09)
--------------------------------
Trois endroits écrivaient dans une colonne nommée « CLV » une re-dérivation
de l'edge d'ENTRÉE (`xbet_odd / pinnacle_price`). Cette valeur est positive
par construction : MIN_EDGE ne laisse jamais sortir un signal d'edge négatif.
Mesuré en base ce jour-là :

  · `signals.clv_pct`            : 413 valeurs >= 0 sur 414 renseignées ;
  · `ai_learning_ledger.was_clv_positive` : True sur 565 lignes sur 566 ;
  · la vraie clôture n'était capturée que sur 81 lignes.

Le dashboard annonçait donc ~99,8 % de « CLV positif » en croyant mesurer la
ligne de clôture. Une métrique qui ne peut pas être mauvaise ne mesure rien —
et celle-ci est la seule qui converge à faible échantillon, donc la seule qui
permette de piloter avant d'avoir des centaines de paris réglés.

L'invariant gardé ici : **aucune écriture de CLV sans un prix POSTÉRIEUR
réellement observé.** Sans capture, la colonne reste NULLE. Un trou se voit
et se répare ; un faux chiffre se croit.

Voir INCIDENTS.md « Le CLV du dashboard ».
"""
from datetime import datetime, timezone

import core.audit_engine as audit_engine
import core.settlement as settlement
from core.db import log_to_ledger
from tests.test_db import _StagedSupabase

_NOW = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)


def _sig():
    return {"id": 1, "match": "A vs B", "sport": "soccer", "market_key": "h2h",
            "selection_name": "A", "xbet_odd": 2.10, "pinnacle_price": 1.90,
            "edge_pct": 3.0, "match_time": "2026-09-09T20:00:00+00:00"}


class TestSettlementNecritPlusDeCLV:
    def test_le_patch_de_reglement_ne_contient_pas_clv_pct(self, monkeypatch):
        """settle_signal observe un SCORE, jamais un prix : il n'a pas de CLV
        à déclarer. Il en écrivait un — l'edge d'entrée — jusqu'au 09-09."""
        patchs = []
        monkeypatch.setattr(settlement, "fetch_match_result",
                            lambda *a, **k: {"home_score": 2, "away_score": 0,
                                             "completed": True})
        monkeypatch.setattr(settlement, "update_signal_fields",
                            lambda sb, sid, patch, **k: patchs.append(patch) or True)
        monkeypatch.setattr(settlement, "log_to_ledger",
                            lambda *a, **k: None)

        assert settlement.settle_signal(None, _sig(), "2026-09-09T22:00:00+00:00")
        assert patchs, "le règlement doit bien écrire un patch"
        assert "clv_pct" not in patchs[0], (
            "le règlement ne mesure aucune clôture : il ne peut pas écrire de CLV")
        assert patchs[0]["outcome"] == "WIN" and patchs[0]["status"] == "settled"

    def test_le_ledger_recoit_un_clv_nul(self, monkeypatch):
        recus = []
        monkeypatch.setattr(settlement, "fetch_match_result",
                            lambda *a, **k: {"home_score": 2, "away_score": 0,
                                             "completed": True})
        monkeypatch.setattr(settlement, "update_signal_fields",
                            lambda *a, **k: True)
        monkeypatch.setattr(settlement, "log_to_ledger",
                            lambda sb, sig, clv, outcome: recus.append(clv))

        settlement.settle_signal(None, _sig(), "2026-09-09T22:00:00+00:00")
        assert recus == [None]


class TestLedgerNinventePasDeVerdict:
    def test_sans_clv_la_colonne_reste_nulle(self):
        """`was_clv_positive` valait `clv > 0` : avec un edge d'entrée en
        entrée, elle ne pouvait qu'être True. Sans CLV, elle vaut None."""
        sb = _StagedSupabase(missing=set())
        log_to_ledger(sb, _sig(), clv=None, outcome="WIN")
        ligne = sb.attempts[-1]
        assert ligne["clv_final"] is None
        assert ligne["was_clv_positive"] is None

    def test_avec_un_vrai_clv_le_verdict_reste_calcule(self):
        sb = _StagedSupabase(missing=set())
        log_to_ledger(sb, _sig(), clv=-1.5, outcome="LOSS")
        ligne = sb.attempts[-1]
        assert ligne["clv_final"] == -1.5
        assert ligne["was_clv_positive"] is False


class TestAuditExpireNinventePasDeCloture:
    """Passe 2 d'`audit_one` : le signal est trop vieux pour être réglé par
    un score, on ne peut plus que constater la clôture — ou son absence."""

    def _auditer(self, monkeypatch, sig):
        """Pousse `sig` jusqu'à la passe 2 et rend (payload écrit, clv ledger)."""
        payloads: list = []
        ledger: list = []
        monkeypatch.setattr(audit_engine, "settle_signal", lambda *a, **k: False)
        monkeypatch.setattr(audit_engine, "_past_expiry", lambda *a, **k: True)
        monkeypatch.setattr(audit_engine, "_update_signal",
                            lambda sb, s, payload: payloads.append(payload) or True)
        monkeypatch.setattr(audit_engine, "log_to_ledger",
                            lambda sb, s, clv, status: ledger.append(clv))
        statut = audit_engine.audit_one(None, sig, [1], _NOW)
        return statut, payloads[0], ledger[0]

    def test_expire_sans_capture_ne_pose_ni_clv_ni_closing_line(self, monkeypatch):
        """La branche `expired` écrivait le prix d'ENTRÉE dans `closing_line`
        et l'edge d'entrée dans `clv_pct`. Une clôture égale à l'ouverture
        n'a jamais été observée : les deux colonnes restent nulles."""
        statut, ecrit, clv_ledger = self._auditer(monkeypatch, _sig())
        assert statut == "expired"
        assert ecrit["clv_pct"] is None
        assert ecrit["closing_line"] is None
        assert clv_ledger is None

    def test_avec_une_cloture_capturee_le_clv_est_ecrit(self, monkeypatch):
        sig = _sig()
        sig["closing_pinnacle_price"] = 1.95   # clôture réellement observée
        statut, ecrit, clv_ledger = self._auditer(monkeypatch, sig)
        assert statut == "closed"
        assert ecrit["clv_pct"] is not None
        assert ecrit["closing_line"] == 1.95
        assert clv_ledger == ecrit["clv_pct"]
