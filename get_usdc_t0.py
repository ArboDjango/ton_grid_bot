#!/usr/bin/env python3
from binance.client import Client
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

# Date Pivot : 22 mai 2026 00:00:00
PIVOT_TS = int(datetime(2026, 5, 22).timestamp() * 1000)

PAIRES = ["INJUSDC", "EGLDUSDC"]

def get_flux_usdc_total():
    flux_total = 0
    for symbol in PAIRES:
        trades = client.get_my_trades(symbol=symbol, startTime=PIVOT_TS)
        for t in trades:
            # Si on achète (isBuyer=True), on dépense des USDC (négatif)
            # Si on vend (isBuyer=False), on gagne des USDC (positif)
            montant = float(t['qty']) * float(t['price'])
            if t['isBuyer']:
                flux_total -= montant
            else:
                flux_total += montant
    return flux_total

# 1. Solde actuel en USDC
balance_actuelle = float(client.get_asset_balance(asset='USDC')['free'])

# 2. Calcul du flux net (ce que les bots ont mangé/gagné en USDC depuis le 22)
flux_depuis_t0 = get_flux_usdc_total()

# 3. Solde T0 = Solde Actuel - Flux (on inverse le signe du flux)
# Si flux est positif (tu as gagné des USDC), alors tu en avais moins au T0
# Si flux est négatif (tu as dépensé des USDC), alors tu en avais plus au T0
solde_t0 = balance_actuelle - flux_depuis_t0

print(f"💰 CALCUL DU SOLDE USDC AU 22 MAI :")
print(f"  Solde actuel : {balance_actuelle:.2f} $")
print(f"  Flux USDC des trades depuis le 22 mai : {flux_depuis_t0:+.2f} $")
print(f"  --------------------------------------------------")
print(f"  SOLDE USDC AU 22 MAI (T0) : {solde_t0:.2f} $")
