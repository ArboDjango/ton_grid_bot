import time
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ==========================================
# CONFIGURATION INITIALE
# ==========================================
API_KEY = "Xx7qD3efMitPfAzR98kBhXMiCDQRA4YYbS0iIQrFV4dVrC5rjkam22p4FmMhi03D"
API_SECRET = "IXVwJ03j30W77BXSrLQfgCsSI7XHFXEWPmmmIKNtakKdsitl92OFzFzvET7tKr7j"

SYMBOL = "INJUSDC"
BASE_ASSET = "INJ"      
QUOTE_ASSET = "USDC"    

# --- OPTIMISATION DU NOMBRE DE NIVEAUX (Selon la documentation SSO) ---
# Formule théorique : Niveaux = (Prix * Baisse_Max_Absorbable_%) / (ATR * ATR_MULT)
# - Objectif : Couvrir mathématiquement les cycles de survente (RSI) et les mèches de capitulation.
# - Recommandation : 8 (Équilibre parfait gain/sécurité), 6 (Plus agressif), 10-12 (Prudent/Marché baissier).
# - Impact : Détermine la taille de chaque palier d'achat/vente (Capital Total / TOTAL_LEVELS_TARGET).
TOTAL_LEVELS_TARGET = 8

# Initialisation du client Binance
client = Client(API_KEY, API_SECRET)

# Configuration des logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("/home/arbodjango/ton_grid_bot/bot_activity.log"), logging.StreamHandler()]
)

last_trade_bar_time = None

# ==========================================
# FONCTIONS TECHNIQUE & PORTEFEUILLE
# ==========================================

def get_balances_and_total_wallet_value(current_price):
    """Calcule les soldes et la valeur totale du portefeuille en USDC (INJ + USDC)"""
    try:
        account = client.get_account()
        balances = {item['asset']: float(item['free']) for item in account['balances'] if float(item['free']) > 0}
        
        usdc_balance = balances.get(QUOTE_ASSET, 0.0)
        inj_balance = balances.get(BASE_ASSET, 0.0)
        
        # Valeur totale = USDC disponible + (Quantité d'INJ * son prix actuel en USDC)
        total_wallet_value = usdc_balance + (inj_balance * current_price)
        
        return balances, usdc_balance, inj_balance, total_wallet_value
    except Exception as e:
        logging.error(f"Erreur lors du calcul du portefeuille : {e}")
        return {}, 0.0, 0.0, 0.0

def get_market_data():
    """Récupère les données de prix et calcule la volatilité (ATR)"""
    try:
        candles = client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_3MINUTE, limit=15)
        current_price = float(candles[-1][4]) 
        current_bar_time = candles[-1][0]     
        
        high_low_diffs = [float(c[2]) - float(c[3]) for c in candles[:-1]]
        atr_value = sum(high_low_diffs) / len(high_low_diffs)
        
        return current_price, atr_value, current_bar_time
    except Exception as e:
        logging.error(f"Erreur données marché : {e}")
        return None, None, None

def execute_market_order(side, qty):
    """Exécute l'ordre Spot au marché"""
    try:
        qty_rounded = round(qty, 2) # Arrondi strict pour INJ
        if qty_rounded <= 0:
            return False
            
        logging.info(f"Envoi ordre {side} : {qty_rounded} {BASE_ASSET}")
        order = client.create_order(
            symbol=SYMBOL,
            side=side,
            type=Client.ORDER_TYPE_MARKET,
            quantity=qty_rounded
        )
        return True
    except BinanceAPIException as e:
        logging.error(f"Erreur Binance API : {e.message}")
        return False

# ==========================================
# BOUCLE PRINCIPALE
# ==========================================
logging.info(f"🚀 Démarrage du robot Auto-Adaptatif INJ/USDC...")

