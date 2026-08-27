"""
tests/test_documentation.py — PHASE D4.

La documentation affirmait des faits que le dépôt contredisait :

  · « 14 workflows » — il y en a 6 depuis la fusion du 2026-08-26 ;
  · « registre de 17 fournisseurs » — 18 ;
  · « `.python-version` — 3.11, doit rester aligné avec vercel.json et les
    workflows » — il vaut 3.12, et l'« aligner » est PRÉCISÉMENT le geste qui
    a cassé la production le 2026-08-22 ;
  · « Déploiement dashboard = push sur main (Vercel auto) » — le déploiement
    Git est désactivé dans vercel.json, c'est ci.yml qui pousse après les
    tests ;
  · « Tailwind via CDN » — vendorisé depuis C4 ;
  · « 945 tests » — 1392 ;
  · « Système 7/9 : 90,2 % de réussite, +100 % de ROI mensuel » — mesuré en
    base : 56,1 % sur 114 réglés, ROI net de taxe −10,3 %. Et le mécanisme
    lui-même n'existe nulle part dans le code.

Le dernier n'est pas une coquille : c'est un chiffre inventé, dans le fichier
que lit d'abord quiconque arrive sur le dépôt.

Ce que ces tests gardent : tout compte écrit dans la documentation est
comparé à SA SOURCE. C'est la règle déjà appliquée aux clés IA, aux sports du
dashboard et aux pools de secrets — « ne jamais tenir à la main une liste qui
existe ailleurs ; soit on la dérive, soit un test la compare ».

⚠️ Ce qui n'est PAS gardé ici, et pourquoi : les chiffres de performance. Ils
viennent de Supabase, la suite est hors réseau. Le test ci-dessous vérifie
donc la seule chose vérifiable sans base — qu'aucun taux de réussite n'est
annoncé NU, sans son intervalle de Wilson ni son point mort après taxe. C'est
la même garde de sûreté que sur /performance.
"""
import pathlib
import re

import pytest

_RACINE = pathlib.Path(__file__).resolve().parent.parent
_README = _RACINE / "README.md"
_CLAUDE = _RACINE / "CLAUDE.md"


def _texte(f: pathlib.Path) -> str:
    return f.read_text(encoding="utf-8")


class TestLesComptesSuiventLeurSource:
    def test_le_nombre_de_workflows_annonce_est_le_bon(self):
        reel = len(list((_RACINE / ".github" / "workflows").glob("*.yml")))
        annonces = {int(n) for n in re.findall(r"(\d+)\s+workflows", _texte(_README))}
        faux = annonces - {reel}
        assert not faux, (
            f"README annonce {sorted(faux)} workflows, il y en a {reel}. "
            "Les quatre workflows de scan ont fusionné dans scan.yml le 2026-08-26.")

    def test_le_nombre_de_fournisseurs_ia_annonce_est_le_bon(self):
        from core.ai_router import REGISTRY
        annonces = {int(n) for n in re.findall(r"(?:[Rr]egistre (?:de )?|— )(\d+)\s*\n?\s*fournisseurs",
                                               _texte(_README))}
        faux = annonces - {len(REGISTRY)}
        assert not faux, f"README annonce {sorted(faux)} fournisseurs, le registre en porte {len(REGISTRY)}"

    def test_le_nombre_de_fournisseurs_utilisables_est_le_bon(self):
        """« utilisables en production » = sans `terms_flag`. C'est ce compte
        qui dit combien de capacité existe VRAIMENT — celui qu'on regarde
        quand un quota lâche."""
        from core.ai_router import REGISTRY
        prod = len([p for p in REGISTRY if not getattr(p, "terms_flag", None)])
        annonces = {int(n) for n in re.findall(r"(\d+)\s+utilisables en production", _texte(_README))}
        faux = annonces - {prod}
        assert not faux, f"README annonce {sorted(faux)} utilisables, il y en a {prod}"

    def test_aucun_compte_de_tests_fige_dans_la_documentation(self):
        """Un nombre de tests écrit en dur périme au commit suivant. CLAUDE.md
        en portait un (945) pour une suite qui en compte plus de 1300.
        L'invariant qui tient est « zéro échec »."""
        for f in (_README, _CLAUDE):
            trouve = re.findall(r"(\d{3,5})\s+tests\b", _texte(f))
            assert not trouve, f"{f.name} fige un nombre de tests : {trouve}"


