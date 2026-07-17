import time
import logging
import math
import pandas as pd
import ta
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ── Credentials & Config ───────────────────────────────────────
API_KEY = "Xx7qD3efMitPfAzR98kBhXMiCDQRA4YYbS0iIQrFV4dVrC5rjkam22p4FmMhi03D"
API_SECRET = "IXVwJ03j30W77BXSrLQfgCsSI7XHFXEWPmmmIKNtakKdsitl92OFzFzvET7tKr7j"
SYMBOL      = "INJUSDC"
BASE_ASSET  = "INJ"
QUOTE_ASSET = "USDC"

MIN_ORDER_USDC = 5.5   # Minimum strict imposé par l'API Binance
LOOP_SLEEP = 3         # Fréquence de scan (3 secondes optimale pour le T320)

# Configuration stricte des Logs (Fichier + Console)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("flexible_grid_live.log"), logging.StreamHandler()]
)

client = Client(API_KEY, API_SECRET)

# =======================================================================
# 1. PARAMÈTRES ET CONFIGURATIONS API BINANCE
# =======================================================================
def get_symbol_precisions():
    """Récupère proprement les précisions de prix et de quantité depuis les filtres Binance."""
    try:
        info = client.get_symbol_info(SYMBOL)
        price_precision = int(info.get('pricePrecision', 4))
        qty_precision = int(info.get('quantityPrecision', 2))
        
        # Lecture avancée dans les filtres si nécessaire
        for f in info.get('filters', []):
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = f['tickSize']
                price_precision = int(round(-math.log10(float(tick_size))))
            elif f['filterType'] == 'LOT_SIZE':
                step_size = f['stepSize']
                qty_precision = int(round(-math.log10(float(step_size))))
                
        return price_precision, qty_precision
    except Exception as e:
        logging.error(f"❌ Erreur lors de la récupération des précisions Binance : {e}")
        return 4, 2  # Sécurité de secours par défaut
def get_balances(current_price):
    """Interroge le portefeuille Spot réel et calcule la valeur totale."""
    try:
        account = client.get_account()
        balances = {b['asset']: float(b['free']) for b in account['balances'] if float(b['free']) > 0}
        usdc_bal = balances.get(QUOTE_ASSET, 0.0)
        inj_bal  = balances.get(BASE_ASSET,  0.0)
        total_value = usdc_bal + (inj_bal * current_price)
        return usdc_bal, inj_bal, total_value
    except Exception as e:
        logging.error(f"❌ Erreur de lecture du portefeuille Binance : {e}")
        return 0.0, 0.0, 0.0

def execute_live_order(side, qty_usdc, current_price, qty_precision):
    """Formate la quantité et transmet un ordre MARKET réel à Binance."""
    try:
        raw_qty = qty_usdc / current_price
        qty_asset = round(raw_qty, qty_precision)
        
        # Nettoyage strict des décimales pour éviter le rejet des types float par l'API
        factor = 10 ** qty_precision
        qty_asset = math.floor(qty_asset * factor) / factor

        if qty_asset <= 0:
            logging.warning(f"⚠️ Quantité calculée trop faible ({raw_qty}), ordre annulé.")
            return False

        logging.info(f"🛒 [ORDRE REAL LIVE] Transmission -> {side} {qty_asset} {BASE_ASSET} (~{qty_usdc:.2f} USDC)")
        client.create_order(symbol=SYMBOL, side=side, type=Client.ORDER_TYPE_MARKET, quantity=qty_asset)
        return True
    except BinanceAPIException as e:
        logging.error(f"❌ Ordre rejeté par Binance : {e.message}")
        return False
    except Exception as e:
        logging.error(f"❌ Erreur critique lors de l'exécution de l'ordre : {e}")
        return False

# =======================================================================
# 2. STRATE ACADÉMIQUE DE TSING HUA (IA & STRUCTURATION DE LA GRILLE)
# =======================================================================
def mock_ann_sso_model(price, atr, adx, rsi):
    """
    Simule le modèle prédictif ANN + SSO décrit dans le papier de recherche.
    L'IA module dynamiquement l'asymétrie de la grille selon l'état des indicateurs.
    """
    if adx < 25:
        # Régime stable (Range) : Grille serrée pour capter le micro-scalping
        gul_pct, gll_pct, nu, nl = 0.07, 0.07, 5, 5
    else:
        # Régime nerveux (Trend naissant) : On élargit et on augmente les opportunités d'achat
        gul_pct, gll_pct, nu, nl = 0.12, 0.12, 4, 6 
        
    # Ajustement adaptatif basé sur le surachat/survente du RSI
    if rsi > 65:
        gul_pct += 0.02
        nu += 1 
    elif rsi < 35:
        gll_pct += 0.02
        nl += 1
    return price * (1.0 + gul_pct), price * (1.0 - gll_pct), nu, nl

