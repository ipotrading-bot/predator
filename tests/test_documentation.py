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
_INCIDENTS = _RACINE / "INCIDENTS.md"


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
        # `:!` exclut CE fichier, qui NOMME le mécanisme pour le nier — même
        # parti pris que tests/test_sans_scipy.py. Sans l'exclusion le test
        # se détecte lui-même ; il ne passait au commit de D4 que parce qu'il
        # n'était pas encore SUIVI par git, `git grep` ne lisant que l'index.
        sortie = subprocess.run(
            ["git", "grep", "-lE", r"MIN_WINS|7/9", "--", "*.py", ":!tests/test_documentation.py"],
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


# ── D5 : la scission CLAUDE.md / INCIDENTS.md ────────────────────────────

class TestLaScissionTient:
    """`CLAUDE.md` faisait 38 Ko, dont 33 de récit d'incidents, et il est
    chargé dans CHAQUE session. Un fichier de consignes qu'on ne lit plus en
    entier ne donne plus de consignes : le récit est parti dans
    `INCIDENTS.md`, les consignes sont restées.

    Ces tests existent parce que la pente naturelle est le regonflement — le
    prochain incident coûteux voudra s'écrire là où on le lira d'abord."""

    PLAFOND = 5_000

    def test_claude_md_reste_sous_son_plafond(self):
        taille = len(_texte(_CLAUDE).encode("utf-8"))
        assert taille <= self.PLAFOND, (
            f"CLAUDE.md fait {taille} o (plafond {self.PLAFOND}). Le récit d'un "
            "incident va dans INCIDENTS.md ; ici ne restent que la commande, "
            "l'architecture, la convention et la règle dure.")

    def test_le_recit_a_bien_ete_deplace_et_non_resume(self):
        """La scission ne devait rien perdre : INCIDENTS.md doit peser
        l'ordre de grandeur de ce qui est sorti de CLAUDE.md."""
        assert len(_texte(_INCIDENTS).encode("utf-8")) > 25_000

    def test_claude_md_envoie_lire_les_incidents(self):
        """Sans ce renvoi, la scission ne fait que CACHER les pièges."""
        texte = _texte(_CLAUDE)
        assert "INCIDENTS.md" in texte
        assert "AVANT DE" in texte, "le renvoi doit être impératif, pas décoratif"

    def test_chaque_section_citee_par_une_regle_dure_existe(self):
        """Les règles dures renvoient à une section d'INCIDENTS.md sous la
        forme `— *Nom*`. Une règle qui pointe vers une section disparue est
        pire qu'une règle sans renvoi : elle fait croire qu'on peut vérifier.

        ⚠️ La citation doit être le PRÉFIXE d'un vrai titre, pas « un mot en
        commun quelque part ». Une première version se contentait de
        `any(mot in titres)` : elle acceptait « Une section qui n'existe pas »
        parce que le mot « section » figurait ailleurs dans le fichier.
        Trouvé par sabotage.
        """
        regles = [l for l in _texte(_CLAUDE).splitlines() if re.match(r"^\s*\d+\.", l)]
        assert len(regles) >= 8, "les règles dures ont disparu de CLAUDE.md"

        def normalise(t):
            import unicodedata
            t = unicodedata.normalize("NFD", t.lower())
            return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())

        titres = [normalise(t) for t in
                  re.findall(r"^### (.+)$", _texte(_INCIDENTS), re.M)]
        assert titres, "INCIDENTS.md n'a plus de sections"

        # `— *…*` seulement : le `*…*` isolé attraperait l'intérieur des
        # passages en gras, qui ne sont pas des renvois.
        citations = re.findall(r"—\s*\*([^*]{2,60})\*", _texte(_CLAUDE))
        assert citations, "plus aucune règle ne renvoie à une section"
        for citation in citations:
            attendu = normalise(citation)
            assert any(t.startswith(attendu) for t in titres), (
                f"la règle dure cite « {citation} » : aucune section "
                f"d'INCIDENTS.md ne commence par ça")

    def test_les_prohibitions_absolues_survivent_dans_claude_md(self):
        """Celles-là doivent être lisibles SANS ouvrir INCIDENTS.md : elles
        interdisent un geste qu'on ferait avant d'avoir lu quoi que ce soit."""
        texte = _texte(_CLAUDE)
        for prohibition in ("toJSON(secrets)",          # workflow refusé par GitHub, zéro job
                            "ci_env.py",                # blocs de secrets générés
                            "ai_router.py",             # aucun modèle en dur
                            ".python-version",          # appartient à Vercel
                            "Wilson"):                  # jamais un taux nu
            assert prohibition in texte, f"règle dure perdue dans la scission : {prohibition}"