current_price, atr_value, current_bar_time = get_market_data()
if current_price and atr_value:
    balances, usdc_balance, inj_balance, total_portfolio_usdc = get_balances_and_total_wallet_value(current_price)
    step = atr_value * 1.4  
    logging.info(f"🔄 Grille INITIALE INJ. Prix: {current_price:.4f} | Prochain Achat: {current_price - step:.4f} | Prochaine Vente: {current_price + step:.4f}")
    logging.info(f"💼 Portefeuille DE DÉPART: {total_portfolio_usdc:.2f} USDC (Solde: {usdc_balance:.2f} USDC | Réserve: {inj_balance:.2f} INJ)")

# 🟢 TRÈS IMPORTANT : ON PLACE LES MEMOIRES DE GRILLE ICI (HORS DE LA BOUCLE)
last_grid_bar_time = None
target_buy_price = None
target_sell_price = None

# --- BOUCLE DE TRADING SILENCIEUSE ---
while True:
    current_price, atr_value, current_bar_time = get_market_data()
    
    if current_price and atr_value:
        balances, usdc_balance, inj_balance, total_portfolio_usdc = get_balances_and_total_wallet_value(current_price)
        
        if total_portfolio_usdc > 0:
	    # 1. FIXATION DE LA GRILLE PAR BOUGIE
            if last_grid_bar_time is None or current_bar_time > last_grid_bar_time or target_buy_price is None:
                volatility_ratio = (atr_value / current_price) * 100
                atr_mult = 2.0 if volatility_ratio > 0.5 else 1.4
                
                step = atr_value * atr_mult
                target_buy_price = current_price - step
                target_sell_price = current_price + step
                last_grid_bar_time = current_bar_time  # On bloque le temps de la grille
                
                # 🟢 LIGNE DE LOG ENRICHIE AVEC LES SOLDES ET LA VALEUR TOTALE :
                logging.info(
                    f"🎯 Grille réajustée | Prix: {current_price:.4f} "
                    f"| Target Achat: {target_buy_price:.4f} | Target Vente: {target_sell_price:.4f} "
                    f"| Portefeuille: {total_portfolio_usdc:.2f} USDC (Solde: {usdc_balance:.2f} USDC | Réserve: {inj_balance:.2f} INJ)"
                )            
            
            # 2. TAILLE DE POSITION
            target_trade_size_usdc = total_portfolio_usdc / TOTAL_LEVELS_TARGET
            if target_trade_size_usdc < 5.5:
                target_trade_size_usdc = 5.5
            
            # 🟢 AFFICHAGE DE CONTROLE (TEMPORAIRE)
            # print(f"DEBUG - Prix Binance: {current_price} | Cible FIXE Achat: {target_buy_price:.4f} | Cible FIXE Vente: {target_sell_price:.4f}")
            
            # 3. LOGIQUE DE TRADING (Anti-mitraillage)
            if last_trade_bar_time is None or current_bar_time > last_trade_bar_time:
                
                # ---- LOGIQUE D'ACHAT ----
                if current_price <= target_buy_price:
                    if usdc_balance >= target_trade_size_usdc:
                        qty_to_buy = target_trade_size_usdc / current_price
                        success = execute_market_order(Client.SIDE_BUY, qty_to_buy)
                        if success:
                            logging.info(f"⚡ ACHAT ADAPTATIF RÉUSSI : {qty_to_buy:.2f} INJ (Prix: {current_price:.4f})")
                            last_trade_bar_time = current_bar_time
                            target_buy_price = None  # Force la grille à se réinitialiser
                    else:
                        logging.warning(f"Achat requis mais solde USDC insuffisant.")
                
                # ---- LOGIQUE DE VENTE ----
                elif current_price >= target_sell_price:
                    qty_to_sell = target_trade_size_usdc / current_price
                    if inj_balance >= qty_to_sell:
                        success = execute_market_order(Client.SIDE_SELL, qty_to_sell)
                        if success:
                            logging.info(f"💰 VENTE ADAPTATIVE RÉUSSIE : {qty_to_sell:.2f} INJ (Prix: {current_price:.4f})")
                            last_trade_bar_time = current_bar_time
                            target_sell_price = None  # Force la grille à se réinitialiser
                    else:
                        logging.warning(f"Vente requise mais réserve d'INJ insuffisante.")

    time.sleep(10)