def compute_grid_ratios(P0, Gul, Gll, nu, nl):
    """Calcule les coefficients multiplicatifs asymétriques Gsu et Gsl."""
    Gsu = math.pow(P0 / Gul, 1.0 / nu)
    Gsl = math.pow(P0 / Gll, 1.0 / nl)
    return Gsu, Gsl

def generate_static_grid(P0, Gsu, Gsl, nu, nl):
    """Génère la matrice géométrique des niveaux de prix."""
    sells = [P0 / math.pow(Gsu, i) for i in range(1, nu + 1)]
    buys = [P0 / math.pow(Gsl, i) for i in range(1, nl + 1)]
    return sorted(sells, reverse=True) + [P0] + sorted(buys, reverse=True)

def get_market_data():
    """Récupère le flux de données récentes de l'API et extrait les indicateurs techniques."""
    try:
        klines = client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_3MINUTE, limit=50)
        df = pd.DataFrame(klines, columns=['time','open','high','low','close','volume','c_time','qav','t','b','t_b','i'])
        for col in ['open','high','low','close','volume']:
            df[col] = df[col].astype(float)
        
        # Strate mathématique temps réel
        df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
        df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
        df['rsi'] = ta.momentum.rsi(df['close'], window=14)
        
        last = df.iloc[-1]
        return {"price": float(last['close']), "atr": float(last['atr']), "adx": float(last['adx']), "rsi": float(last['rsi'])}
    except Exception as e:
        logging.error(f"❌ Erreur lors du calcul des données marché : {e}")
        return None

# =======================================================================
# 3. BOUCLE DE RUN EXECUTIVE (TIMING SERVEUR + MULTI-CROSS)
# =======================================================================
logging.info("🔥 Lancement du Robot Flexible Grid V6.2 — Spécial Portefeuille 50/50 & Multi-Cross")

price_precision, qty_precision = get_symbol_precisions()
grid_initialized = False
full_grid_prices = []
current_position_idx = 0
nu_current, nl_current = 0, 0
Gv_buy, Gv_sell = MIN_ORDER_USDC, MIN_ORDER_USDC

# Ancrage permanent du Pivot initial
P0 = 0.0

# Variable de contrôle temporel strict pour ton Dell T320
NEXT_RUN = time.time()

