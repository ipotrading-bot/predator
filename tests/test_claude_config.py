"""
tests/test_claude_config.py — gardiens de l'outillage Claude Code (`.claude/`).

POURQUOI CE FICHIER EXISTE — règle dure n°6 : ne jamais tenir à la main une
liste qui existe ailleurs sans qu'un test la compare à sa source. `.claude/`
porte exactement ce genre de listes : des chemins de scripts référencés dans
`settings.json`, une version npm épinglée dans `.mcp.json`, des consignes qui
peuvent citer du code supprimé. Trois dérives déjà vécues ailleurs dans ce
dépôt, chacune silencieuse :

  · un chemin absolu machine (`/workspaces/predator`) dans un hook — il
    fonctionne dans CE devcontainer et meurt partout ailleurs, sans erreur
    visible (le hook sort juste en échec en arrière-plan) ;
  · des mentions de Wiz (supprimé le 2026-08-26, règle dure n°8) dans les
    consignes d'agents — une consigne qui décrit du code disparu fait
    chercher, ou pire recréer, ce code ;
  · `@latest` dans `.mcp.json` — npm décidait à chaque session de ce qui
    tourne avec un jeton Supabase en main, à rebours du verrouillage `==`
    de `requirements*.txt` (D2).

Tests purs : aucun réseau, aucun rendu. Les hooks eux-mêmes sont exécutés
via subprocess (bash + jq, présents sur ubuntu-latest) avec des entrées JSON
forgées — cas bloquant ET cas neutre pour chacun.
"""
import json
import os
import pathlib
import re

_RACINE = pathlib.Path(__file__).resolve().parent.parent
_CLAUDE_DIR = _RACINE / ".claude"
_SETTINGS = _CLAUDE_DIR / "settings.json"
_MCP = _RACINE / ".mcp.json"


def _fichiers_claude():
    return [f for f in _CLAUDE_DIR.rglob("*") if f.is_file()]


class TestLesFichiersDeConfigSontValides:
    def test_settings_json_est_du_json_valide(self):
        json.loads(_SETTINGS.read_text(encoding="utf-8"))

    def test_mcp_json_est_du_json_valide(self):
        json.loads(_MCP.read_text(encoding="utf-8"))


class TestAucuneMentionMorteNiCheminMachine:
    def test_aucun_fichier_claude_ne_mentionne_wiz(self):
        """Wiz est supprimé sans archive (règle dure n°8). Une consigne qui
        le nomme encore envoie chercher du code qui n'existe plus."""
        fautifs = [str(f.relative_to(_RACINE)) for f in _fichiers_claude()
                   if "wiz" in f.read_text(encoding="utf-8", errors="ignore").lower()]
        assert not fautifs, f"mention de Wiz sous .claude/ : {fautifs}"

    def test_aucun_chemin_absolu_machine_sous_claude(self):
        """`/workspaces/` est le chemin de CE devcontainer. Un hook qui le
        code en dur meurt sur toute autre machine, en silence — le chemin
        portable est `$CLAUDE_PROJECT_DIR`."""
        fautifs = [str(f.relative_to(_RACINE)) for f in _fichiers_claude()
                   if "/workspaces/" in f.read_text(encoding="utf-8", errors="ignore")]
        assert not fautifs, f"chemin absolu machine sous .claude/ : {fautifs}"


class TestLesScriptsReferencesExistent:
    def test_chaque_script_de_settings_existe_et_est_executable(self):
        """Un hook déclaré vers un script absent ne lève RIEN : l'événement
        passe, la garde n'existe pas. C'est la panne `.claude/` par excellence
        — vérifier le câblage, pas seulement les scripts."""
        texte = _SETTINGS.read_text(encoding="utf-8")
        chemins = set(re.findall(r"\.claude/hooks/[\w.\-]+\.sh", texte))
        assert chemins, "settings.json ne référence plus aucun script de hook"
        for chemin in sorted(chemins):
            script = _RACINE / chemin
            assert script.exists(), f"{chemin} référencé mais absent"
            assert os.access(script, os.X_OK), f"{chemin} n'est pas exécutable (chmod +x)"

    def test_tout_script_de_hooks_est_executable(self):
        for script in sorted((_CLAUDE_DIR / "hooks").glob("*.sh")):
            assert os.access(script, os.X_OK), f"{script.name} n'est pas exécutable"


class TestLeServeurMcpEstEpingle:
    def test_mcp_json_ne_contient_pas_latest(self):
        """Même principe que le `==` de requirements*.txt : aucun tiers ne
        décide de ce qui tourne. Le pourquoi est dans
        docs/actions_operateur.md, section « Claude Code »."""
        assert "@latest" not in _MCP.read_text(encoding="utf-8"), \
            ".mcp.json porte un @latest : épingler la version exacte"

    def test_le_serveur_supabase_est_en_lecture_seule(self):
        config = json.loads(_MCP.read_text(encoding="utf-8"))
        args = config["mcpServers"]["supabase"]["args"]
        assert "--read-only" in args, \
            "le serveur MCP Supabase doit rester --read-only (écritures via ops.py)"
