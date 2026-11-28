"""
tests/test_dashboard_vitesse.py — le dashboard répond vite (2026-11-28).

Plainte opérateur : « lent au démarrage, les liens chargent lentement ».
Mesuré ce jour-là sur la production, avant correctif :

    /             3,1 s à froid, 0,4 à 1,4 s à chaud
    /performance  3,7 à 4,4 s, À CHAQUE chargement
    /ledger       1,1 s        /audit  0,8 s

Trois causes, trois gardiens :

  1. LA FONCTION TOURNAIT SUR UN AUTRE CONTINENT QUE LA BASE. Vercel la
     posait à Washington (`iad1`, son défaut) ; Supabase est à Stockholm
     (`eu-north-1`). Chaque requête SQL traversait l'Atlantique, et
     /performance en enchaîne neuf.
  2. /performance RELISAIT TOUT LE LEDGER À CHAQUE CLIC du menu des mois
     (2 045 lignes, 1,8 Mo, depuis le cumul du 2026-11-25).
  3. `/manifest.json` et `/favicon.ico`, liés par tous les gabarits,
     partaient en `no-store` : deux réveils de la fonction par navigation.

Tests PURS : aucune route appelée, aucun gabarit rendu (tests/conftest.py).
"""
import json
import pathlib

import pytest

import api.index as dash

_RACINE = pathlib.Path(__file__).resolve().parent.parent

# Région Vercel qui partage le centre de données de la base. Le projet
# Supabase (chnyxeyqpdipeogirrpu) est en `eu-north-1` — Stockholm, `arn1`
# chez Vercel. Lu le 2026-11-28 sur l'API de gestion Supabase ; la région
# d'un projet Supabase ne change pas après sa création.
_REGION_DE_LA_BASE = "arn1"


class TestLaFonctionVitPresDeLaBase:
    def test_vercel_json_pose_la_fonction_dans_la_region_de_la_base(self):
        """Sans `regions`, Vercel retombe sur `iad1` : ~100 ms d'aller-retour
        par requête SQL au lieu de quelques millisecondes."""
        conf = json.loads((_RACINE / "vercel.json").read_text(encoding="utf-8"))
        assert conf.get("regions") == [_REGION_DE_LA_BASE], (
            "vercel.json doit garder la fonction dans la région de la base "
            f"({_REGION_DE_LA_BASE}) : ailleurs, chaque requête SQL traverse "
            "un océan — voir INCIDENTS.md, « La fonction tournait sur un "
            "autre continent que la base ».")


class _FauxLecteur:
    """Compte les lectures réelles déclenchées par `_lecture_ledger`."""

    def __init__(self, monkeypatch, casse: bool = False):
        self.ledger, self.coups, self.casse = 0, 0, casse
        monkeypatch.setattr(dash, "_ledger_par_pages", self._ledger)
        monkeypatch.setattr(dash, "_coups_d_envoi", self._coups)
        monkeypatch.setattr(dash, "_lecture_ledger_memo", {})

    def _ledger(self, _sb, depuis):
        self.ledger += 1
        if self.casse:
            raise RuntimeError("réseau")
        return [{"id": self.ledger, "signal_id": 7, "depuis": depuis}]

    def _coups(self, _sb, _rows):
        self.coups += 1
        return {"7": "2026-11-01T18:00:00+00:00"}