while True:
    current_time = time.time()
    if current_time < NEXT_RUN:
        time.sleep(0.1) # Repos processeur ultra-léger (Évite la surcharge CPU)
        continue
        
    # Synchronisation parfaite toutes les 3 secondes exactes, sans dérive de calcul
    NEXT_RUN = current_time + LOOP_SLEEP

    data = get_market_data()
    if not data:
        continue

    price = data['price']
    adx   = data['adx']
    rsi   = data['rsi']
    atr   = data['atr']

    usdc_bal, inj_bal, total_wallet = get_balances(price)

    # ── FILTRE SÉCURITÉ ACTIVE : COUPE-CIRCUIT ADX ──
    if adx > 35:
        logging.warning(f"⛔ Marché trop directionnel détecté (ADX={adx:.1f}) — Suspension temporaire des ordres.")
        continue

    # ── INITIALISATION UNIQUE DU PIVOT (ÉVITE LE RESET INTEMPESTIF) ──
    if not grid_initialized:
        P0 = price
        Gul, Gll, nu_current, nl_current = mock_ann_sso_model(P0, atr, adx, rsi)
        Gsu, Gsl = compute_grid_ratios(P0, Gul, Gll, nu_current, nl_current)
        full_grid_prices = generate_static_grid(P0, Gsu, Gsl, nu_current, nl_current)
        
        current_position_idx = nu_current  # Position initiale du curseur sur le niveau central P0
        
        # Équilibrage intelligent : On calibre la taille des ACHATS uniquement sur l'USDC disponible
        Gv_buy = usdc_bal / nl_current
        Gv_buy = max(MIN_ORDER_USDC, min(Gv_buy, 35.0))
        
        # Équilibrage intelligent : On calibre la taille des VENTES sur la valeur de tes INJ disponibles
        valeur_inj_usdc = inj_bal * price
        Gv_sell = valeur_inj_usdc / nu_current
        Gv_sell = max(MIN_ORDER_USDC, min(Gv_sell, 35.0))
        
        grid_initialized = True
        logging.info(f"🟢 [ANCRAGE IA RÉUSSI] Grille verrouillée sur le Pivot Initial P0 = {P0:.4f}")
        logging.info(f"   Ajustement Capital -> Taille d'Achat : {Gv_buy:.2f} USDC | Taille de Vente : {Gv_sell:.2f} USDC")
        logging.info(f"   Paliers d'Achats configurés : {[round(x, price_precision) for x in full_grid_prices[current_position_idx+1:]]}")
        logging.info(f"   Paliers de Ventes configurés : {[round(x, price_precision) for x in full_grid_prices[:current_position_idx]]}")

    # SÉCURITÉ EXTÉRIEURE : Si le prix s'échappe de l'espace de la grille, on temporise sans reset la mémoire
    if price > full_grid_prices[0] or price < full_grid_prices[-1]:
        logging.warning(f"⚠️ Le prix ({price:.3f}) est en dehors de la zone d'arbitrage [{full_grid_prices[-1]:.3f} - {full_grid_prices[0]:.3f}]. En attente de réintégration.")
        print(f"💰 [LIVE EXTÉRIEUR] Valeur Portefeuille: {total_wallet:.2f} USDC")
        time.sleep(5)
        continue

    # ── BALAYAGE AGRESSIF ET GESTION DES MULTI-CROSS ──

    # --- CAS DES VENTES EN RAFALE (Mèche impulsive vers le haut) ---
    while True:
        next_sell_idx = current_position_idx - 1
        
        if next_sell_idx >= 0 and price >= full_grid_prices[next_sell_idx]:
            target_price = full_grid_prices[next_sell_idx]
            qty_needed = Gv_sell / price
            
            if inj_bal >= (qty_needed * 0.98): # Tolérance pour couvrir les frais Binance
                if execute_live_order(Client.SIDE_SELL, Gv_sell, price, qty_precision):
                    logging.info(f"💥 [MULTI-CROSS SELL] Niveau {target_price:.{price_precision}f} franchi et vendu !")
                    current_position_idx = next_sell_idx # Le curseur monte d'un niveau
                    inj_bal -= qty_needed                 # Déduction locale immédiate pour le prochain tour de boucle
                    continue                             # Re-test instantané du niveau supérieur suivant
            else:
                logging.warning(f"⚠️ Multi-cross SELL bloqué : Solde de jetons INJ insuffisant ({inj_bal:.3f} INJ).")
                break
        else:
            break # Aucun autre niveau supérieur n'est traversé par le prix actuel, on sort de la boucle de vente

    # --- CAS DES ACHATS EN RAFALE (Chute verticale à travers plusieurs lignes) ---
    while True:
        next_buy_idx = current_position_idx + 1
        
        if next_buy_idx < len(full_grid_prices) and price <= full_grid_prices[next_buy_idx]:
            target_price = full_grid_prices[next_buy_idx]
            
            # Filtre de momentum RSI pour empêcher de racheter le fond d'un crash vertical sans stabilisation
            if rsi < 25:
                logging.warning(f"⚠️ Multi-cross BUY suspendu au niveau {target_price:.{price_precision}f} : RSI trop bas ({rsi:.1f})")
                break
                
            if usdc_bal >= Gv_buy:
                if execute_live_order(Client.SIDE_BUY, Gv_buy, price, qty_precision):
                    logging.info(f"💥 [MULTI-CROSS BUY] Niveau {target_price:.{price_precision}f} franchi et acheté !")
                    current_position_idx = next_buy_idx # Le curseur descend d'un niveau
                    usdc_bal -= Gv_buy                   # Déduction locale immédiate pour le prochain tour de boucle
                    continue                             # Re-test instantané du niveau inférieur suivant
            else:
                logging.warning(f"⚠️ Multi-cross BUY bloqué : Solde USDC disponible insuffisant ({usdc_bal:.2f} USDC).")
                break
        else:
            break # Aucun autre niveau inférieur n'est traversé, on sort de la boucle d'achat

    # Log d'état régulier imprimé à l'écran
    print(f"💰 [LIVE INJ] Prix: {price:.3f} | Portefeuille: {total_wallet:.2f} USDC | Position Index Grille: {current_position_idx} | Pivot Actif: {P0:.3f}")