class TestLesDeuxInterpreteurs:
    def test_le_readme_annonce_la_version_reelle_du_fichier(self):
        version = (_RACINE / ".python-version").read_text(encoding="utf-8").strip()
        ligne = [l for l in _texte(_README).splitlines() if ".python-version" in l and "#" in l]
        assert ligne, "l'arborescence du README ne décrit plus .python-version"
        assert version in ligne[0], (
            f".python-version vaut {version!r} ; le README annonce autre chose : {ligne[0].strip()!r}")

    def test_le_readme_ne_recommande_plus_daligner_ce_fichier(self):
        """Le geste que le README recommandait est celui qui a cassé la
        production : l'image de build Vercel n'embarque pas 3.11."""
        assert "doit rester aligné avec vercel.json et les workflows" not in _texte(_README)


class TestLeDeploiementEstDecritTelQuilEst:
    @pytest.mark.parametrize("fichier", [_README, _CLAUDE], ids=lambda f: f.name)
    def test_aucun_fichier_ne_dit_que_le_push_deploie(self, fichier):
        """`vercel.json` désactive le déploiement Git. Croire le contraire
        fait attendre une mise en ligne qui n'arrive que par ci.yml, et
        seulement si la suite est verte."""
        import json
        actif = json.loads((_RACINE / "vercel.json").read_text(encoding="utf-8")) \
            .get("git", {}).get("deploymentEnabled", {}).get("main", True)
        if actif:
            return                       # si un jour on le réactive, la garde s'inverse d'elle-même
        texte = _texte(fichier)
        for phrase in ("push sur main (Vercel auto)",
                       "déploiement du dashboard sur push vers `main`"):
            assert phrase not in texte, f"{fichier.name} : « {phrase} » est faux"


class TestAucunChiffreDePerformanceNu:
    def test_un_taux_de_reussite_est_toujours_accompagne(self):
        """Même règle que /performance : jamais un taux nu. Un pourcentage de
        réussite sans son intervalle de Wilson ni son point mort après taxe
        laisse croire qu'on a mesuré ce qu'on n'a pas mesuré — c'est ce que
        faisait « Win rate historique : 90.2% »."""
        texte = _texte(_README)
        if "réussite" not in texte:
            return
        assert "Wilson" in texte, "un taux de réussite est annoncé sans son intervalle"
        assert "point mort" in texte, "un taux de réussite est annoncé sans son seuil de rentabilité"

    def test_le_systeme_7_sur_9_nest_plus_presente_comme_reel(self):
        """Il n'existe nulle part dans le code : `7/9`, `MIN_WINS`,
        « 9 signaux » — zéro occurrence. Le README peut RACONTER qu'il était
        annoncé (c'est la trace de la correction), pas l'annoncer."""
        import subprocess
        sortie = subprocess.run(["git", "grep", "-lE", r"MIN_WINS|7/9", "--", "*.py"],
                                cwd=_RACINE, capture_output=True, text=True).stdout.strip()
        assert not sortie, f"le mécanisme 7/9 existerait donc dans : {sortie}"
        titres = [l for l in _texte(_README).splitlines() if l.startswith("#")]
        assert not any("7/9" in t for t in titres), \
            "« Système 7/9 » ne doit plus être un titre de section : rien ne l'implémente"


class TestLArborescenceDecritDesFichiersQuiExistent:
    def test_chaque_fichier_cite_dans_larborescence_existe(self):
        """Une arborescence qui nomme des fichiers absents envoie chercher du
        code qui n'est pas là — le README en avait déjà porté (`main.py`,
        `config.py`), au point qu'une note le signale."""
        # TOUS les blocs de code qui dessinent une arborescence, pas le
        # premier venu : le README en porte plusieurs, et l'arborescence n'est
        # pas le premier. Une première version de ce test lisait `split()[1]`
        # et validait donc le SCHÉMA d'architecture — elle passait sur un
        # fichier fantôme ajouté à l'arborescence, éprouvé par sabotage.
        blocs = _texte(_README).split("```")[1::2]
        bloc = "\n".join(b for b in blocs if "├──" in b or "└──" in b)
        assert bloc, "aucune arborescence trouvée dans le README"
        manquants = []
        for nom in re.findall(r"([A-Za-z0-9_]+\.(?:py|json|txt|js))", bloc):
            if not list(_RACINE.rglob(nom)):
                manquants.append(nom)
        assert not manquants, f"cités dans l'arborescence mais absents : {sorted(set(manquants))}"

    def test_les_gabarits_annonces_sont_ceux_du_dossier(self):
        reels = {f.stem for f in (_RACINE / "templates").glob("*.html")}
        ligne = [l for l in _texte(_README).splitlines() if "templates/" in l and "#" in l]
        assert ligne, "l'arborescence ne décrit plus templates/"
        annonces = {m for m in re.findall(r"[a-z]+", ligne[0].split("#", 1)[1])} & \
                   (reels | {"wiz", "signals", "dashboard"})
        assert annonces <= reels, (
            f"le README annonce des pages qui n'existent plus : {sorted(annonces - reels)}")
