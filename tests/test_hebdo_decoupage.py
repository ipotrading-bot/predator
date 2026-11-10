"""Gardiens du découpage du rapport hebdo (2026-11-10).

Les lundis 2026-11-02 et 2026-11-09, le rapport (5 432 puis 5 703 caractères)
a été refusé par Telegram — HTTP 400 « message is too long », limite 4096 —
et `_send` l'envoyait d'un bloc : rien reçu deux semaines de suite. Le rapport
part désormais en plusieurs messages, coupés entre deux sections, sans perdre
une ligne.
"""
from scripts import weekly_report as w

_LIMITE_TELEGRAM = 4096


def _rapport(sections: int = 12, lignes: int = 9) -> str:
    """Un rapport de la forme du vrai : sections séparées par une ligne vide,
    un titre en gras et des lignes de ~80 caractères."""
    blocs = []
    for s in range(sections):
        corps = [f"• ligue {s}-{i} — 33-15 · réussite 68.8% (Wilson- 54.7%, requis 57.8%) · +8.8 u"
                 for i in range(lignes)]
        blocs.append("\n".join([f"🏟 *Section {s}*"] + corps))
    return "\n\n".join(blocs)


class TestDecouper:
    def test_un_rapport_court_reste_un_seul_message(self):
        assert w._decouper("📚 *PREDATOR*\nune ligne") == ["📚 *PREDATOR*\nune ligne"]

    def test_un_rapport_de_la_taille_refusee_tient_sous_la_limite(self):
        texte = _rapport()
        assert len(texte) > _LIMITE_TELEGRAM                 # le cas refusé
        morceaux = w._decouper(texte)
        assert len(morceaux) > 1
        assert all(len(m) <= w.TELEGRAM_COUPE <= _LIMITE_TELEGRAM for m in morceaux)

    def test_aucune_ligne_n_est_perdue_ni_reordonnee(self):
        texte = _rapport()
        lignes = [ligne for m in w._decouper(texte) for ligne in m.split("\n") if ligne]
        assert lignes == [ligne for ligne in texte.split("\n") if ligne]

    def test_la_coupe_tombe_entre_deux_sections(self):
        # Chaque morceau commence par un titre de section : aucune section
        # qui tient dans la limite n'est coupée en deux.
        assert all(m.startswith("🏟 *Section") for m in w._decouper(_rapport()))

    def test_le_gras_reste_apparie_dans_chaque_morceau(self):
        assert all(m.count("*") % 2 == 0 for m in w._decouper(_rapport()))

    def test_une_section_trop_longue_est_coupee_entre_deux_lignes(self):
        texte = _rapport(sections=1, lignes=80)
        morceaux = w._decouper(texte)
        assert len(morceaux) > 1 and all(len(m) <= w.TELEGRAM_COUPE for m in morceaux)
        assert "\n".join(morceaux) == texte

    def test_une_ligne_demesuree_ne_perd_pas_le_rapport(self):
        texte = "tête\n\n" + "x" * 9000 + "\n\nqueue"
        morceaux = w._decouper(texte)
        assert all(len(m) <= w.TELEGRAM_COUPE for m in morceaux)
        assert "".join(morceaux).count("x") == 9000
        assert morceaux[0] == "tête" and morceaux[-1].endswith("queue")


class _R:
    def __init__(self, code, text="ok"):
        self.status_code, self.text = code, text


class TestEnvoi:
    def _telegram(self, monkeypatch, repondre):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
        envois = []

        def post(url, json, timeout):
            envois.append(json["text"])
            return repondre(json, len(envois))
        monkeypatch.setattr(w.requests, "post", post)
        return envois

    def test_un_long_rapport_part_en_plusieurs_messages(self, monkeypatch):
        # Telegram simulé avec sa vraie limite : le rapport entier serait refusé.
        envois = self._telegram(monkeypatch, lambda json, n: (
            _R(400, "Bad Request: message is too long")
            if len(json["text"]) > _LIMITE_TELEGRAM else _R(200)))
        texte = _rapport()
        assert w._send(texte) is True
        assert envois == w._decouper(texte) and len(envois) > 1

    def test_un_morceau_refuse_fait_echouer_le_job(self, monkeypatch):
        envois = self._telegram(monkeypatch, lambda json, n: _R(200) if n == 1 else _R(500, "x"))
        assert w._send(_rapport()) is False
        assert len(envois) == 2                 # on s'arrête au premier refus
