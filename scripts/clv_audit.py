"""
scripts/clv_audit.py — Phase Shadow : Audit CLV Automatique
Usage: python scripts/clv_audit.py

Analyse les signaux enregistrés dans Supabase et compare :
- Cote trouvée (soft_book_price) vs Cote de clôture Pinnacle (closing_price)
- Calcule le CLV (Closing Line Value) réel
- Détecte si le bot bat vraiment le marché

Doctrine PhD MIT : Si CLV > 0 sur 48h → Signal BET.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

# Ajouter la racine du projet au path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from config import settings
    from data.supabase_client import SupabaseClient
except ImportError:
    print("❌ Erreur d'import. Vérifiez que vous exécutez depuis la racine du projet.")
    print("   Usage: python scripts/clv_audit.py")
    sys.exit(1)


def main():
    db = SupabaseClient()
    print("=" * 60)
    print("🦅 PREDATOR PAIM — Phase Shadow : Audit CLV")
    print("=" * 60)

    # 1. Récupérer les signaux des dernières 72h
    try:
        three_days_ago = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        response = db._client.table("signals") \
            .select("*") \
            .gte("created_at", three_days_ago) \
            .order("created_at", desc=True) \
            .execute()
        signals = response.data or []
    except Exception as e:
        print(f"❌ Erreur Supabase: {e}")
        return

    if not signals:
        print("\n📭 Aucun signal trouvé dans les 72 dernières heures.")
        print("   Le scan n'a détecté aucune opportunité — capital préservé ✅")
        return

    # 2. Analyser chaque signal
    print(f"\n📊 {len(signals)} signaux analysés")
    print("-" * 60)

    total_signals = len(signals)
    settled = [s for s in signals if s.get("status") == "settled"]
    pending = [s for s in signals if s.get("status") == "pending"]
    wins = [s for s in settled if s.get("outcome") == 1]
    losses = [s for s in settled if s.get("outcome") == 0]

    # CLV moyen (closing_line_value)
    clv_values = []
    for s in signals:
        implied_prob = s.get("implied_prob_soft", 0)
        closing_price = s.get("clv", 0)  # clv stocke la cote de clôture dans notre DB
        if implied_prob > 0 and closing_price > 0:
            closing_prob = 1.0 / closing_price
            clv = (implied_prob - closing_prob) / closing_prob
            clv_values.append(clv)

    avg_clv = sum(clv_values) / len(clv_values) if clv_values else 0

    # Win Rate
    win_rate = len(wins) / len(settled) * 100 if settled else 0

    # Profit / Loss simulé
    total_staked = sum(s.get("recommended_stake", 0) for s in signals)
    total_won = sum(s.get("recommended_stake", 0) * (s.get("clv", 0) - 1) for s in wins)
    total_lost = sum(s.get("recommended_stake", 0) for s in losses)
    net_pnl = total_won - total_lost

    # 3. Rapport
    print(f"\n{'='*60}")
    print(f"📋 RAPPORT D'AUDIT SHADOW")
    print(f"{'='*60}")
    print(f"  📅 Période          : 72h (jusqu'à {datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"  📊 Signaux totaux   : {total_signals}")
    print(f"  ⏳ En attente       : {len(pending)}")
    print(f"  ✅ Réglés           : {len(settled)}")
    print(f"  🏆 Victoires        : {len(wins)}")
    print(f"  ❌ Défaites         : {len(losses)}")
    print(f"  📈 Win Rate         : {win_rate:.1f}%" if settled else "  📈 Win Rate         : N/A (pas de règlement)")
    print(f"  🎯 CLV Moyen        : {avg_clv:.4f} ({avg_clv*100:.2f}%)")
    print(f"  💰 Mise totale      : {total_staked:.0f}€")
    print(f"  📊 P&L Brut         : {net_pnl:+.2f}€")
    print(f"  💰 Bankroll         : {settings.starting_bankroll:.0f}€")
    roi = (net_pnl / total_staked * 100) if total_staked > 0 else 0
    print(f"  📈 ROI              : {roi:+.2f}%")

    # 4. Verdict PhD MIT
    print(f"\n{'='*60}")
    print(f"🧠 VERDICT DOCTRINAIRE (PhD MIT)")
    print(f"{'='*60}")

    if avg_clv > 0.02:
        print(f"  ✅ CLV > 2% → Le bot bat le marché. Signal BET confirmé.")
    elif avg_clv > 0:
        print(f"  ⚠️  CLV > 0 mais < 2% → Le bot est marginalement positif.")
        print(f"      → Augmentez le seuil EV+ minimum à 5%.")
    else:
        print(f"  ❌ CLV ≤ 0 → Le bot ne bat PAS le marché.")
        print(f"      → Les signaux sont du bruit. Ajustez les filtres.")

    if roi > 0:
        print(f"  ✅ ROI positif → Stratégie viable.")
    else:
        print(f"  ⚠️  ROI négatif → Réduisez le staking ou augmentez les filtres.")

    # Sauvegarde du rapport dans Supabase
    report = {
        "total_signals": total_signals,
        "settled": len(settled),
        "pending": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "avg_clv": round(avg_clv, 5),
        "total_staked": round(total_staked, 2),
        "net_pnl": round(net_pnl, 2),
        "roi": round(roi, 2),
        "verdict": "BET" if avg_clv > 0.02 else "WARNING" if avg_clv > 0 else "NOISE",
        "audited_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        db._client.table("clv_audit_logs").insert(report).execute()
        print(f"\n  💾 Rapport sauvegardé dans Supabase (clv_audit_logs)")
    except Exception as e:
        print(f"\n  ⚠️  Sauvegarde du rapport ignorée: {e}")

    print(f"\n{'='*60}")
    print(f"🏁 Audit terminé. Prochain audit automatique dans 24h.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()