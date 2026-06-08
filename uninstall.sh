#!/bin/bash
cd ~/.vscode-remote/extensions/

echo "🗑️  Suppression des extensions inutiles..."

rm -rf anthropic.claude-code*
rm -rf codeium.codeium*
rm -rf saoudrizwan.claude-dev*
rm -rf supermaven.supermaven*
rm -rf ms-vscode.vscode-chat-customizations-evaluations*
rm -rf nickdemayo.vscode-json-editor*
rm -rf github.github-vscode-theme*
rm -rf christian-kohler.npm-intellisense*
rm -rf ms-toolsai.datawrangler*
rm -rf msrvida.vscode-sanddance*
rm -rf rangav.vscode-thunder-client*
rm -rf ms-ceintl.vscode-language-pack-fr*

echo "✅ Suppression terminée!"
echo ""
echo "📊 Extensions restantes:"
ls -1 | grep -v "^\.obsolete" | grep -v "^extensions.json" | wc -l
echo ""
echo "⚡ Redémarrez VS Code pour appliquer les changements"
