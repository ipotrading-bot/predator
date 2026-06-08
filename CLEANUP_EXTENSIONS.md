# 🧹 Guide de Nettoyage des Extensions VS Code

## Pourquoi?
Ces extensions ralentissent l'installation et ne sont pas nécessaires pour ce projet Python.

## Extensions à SUPPRIMER (12 au total)

### Outils IA redondants ❌
- `anthropic.claude-code` (versions 2.1.168 et 2.1.89)
- `codeium.codeium`
- `saoudrizwan.claude-dev`
- `supermaven.supermaven`
⚠️ **Gardez uniquement: Cline (roo-cline)** pour l'IA

### Extensions cosmétiques/non-essentielles ❌
- `github.github-vscode-theme`
- `ms-ceintl.vscode-language-pack-fr` (optionnel si vous aimez le français)
- `nickdemayo.vscode-json-editor`
- `ms-vscode.vscode-chat-customizations-evaluations`

### Extensions spécialisées inutiles pour ce projet ❌
- `codeium.codeium`
- `ms-toolsai.datawrangler`
- `msrvida.vscode-sanddance`
- `rangav.vscode-thunder-client`
- `christian-kohler.npm-intellisense` (non-utilisé, c'est un projet Python)

---

## ✅ Extensions à GARDER (12 essentielles)

```json
{
  "ms-python.python": "Support Python",
  "ms-python.vscode-pylance": "Language Server",
  "ms-python.debugpy": "Debugger",
  "eamodio.gitlens": "Git",
  "github.vscode-pull-request-github": "GitHub PRs",
  "mtxr.sqltools": "SQL Database",
  "mtxr.sqltools-driver-pg": "PostgreSQL Driver",
  "humao.rest-client": "API Testing",
  "usernamehw.errorlens": "Error Hints",
  "yzhang.markdown-all-in-one": "Markdown",
  "weiskopfsodefa.vercel-vscode-by-sodefa": "Vercel (pour vercel.json)",
  "rooveterinaryinc.roo-cline": "Cline Assistant"
}
```

---

## 🚀 Comment appliquer?

### Option 1: Désinstallation manuelle
1. Ouvrez la palette de commandes: `Ctrl+Shift+P`
2. Tapez: `Extensions: Show Installed Extensions`
3. Cherchez chaque extension listée ci-dessus
4. Cliquez sur l'engrenage → `Uninstall`

### Option 2: Utiliser le fichier de config
Les fichiers de configuration ont été créés:
- `.devcontainer.json` - Contient la liste des extensions à installer
- `.vscode/extensions.json` - Recommandations pour le projet

Lors de la prochaine création du dev container, seules les extensions essentielles seront installées.

---

## 📊 Avant/Après

| | Avant | Après |
|--|-------|-------|
| Nombre d'extensions | 37 | ~25 |
| Extensions inutiles | 12 | 0 |
| Temps d'installation | Lent | 🚀 Rapide |

**Gain estimé:** -40% du temps d'installation VS Code
