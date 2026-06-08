#!/bin/bash

# Script pour désinstaller les extensions inutiles
# Les extensions redondantes/non-essentielles seront supprimées

echo "🧹 Nettoyage des extensions VS Code inutiles..."
echo ""

# Extensions à supprimer
EXTENSIONS_TO_REMOVE=(
  "anthropic.claude-code"
  "codeium.codeium"
  "saoudrizwan.claude-dev"
  "supermaven.supermaven"
  "ms-vscode.vscode-chat-customizations-evaluations"
  "nickdemayo.vscode-json-editor"
  "github.github-vscode-theme"
  "ms-ceintl.vscode-language-pack-fr"
  "christian-kohler.npm-intellisense"
  "ms-toolsai.datawrangler"
  "msrvida.vscode-sanddance"
  "rangav.vscode-thunder-client"
)

for extension in "${EXTENSIONS_TO_REMOVE[@]}"; do
  echo "❌ Suppression: $extension"
  rm -rf ~/.vscode-remote/extensions/$extension*
done

echo ""
echo "✅ Nettoyage terminé!"
echo ""
echo "📊 Extensions gardées:"
ls ~/.vscode-remote/extensions/ | grep -v "^\.obsolete" | wc -l
echo "extensions"
echo ""
echo "💡 Conseil: Redémarrez VS Code pour appliquer les changements"
