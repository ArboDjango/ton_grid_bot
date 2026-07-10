import re
from datetime import datetime

log_path = "/home/arbodjango/ton_grid_bot/bot.log"

buys = []
sells = []
slippages = []

# Expressions régulières pour scanner le log
buy_pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*✅ BUY @ ([\d.]+)")
sell_pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*💰 SELL @ ([\d.]+)")
slip_pattern = re.compile(r"Slippage: ([\d.]+)%")

with open(log_path, "r") as f:
    for line in f:
        buy_match = buy_pattern.search(line)
        sell_match = sell_pattern.search(line)
        slip_match = slip_pattern.search(line)
        
        if buy_match:
            buys.append((datetime.strptime(buy_match.group(1), "%Y-%m-%d %H:%M:%S"), float(buy_match.group(2))))
        if sell_match:
            sells.append((datetime.strptime(sell_match.group(1), "%Y-%m-%d %H:%M:%S"), float(sell_match.group(2))))
        if slip_match:
            slippages.append(float(slip_match.group(1)))

total_trades = len(buys) + len(sells)

print(f"========== RAPPORT DE PERFORMANCE ==========")
print(f"🔄 Nombre total de trades : {total_trades}")
print(f"🛒 Total ACHATS (BUY)     : {len(buys)}")
print(f"💰 Total VENTES (SELL)    : {len(sells)}")

# Calcul des paires (allers-retours validés)
pairs_completed = min(len(buys), len(sells))
print(f"🧱 Allers-retours bouclés : {pairs_completed}")

if total_trades > 0:
    avg_slippage = sum(slippages) / len(slippages) if slippages else 0.0
    print(f"📉 Slippage Moyen         : {avg_slippage:.4f}%")
    
    # ── CALCUL ESTIMATIF DES GAINS ────────────────────────
    # Gv par cellule = 5.50 USDC
    GV_CELLULE = 5.50
    # Frais Binance standards : 0.1% buy + 0.1% sell = 0.20%
    FRAIS_BINANCE_PCT = 0.0020
    
    # Historiquement, tu as eu deux grilles (une serrée à 0.28% et une large à 0.63%)
    # On applique une moyenne conservatrice de l'espacement brut à 0.40% pour l'historique global
    espacement_moyen_pct = 0.0040 
    
    # Si la majorité de tes trades récents sont sur la nouvelle grille, décommente la ligne ci-dessous :
    # espacement_moyen_pct = 0.0063
    
    gain_brut_par_paire = GV_CELLULE * espacement_moyen_pct
    frais_par_paire     = GV_CELLULE * FRAIS_BINANCE_PCT
    slippage_par_paire  = GV_CELLULE * (avg_slippage / 100) * 2 # Aller + Retour
    
    gain_net_par_paire  = gain_brut_par_paire - frais_par_paire - slippage_par_paire
    total_gains_est     = pairs_completed * gain_net_par_paire
    
    print(f"\n🪙 ESTIMATION FINANCIÈRE (Base Gv = {GV_CELLULE}$) :")
    print(f"   Gain net estimé / paire : {gain_net_par_paire:.4f} USDC")
    print(f"   💸 GAIN NET TOTAL       : {total_gains_est:.2f} USDC")
    print(f"   (Frais Binance déduits  : ~{pairs_completed * frais_par_paire:.2f} USDC)")

if len(buys) >= 2 and len(sells) >= 2:
    first_trade = min(buys[0][0], sells[0][0])
    last_trade = max(buys[-1][0], sells[-1][0])
    duration_mins = (last_trade - first_trade).total_seconds() / 60
    trades_per_hour = total_trades / (duration_mins / 60) if duration_mins > 0 else 0
    print(f"\n⏱️  Cadence des trades      : {trades_per_hour:.1f} trades / heure")
print(f"============================================")
