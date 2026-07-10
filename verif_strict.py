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

def verifier_paire_strict(client, asset, symbol, date_depart_str):
    print(f"\n 🔍 RÉCONCILIATION STRICTE POUR {asset}/USDC")
    print("═" * 55)
    
    # 1. Récupération des données Live
    print(" 📡 Récupération des soldes et prix live...")
    ticker = client.get_ticker(symbol=symbol)
    prix_actuel = float(ticker["lastPrice"])
    
    account = client.get_account()
    solde_crypto_live = next(float(b["free"]) + float(b["locked"]) for b in account["balances"] if b["asset"] == asset)
    solde_usdc_live = next(float(b["free"]) + float(b["locked"]) for b in account["balances"] if b["asset"] == "USDC")
    
    # 2. Récupération historique des trades
    print(" 📜 Téléchargement de l'historique des flux API...")
    date_depart_ts = int(datetime.strptime(date_depart_str, "%Y-%m-%d").timestamp() * 1000)
    all_trades = client.get_my_trades(symbol=symbol, limit=1000)
    trades_periode = [t for t in all_trades if t["time"] >= date_depart_ts]
    
    # 3. Remonter le temps (Calcul du Rollback)
    crypto_flux = 0.0
    usdc_flux = 0.0
    frais_usdc_totaux = 0.0
    prix_premier_trade = float(trades_periode[0]["price"]) if trades_periode else prix_actuel
    
    for t in trades_periode:
        qty = float(t["qty"])
        price = float(t["price"])
        notional = qty * price
        
        # Traitement des frais
        fee = float(t["commission"])
        fee_asset = t["commissionAsset"]
        fee_in_usdc = fee if fee_asset == "USDC" else (fee * price if fee_asset == asset else 0.0)
        frais_usdc_totaux += fee_in_usdc
        
        if t["isBuyer"]:
            crypto_flux += qty
            usdc_flux -= notional
        else:
            crypto_flux -= qty
            usdc_flux += notional
            
    # Reconstruction de la Vérité Terrain au 21 Mai
    crypto_depart = solde_crypto_live - crypto_flux
    # Note: On isole la part d'USDC liée aux mouvements du bot
    
    valeur_portefeuille_depart = (crypto_depart * prix_premier_trade)
    valeur_portefeuille_actuel = (solde_crypto_live * prix_actuel) + usdc_flux
    
    pnl_strict_usdc = valeur_portefeuille_actuel - valeur_portefeuille_depart - frais_usdc_totaux
    performance_pct = (pnl_strict_usdc / valeur_portefeuille_depart * 100) if valeur_portefeuille_depart > 0 else 0.0
    
    # 4. Affichage du rapport d'audit
    print("═" * 55)
    print(f" 🗓️ État reconstruit au {date_depart_str} (Prix moyen initial : {prix_premier_trade:.4f}$)")
    print(f"    Stock Crypto Initial : {crypto_depart:.4f} {asset} (Valeur : {valeur_portefeuille_depart:.2f} USDC)")
    print("─" * 55)
    print(f" 📈 État Live Actuel (Prix : {prix_actuel:.4f}$)")
    print(f"    Stock Crypto Actuel  : {solde_crypto_live:.4f} {asset}")
    print(f"    Variation nette flux : {crypto_flux:+.4f} {asset} │ {usdc_flux:+.2f} USDC")
    print("─" * 55)
    print(f" 💵 Frais de trading appliqués : {frais_usdc_totaux:.4f} USDC")
    print(f" 🏆 VRAI PnL RECONSTRUIT STRICT: {pnl_strict_usdc:+.4f} USDC ({performance_pct:+.2f}%)")
    print("═" * 55)

if __name__ == "__main__":
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        print("❌ Clés API introuvables. Vérifie ton fichier .env")
        sys.exit(1)
        
    client = Client(api_key, api_secret)
    # Lancement de l'audit strict depuis le 17 mai
    verifier_paire_strict(client, "INJ", "INJUSDC", "2026-05-17")