class TestLesCadencesDocumenteesSuiventLesWorkflows:
    """Les crons vivent dans `.github/workflows/`. Deux documents les
    recopient — la table de la skill `predator-pipeline` (pour l'agent) et
    `docs/systeme_de_scan.md` (pour l'opérateur) — et une copie diverge.

    Mesuré le 2026-09-04 : la table de la skill annonçait encore les scans
    standard à 02/06/09/12/17/19/21/23, alors que le recalage du 2026-09-03
    les avait déplacés à 06/09/11/13/16/19/21/23. Une doc de cadence fausse
    est pire qu'absente : c'est elle qu'on lit pour décider si un cron a
    « raté » — donc pour décider si un scan manquant est un incident.

    Les cadences courantes portent le marqueur ``cron:`…` `` ; les cadences
    HISTORIQUES (« était 4-59/10 ») s'écrivent sans, sinon ce test réclamerait
    qu'un workflow les porte encore.
    """

    _CADENCES = _RACINE / ".claude" / "skills" / "predator-pipeline" / "cadences_cron.md"
    _SCAN_OPERATEUR = _RACINE / "docs" / "systeme_de_scan.md"

    @staticmethod
    def _crons_des_workflows() -> set[str]:
        crons = set()
        for yml in (_RACINE / ".github" / "workflows").glob("*.yml"):
            crons |= set(re.findall(r"-\s*cron:\s*['\"]([^'\"]+)['\"]",
                                    yml.read_text(encoding="utf-8")))
        assert crons, "plus aucun cron dans .github/workflows/ — le dépôt ne tourne plus"
        return crons

    def test_la_table_de_la_skill_liste_exactement_les_crons_reels(self):
        documentes = set(re.findall(r"cron:`([^`]+)`", _texte(self._CADENCES)))
        reels = self._crons_des_workflows()
        assert documentes == reels, (
            f"la table de cadence diverge des workflows.\n"
            f"  documentés mais inexistants : {sorted(documentes - reels)}\n"
            f"  réels mais non documentés   : {sorted(reels - documentes)}\n"
            "Corriger cadences_cron.md — un cron courant s'y écrit cron:`…`.")

    def test_les_heures_de_scan_de_la_doc_operateur_sortent_du_cron(self):
        """`docs/systeme_de_scan.md` est ce que lit l'opérateur pour savoir
        quand un scan aurait dû tomber. Ses heures sont dérivées ici du seul
        cron que `ci_scan_mode.py` associe à `standard`."""
        import importlib
        ci_scan_mode = importlib.import_module("scripts.ci_scan_mode")
        standard = [c for c, m in ci_scan_mode.CRON_MODES.items() if m == "standard"]
        assert len(standard) == 1, (
            f"{len(standard)} crons `standard` — ce test suppose l'unicité, l'étendre "
            "en connaissance de cause")
        minute, heures = standard[0].split()[0], standard[0].split()[1]
        attendues = {f"{int(h):02d}:{int(minute):02d}" for h in heures.split(",")}

        ligne = next((l for l in _texte(self._SCAN_OPERATEUR).splitlines()
                      if l.startswith("| Scan **standard**")), None)
        assert ligne, "la ligne « Scan **standard** » a disparu du tableau « Quand (UTC) »"
        annoncees = set(re.findall(r"\b(\d{2}:\d{2})\b", ligne))
        assert annoncees == attendues, (
            f"docs/systeme_de_scan.md annonce {sorted(annoncees)}, le cron dit "
            f"{sorted(attendues)} ({standard[0]}).")
