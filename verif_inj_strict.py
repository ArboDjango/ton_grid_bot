#!/usr/bin/env python3
import os
import sys
from datetime import datetime
try:
    from dotenv import load_dotenv
    load_dotenv()
    from binance.client import Client
except ImportError:
    print("⚠️ Dépendances manquantes. Installe python-dotenv et python-binance.")
    sys.exit(1)

def verifier_inj_strict(client, date_depart_str, usdc_injecte):
    print(f"\n 🔍 AUDIT ET COMPARAISON STRICTE POUR INJ/USDC")
    print("═" * 65)
    
    # 1. Données Live depuis Binance
    ticker = client.get_ticker(symbol="INJUSDC")
    prix_actuel = float(ticker["lastPrice"])
    
    account = client.get_account()
    solde_inj_live = next(float(b["free"]) + float(b["locked"]) for b in account["balances"] if b["asset"] == "INJ")
    
    # 2. Flux API avec Fix 3 : startTime ajouté pour ne pas manquer de trades
    date_depart_ts = int(datetime.strptime(date_depart_str, "%Y-%m-%d").timestamp() * 1000)
    all_trades = client.get_my_trades(symbol="INJUSDC", startTime=date_depart_ts, limit=1000)
    trades_periode = all_trades 
    
    crypto_flux = 0.0
    usdc_flux = 0.0
    frais_usdc_totaux = 0.0
    prix_premier_trade = float(trades_periode[0]["price"]) if trades_periode else prix_actuel

    # Fix 1 & 2 — Récupération prix BNB une seule fois (approximation au prix actuel)
    bnb_price = float(client.get_symbol_ticker(symbol="BNBUSDC")["price"])
    
    for t in trades_periode:
        qty = float(t["qty"])
        price = float(t["price"])
        notional = qty * price
        
        fee = float(t["commission"])
        fee_asset = t["commissionAsset"]
        
        # Fix 1 & 2 — Logique frais améliorée
        if fee_asset == "USDC":
            fee_in_usdc = fee
        elif fee_asset == "INJ":
            fee_in_usdc = fee * price
        elif fee_asset == "BNB":
            fee_in_usdc = fee * bnb_price
        else:
            fee_in_usdc = 0.0
        frais_usdc_totaux += fee_in_usdc

        # Calcul flux (achat/vente)
        if t["isBuyer"]:
            crypto_flux += qty
            usdc_flux -= notional
        else:
            crypto_flux -= qty
            usdc_flux += notional

    # 3. Calcul financier
    cash_bot_final = usdc_injecte + usdc_flux
    crypto_reconstruite_depart = solde_inj_live - crypto_flux
    
    valeur_hold_final = (solde_inj_live * prix_actuel)
    # Fix 2 — Frais déjà sortis du wallet BNB, ne pas les déduire une 2e fois ici
    valeur_portefeuille_actuel = (solde_inj_live * prix_actuel) + cash_bot_final
    
    # Calcul PnL
    pnl_bot_strict = valeur_portefeuille_actuel - (usdc_injecte + (crypto_reconstruite_depart * prix_premier_trade))
    pnl_hold_strict = valeur_hold_final - (crypto_reconstruite_depart * prix_premier_trade)
    alpha = pnl_bot_strict - pnl_hold_strict

    # 4. Affichage
    print(f"    {'Frais BNB (informatif)':<25} │ {-frais_usdc_totaux:>11.4f} $")
    print(f"    {'Valeur INJ':<25} │ {solde_inj_live * prix_actuel:>11.2f} $")
    print(f"    {'Composante Cash/USDC':<25} │ {cash_bot_final:>11.2f} $")
    print(f"    {'VALEUR TOTALE':<25} │ {valeur_portefeuille_actuel:>11.2f} $")
    print(f"    {'PnL Global Net':<25} │ {pnl_bot_strict:>+11.2f} $")
    print("═" * 65)
    print(f" 🏆 SURPERFORMANCE (ALPHA) : {alpha:>+11.2f} $")

if __name__ == "__main__":
    # Récupération des clés
    API_KEY = os.getenv("BINANCE_API_KEY")
    API_SECRET = os.getenv("BINANCE_API_SECRET")
    
    if not API_KEY:
        print("❌ Erreur : Clés API manquantes dans le .env")
        sys.exit(1)
        
    client = Client(API_KEY, API_SECRET)
    # Lance l'audit depuis le 21 mai avec ton capital injecté
    verifier_inj_strict(client, "2026-05-21", 170.0)
