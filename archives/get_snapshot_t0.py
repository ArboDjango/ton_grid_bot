#!/usr/bin/env python3
from binance.client import Client
import os
from datetime import datetime
from dotenv import load_dotenv

# Charger les clés API
load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

# Date Pivot : 22 mai 2026 00:00:00
PIVOT_TS = int(datetime(2026, 5, 22).timestamp() * 1000)

PAIRES = {
    "INJ": {"symbol": "INJUSDC", "asset": "INJ"},
    "EGLD": {"symbol": "EGLDUSDC", "asset": "EGLD"}
}

print(f"🔍 Calcul de votre situation réelle au 22 mai 2026...")
print("-" * 50)

snapshot = {"date_reference": "2026-05-22"}

for name, cfg in PAIRES.items():
    # 1. Récupérer tous les trades depuis le 22 mai
    trades = client.get_my_trades(symbol=cfg['symbol'], startTime=PIVOT_TS)
    
    # 2. Calculer le flux net crypto (Somme des achats - ventes)
    flux_crypto = sum(float(t['qty']) if t['isBuyer'] else -float(t['qty']) for t in trades)
    
    # 3. Récupérer le solde actuel réel sur Binance
    balance = client.get_asset_balance(asset=cfg['asset'])
    solde_actuel = float(balance['free']) + float(balance['locked'])
    
    # 4. Calcul du stock T0
    stock_t0 = solde_actuel - flux_crypto
    
    snapshot[name] = {"stock": round(stock_t0, 4)}
    
    print(f"Paire {name}:")
    print(f"  Flux net depuis le 22 mai: {flux_crypto:+.4f}")
    print(f"  Solde actuel:              {solde_actuel:.4f}")
    print(f"  => STOCK AU 22 MAI (T0):   {stock_t0:.4f}")
    print("-" * 30)

# Optionnel : Sauvegarder dans un fichier JSON
import json
with open('snapshot_t0.json', 'w') as f:
    json.dump(snapshot, f, indent=4)
print("\n✅ Snapshot sauvegardé dans 'snapshot_t0.json'")