class TestLectureDuLedgerMemorisee:
    def test_un_second_chargement_ne_relit_rien(self, monkeypatch):
        """Changer de mois dans le menu = même lecture, autre découpage."""
        faux = _FauxLecteur(monkeypatch)
        un = dash._lecture_ledger(object(), "2026-08-01", maintenant=1000.0)
        deux = dash._lecture_ledger(object(), "2026-08-01", maintenant=1030.0)
        assert un == deux
        assert (faux.ledger, faux.coups) == (1, 1)

    def test_la_memoire_expire(self, monkeypatch):
        """Jamais plus d'une minute de retard sur le ledger."""
        faux = _FauxLecteur(monkeypatch)
        dash._lecture_ledger(object(), "2026-08-01", maintenant=1000.0)
        dash._lecture_ledger(object(), "2026-08-01",
                             maintenant=1000.0 + dash._LECTURE_LEDGER_TTL_S)
        assert faux.ledger == 2
        assert dash._LECTURE_LEDGER_TTL_S <= 60

    def test_une_autre_borne_relit_et_remplace(self, monkeypatch):
        """La borne change au 1er du mois : la fenêtre gardée est la
        dernière lue, une seule — la mémoire ne s'empile pas."""
        faux = _FauxLecteur(monkeypatch)
        dash._lecture_ledger(object(), "2026-08-01", maintenant=1000.0)
        data, _ = dash._lecture_ledger(object(), None, maintenant=1001.0)
        assert faux.ledger == 2 and data[0]["depuis"] is None
        assert list(dash._lecture_ledger_memo) == [None]

    def test_une_panne_ne_se_memorise_pas(self, monkeypatch):
        """Une lecture qui lève remonte à la route (état vide + log) et le
        chargement suivant RELIT : mémoriser l'échec viderait la page une
        minute entière."""
        faux = _FauxLecteur(monkeypatch, casse=True)
        with pytest.raises(RuntimeError):
            dash._lecture_ledger(object(), "2026-08-01", maintenant=1000.0)
        assert dash._lecture_ledger_memo == {}
        faux.casse = False
        data, coups = dash._lecture_ledger(object(), "2026-08-01", maintenant=1001.0)
        assert data and coups and faux.ledger == 2

    def test_une_horloge_qui_recule_relit(self, monkeypatch):
        faux = _FauxLecteur(monkeypatch)
        dash._lecture_ledger(object(), "2026-08-01", maintenant=1000.0)
        dash._lecture_ledger(object(), "2026-08-01", maintenant=10.0)
        assert faux.ledger == 2


class TestPolitiqueDeCache:
    @pytest.mark.parametrize("chemin", ["/", "/performance", "/ledger", "/audit",
                                        "/api/signals", "/api/health", "/api/scan"])
    def test_aucune_donnee_n_est_mise_en_cache(self, chemin):
        """Un signal périmé affiché comme actif est un faux pari : ni le
        navigateur ni le CDN ne gardent une page de données."""
        entetes = dash._politique_de_cache(chemin)
        assert entetes["Cache-Control"] == "no-cache, no-store, must-revalidate"
        assert "s-maxage" not in entetes["Cache-Control"]

    @pytest.mark.parametrize("chemin", ["/static/css/predator.css",
                                        "/static/icons/icon-192.png",
                                        "/manifest.json", "/favicon.ico"])
    def test_les_fichiers_figes_restent_au_cdn(self, chemin):
        cc = dash._politique_de_cache(chemin)["Cache-Control"]
        assert f"max-age={dash._STATIC_MAX_AGE}" in cc.split(", ")
        assert f"s-maxage={dash._CDN_MAX_AGE}" in cc.split(", ")
        assert "no-store" not in cc

    def test_le_navigateur_ne_garde_pas_un_fichier_plus_de_dix_minutes(self):
        """Pas d'empreinte dans les noms de fichiers : un cache NAVIGATEUR
        long garderait un CSS périmé sans moyen de l'invalider. Le cache du
        CDN, lui, tombe à chaque déploiement."""
        assert dash._STATIC_MAX_AGE <= 600

    def test_une_page_sans_donnee_passe_par_le_cdn_et_revalide(self):
        for chemin in dash._PAGES_SANS_DONNEE:
            cc = dash._politique_de_cache(chemin)["Cache-Control"].split(", ")
            assert "max-age=0" in cc and "must-revalidate" in cc
            assert f"s-maxage={dash._CDN_MAX_AGE}" in cc

    def test_une_page_sans_donnee_ne_recoit_vraiment_aucune_donnee(self):
        """`/system` n'est servie par le CDN que parce que sa route ne passe
        RIEN au gabarit. Le jour où elle lit la base, elle sort de la liste."""
        import inspect
        assert dash._PAGES_SANS_DONNEE == ("/system",)
        corps = inspect.getsource(dash.system)
        assert 'render_template("system.html")' in corps
        assert "_db(" not in corps

    def test_les_chemins_figes_sont_ceux_que_les_gabarits_lient(self):
        for gabarit in sorted((_RACINE / "templates").glob("*.html")):
            html = gabarit.read_text(encoding="utf-8")
            for chemin in dash._CHEMINS_FIGES:
                assert f'href="{chemin}"' in html, (gabarit.name, chemin)

    def test_une_erreur_n_est_jamais_mise_en_cache(self):
        """`no_cache` passe un chemin vide dès que le statut est une erreur :
        un 404 sous /static/ gardé un an au CDN masquerait le fichier
        jusqu'au déploiement suivant."""
        import inspect
        assert "response.status_code < 400" in inspect.getsource(dash.no_cache)
        assert "no-store" in dash._politique_de_cache("")["Cache-Control"]
