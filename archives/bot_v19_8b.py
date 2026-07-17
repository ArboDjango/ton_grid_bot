import os
import sys
import time
import json
import math
import logging
import pandas as pd
import numpy as np
import ta
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ── GESTION SÉCURISÉE DES CLÉS API ────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  Attention: 'python-dotenv' n'est pas installé.")

API_KEY    = os.getenv("BINANCE_API_KEY", "...")
API_SECRET = os.getenv("BINANCE_API_SECRET", "...")
# ──────────────────────────────────────────────────────────────


# ── SÉLECTION DYNAMIQUE ET CLOISONNEMENT DE CAPITAL (V9.8) ─────
if len(sys.argv) > 1:
    SYMBOL = sys.argv[1].upper()
else:
    SYMBOL = "INJUSDC"

MAX_BUDGET_USDC = None
if len(sys.argv) > 2:
    try:
        MAX_BUDGET_USDC = float(sys.argv[2])
    except ValueError:
        print("❌ Le deuxième argument (Budget) doit être un nombre flottant ou entier.")
        sys.exit(1)

if SYMBOL.endswith("USDC"):
    BASE_ASSET  = SYMBOL.replace("USDC", "")
    QUOTE_ASSET = "USDC"
elif SYMBOL.endswith("USDT"):
    BASE_ASSET  = SYMBOL.replace("USDT", "")
    QUOTE_ASSET = "USDT"
else:
    print(f"❌ Paire {SYMBOL} non supportée. Fin en USDC ou USDT uniquement.")
    sys.exit(1)

# ── Paramètres grille adaptatifs ──────────────────────────────
NU_LEVELS    = 5
NL_LEVELS    = 5
NU_MIN, NU_MAX = 2, 10
NL_MIN, NL_MAX = 2, 10

CAPITAL_USDC = 0.0

ACTIVE_CAPITAL_RATIO = 0.8
MAX_CELL_RATIO       = 0.8
GV_MULTIPLIER        = 1.0

ATR_MIN_MULT  = 5.0
ATR_MAX_MULT  = 15.0
ATR_BASE_MULT = 7.0

GUL_HARD_MIN_PCT = 0.020
GUL_HARD_MAX_PCT = 0.15
GLL_HARD_MIN_PCT = 0.020
GLL_HARD_MAX_PCT = 0.15

PAPER_GUL_MIN_PCT = 0.05
PAPER_GUL_MAX_PCT = 0.50
PAPER_GLL_MIN_PCT = 0.05
PAPER_GLL_MAX_PCT = 0.95
TRADING_FEE_RT    = 0.00075  # Frais réels Binance avec BNB discount (0.075% par ordre)
EQ16_MIN_RATIO    = 2.0
EQ16_MAX_RETRIES  = 3

# ── SMART ROUTER CONFIGURATION ────────────────────────────────
STRESS_LIMIT_FOR_MAKER = 0.40
LIMIT_TIMEOUT_SECONDS  = 15

# ── Opérationnel & Haute Fréquence ─────────────────────────────
MIN_ORDER_USDC   = 5.5
KLINE_INTERVAL   = Client.KLINE_INTERVAL_3MINUTE
KLINE_LIMIT      = 100
LOOP_SLEEP       = 1
INDICATORS_FREQ  = 60
ADX_TREND_LIMIT  = 35
STATE_FILE       = f"state_{SYMBOL.lower()}.json"
FAILED_COOLDOWN  = 3
RECALC_CYCLES    = 60
GLOBAL_STOP_LOSS = 0.15

PRICE_DECIMALS = 4
QTY_DECIMALS   = 2

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"bot_{SYMBOL.lower()}.log"),
        logging.StreamHandler()
    ]
)

client = Client(API_KEY, API_SECRET)
if hasattr(client, 'session'):
    client.session.request_timeout = 15

# ═══════════════════════════════════════════════════════════════
# INTROSPECTION ET CLOISONNEMENT VIRTUEL (V9.8)
# ═══════════════════════════════════════════════════════════════

def get_instant_price() -> float | None:
    try:
        ticker = client.get_symbol_ticker(symbol=SYMBOL)
        return float(ticker['price'])
    except Exception as e:
        logging.error(f"❌ Erreur récupération prix instantané {SYMBOL} : {e}")
        return None

def get_balances(price: float) -> tuple[float, float, float]:
    try:
        acc  = client.get_account()
        bals = {b['asset']: float(b['free']) for b in acc['balances']}
        quote_bal = bals.get(QUOTE_ASSET, 0.0)
        base_bal  = bals.get(BASE_ASSET,  0.0)

        total_real = quote_bal + base_bal * price

        if MAX_BUDGET_USDC is not None:
            budget_ratio    = min(1.0, MAX_BUDGET_USDC / total_real) if total_real > 0 else 1.0
            quote_bal       = quote_bal * budget_ratio
            base_bal        = base_bal  * budget_ratio
            total_simulated = min(total_real, MAX_BUDGET_USDC)
            return quote_bal, base_bal, total_simulated

        return quote_bal, base_bal, total_real
    except Exception as e:
        logging.error(f"❌ Erreur mesure portefeuille : {e}")
        return 0.0, 0.0, 0.0

def adjust_levels_to_balance(quote_bal: float, base_bal_in_quote: float) -> tuple[int, int]:
    total = quote_bal + base_bal_in_quote
    if total == 0: return NU_LEVELS, NL_LEVELS

    crypto_ratio = base_bal_in_quote / total
    total_budget = NU_LEVELS + NL_LEVELS

    nu_raw      = round(total_budget * crypto_ratio)
    nu_adjusted = max(NU_MIN, min(NU_MAX, nu_raw))
    nl_raw      = total_budget - nu_adjusted
    nl_adjusted = max(NL_MIN, min(NL_MAX, nl_raw))

    if nl_adjusted != nl_raw:
        nu_adjusted = max(NU_MIN, min(NU_MAX, total_budget - nl_adjusted))

    logging.info(f"⚖️ Répartition isolée : {BASE_ASSET}={crypto_ratio*100:.1f}% | {QUOTE_ASSET}={(1-crypto_ratio)*100:.1f}%")
    logging.info(f"⚖️ Rééquilibrage structurel -> Cibles : BUY={nl_adjusted} | SELL={nu_adjusted}")
    return nu_adjusted, nl_adjusted

def get_symbol_precisions():
    global PRICE_DECIMALS, QTY_DECIMALS
    try:
        info = client.get_symbol_info(SYMBOL)
        if info is None:
            raise ValueError(f"Symbole {SYMBOL} introuvable sur Binance")
        found_price, found_qty = False, False
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = float(f['tickSize'])
                if tick_size > 0:
                    PRICE_DECIMALS = int(round(-math.log10(tick_size)))
                    found_price = True
            if f['filterType'] == 'LOT_SIZE':
                step_size = float(f['stepSize'])
                if step_size > 0:
                    QTY_DECIMALS = int(round(-math.log10(step_size)))
                    found_qty = True
        if not found_price or not found_qty:
            raise ValueError("Filtres PRICE_FILTER ou LOT_SIZE manquants dans la réponse Binance")
        logging.info(f"⚙️ Précisions {SYMBOL} -> Prix: {PRICE_DECIMALS} déc. | Qté: {QTY_DECIMALS} déc.")
    except Exception as e:
        logging.error(f"❌ Précisions non chargées : {e} — arrêt pour sécurité.")
        sys.exit(1)

def load_state() -> dict:
    defaults = {
        "grid_ready": False,
        "P0": None, "Gul": None, "Gll": None, "Gsu": None, "Gsl": None, "Gv": None,
        "sell_grid": [], "buy_grid": [],
        "nu": NU_LEVELS, "nl": NL_LEVELS,   # valeurs de repli si init_grid n'a pas encore tourné
        "wallet_peak": 0.0, "total_trades": 0, "failed_count": 0,
        "total_slippage": 0.0, "cycle_recalc": 0,
        "capital_usdc": 0.0,
        # FIX 7 — suivi P&L : table prix d'achat par niveau de vente
        "buy_prices": {},
        "total_pnl": 0.0,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return {**defaults, **json.load(f)}
        except Exception as e:
            logging.warning(f"⚠️ Impossible de lire {STATE_FILE} : {e}")
    return defaults

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logging.error(f"❌ Erreur sauvegarde état : {e}")

# ═══════════════════════════════════════════════════════════════
# FONCTIONS INDICATEURS
# ═══════════════════════════════════════════════════════════════

def get_heavy_indicators() -> dict | None:
    try:
        klines_3m = client.get_klines(symbol=SYMBOL, interval=KLINE_INTERVAL, limit=KLINE_LIMIT)
        df_3m = pd.DataFrame(klines_3m, columns=['time','open','high','low','close','volume','ct','qav','trades','tbb','tbq','i'])
        for col in ['open','high','low','close']: df_3m[col] = df_3m[col].astype(float)

        atr = ta.volatility.average_true_range(df_3m['high'], df_3m['low'], df_3m['close'], window=14).iloc[-1]
        dip = ta.trend.adx_pos(df_3m['high'], df_3m['low'], df_3m['close'], window=14).iloc[-1]
        dim = ta.trend.adx_neg(df_3m['high'], df_3m['low'], df_3m['close'], window=14).iloc[-1]

        atr_series = ta.volatility.average_true_range(df_3m['high'], df_3m['low'], df_3m['close'], window=14)
        atr_median = float(atr_series.median())
        last_price = float(df_3m['close'].iloc[-1])
        atr_norm   = atr_median / last_price if last_price > 0 else 0.01

        klines_15m = client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_15MINUTE, limit=50)
        df_15m = pd.DataFrame(klines_15m, columns=['time','open','high','low','close','volume','ct','qav','trades','tbb','tbq','i'])
        for col in ['open','high','low','close']: df_15m[col] = df_15m[col].astype(float)
        adx_15m = ta.trend.adx(df_15m['high'], df_15m['low'], df_15m['close'], window=14).iloc[-1]

        return {
            "atr": float(atr),
            "adx": float(adx_15m),
            "dip": float(dip),
            "dim": float(dim),
            "atr_norm": float(atr_norm),
        }
    except Exception as e:
        logging.error(f"❌ Erreur get_heavy_indicators : {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# LOGIQUE MATHÉMATIQUE DU PAPIER
# ═══════════════════════════════════════════════════════════════

def compute_asymmetry(dip: float, dim: float, target_nu: int, target_nl: int) -> tuple[float, float, int, int, str]:
    ratio = dip / max(dim, 0.001)
    if ratio >= 1.2:
        strength = min((ratio - 1.2) / 1.8, 1.0)
        return 1.0 + 0.10 * strength, 1.0 - 0.05 * strength, min(NU_MAX, target_nu + round(2 * strength)), max(NL_MIN, target_nl - round(1 * strength)), f"BULLISH (ratio={ratio:.2f})"
    elif ratio <= 0.8:
        strength = min((0.8 - ratio) / 0.8, 1.0)
        return 1.0 - 0.05 * strength, 1.0 + 0.10 * strength, max(NU_MIN, target_nu - round(1 * strength)), min(NL_MAX, target_nl + round(2 * strength)), f"BEARISH (ratio={ratio:.2f})"
    return 1.0, 1.0, target_nu, target_nl, f"NEUTRAL (ratio={ratio:.2f})"

def compute_stress(adx: float, atr: float, price: float, drawdown: float, slippage_avg: float, atr_norm: float = 0.01) -> float:
    ms = min(1.0, (adx / 60.0) * 0.6 + (atr / price / atr_norm) * 0.4 * 0.33)
    ds = min(1.0, drawdown * 5.0)
    es = min(1.0, slippage_avg * 100.0)
    return float(ms * 0.50 + ds * 0.30 + es * 0.20)

def compute_dynamic_bounds(P0: float, atr: float, stress: float, gul_bias: float, gll_bias: float) -> tuple[float, float]:
    base     = ATR_BASE_MULT * (atr / P0)
    stressed = base * (1.0 + stress * 1.0)
    sup = max(GUL_HARD_MIN_PCT, min(stressed * gul_bias, GUL_HARD_MAX_PCT))
    inf = max(GLL_HARD_MIN_PCT, min(stressed / gll_bias, GLL_HARD_MAX_PCT))
    return (
        float(min(max(P0 * (1.0 + sup), P0 * (1 + GUL_HARD_MIN_PCT)), P0 * (1 + GUL_HARD_MAX_PCT))),
        float(min(max(P0 * (1.0 - inf), P0 * (1 - GLL_HARD_MAX_PCT)), P0 * (1 - GLL_HARD_MIN_PCT)))
    )

def compute_grid_ratios(P0: float, Gul: float, Gll: float, nu: int, nl: int) -> tuple[float, float]:
    # Papier Eq.13 : Gsu > 1, niveaux sell = P0 * Gsu^i  (au-dessus de P0, densité croissante)
    # Inverse de l'ancienne formule : on prend la racine de Gul/P0
    gsu = math.pow(Gul / P0, 1.0 / nu)          # > 1.0 ✓  ex: 1.02 par niveau

    # Papier Eq.14 : Gsl > 1, niveaux buy = Gll * Gsl^(i-1) (depuis Gll vers P0)
    # La racine nu-ième de P0/Gll donne le ratio entre niveaux consécutifs
    term = P0 / Gll if Gll > 0 else 1.01
    gsl  = math.pow(term, 1.0 / nl) if term > 0 else 1.01      # > 1.0 ✓  ex: 1.03 par niveau
    return gsu, gsl

def compute_gv(capital: float, P0: float, Gll: float, nu: int, nl: int, Gsl: float) -> float:
    # Eq.8  papier : S0 = Gv * nu * P0
    sell_denom = nu * P0
    # Eq.9  papier : C0 = Gv * [(P0 - Gs) + Gll] / 2 * nl
    # Pour la grille ratio Eq.14, gi_buy = Gll * Gsl^(i-1)
    # Somme exacte (série géométrique) : C0 = Gv * Gll * (Gsl^nl - 1) / (Gsl - 1)
    if abs(Gsl - 1.0) > 1e-9:
        buy_denom = Gll * (math.pow(Gsl, nl) - 1.0) / (Gsl - 1.0)
    else:
        buy_denom = Gll * nl   # cas dégénéré Gsl ≈ 1
    denom = sell_denom + buy_denom
    gv = (capital / denom) * GV_MULTIPLIER if denom > 0 else (capital / (nu + nl)) * GV_MULTIPLIER
    return max(MIN_ORDER_USDC, min(gv, capital * MAX_CELL_RATIO))

def enforce_eq16(P0: float, atr: float, Gul: float, Gll: float, nu: int, nl: int, stress: float, gub: float, glb: float) -> tuple[float, float, int, int]:
    # Papier Eq.16 : gi+1 - gi > h% * gi+1  pour TOUS les niveaux.
    # Le plus petit espacement est toujours le premier niveau (le plus proche de P0).
    # Sell : g1_sell = P0 * Gsu^1  →  espacement = P0*(Gsu-1)  avec Gsu = (Gul/P0)^(1/nu)
    # Buy  : g_last_buy = Gll * Gsl^(nl-1)  →  espacement = Gll*Gsl^(nl-1)*(Gsl-1)
    # On vérifie le minimum des deux.
    for attempt in range(EQ16_MAX_RETRIES + 1):
        gsu_tmp = math.pow(Gul / P0, 1.0 / nu)
        term    = P0 / Gll if Gll > 0 else 1.01
        gsl_tmp = math.pow(term, 1.0 / nl) if term > 0 else 1.01

        g1_sell    = P0 * gsu_tmp                          # premier niveau sell
        gap_sell   = g1_sell - P0                          # espacement minimum côté sell
        ok_sell    = gap_sell > TRADING_FEE_RT * EQ16_MIN_RATIO * g1_sell

        g_last_buy = Gll * math.pow(gsl_tmp, nl - 1)      # dernier niveau buy (le plus proche de P0)
        g_prev_buy = Gll * math.pow(gsl_tmp, nl - 2) if nl >= 2 else Gll
        gap_buy    = g_last_buy - g_prev_buy               # plus petit espacement côté buy
        ok_buy     = gap_buy > TRADING_FEE_RT * EQ16_MIN_RATIO * g_last_buy

        if ok_sell and ok_buy:
            return Gul, Gll, nu, nl

        nu, nl = max(2, nu - 1), max(2, nl - 1)
        Gul, Gll = compute_dynamic_bounds(P0, atr, min(stress + 0.15 * (attempt + 1), 1.0), gub, glb)
    return Gul, Gll, nu, nl

def init_grid(price: float, atr: float, state: dict, stress: float, dip: float, dim: float, force: bool = False):
    global CAPITAL_USDC

    # FIX 6 — bloquer UNIQUEMENT les recalculs périodiques optionnels quand ADX est fort.
    # Si force=True (premier démarrage, hors bornes, grille vide) on initialise quoi qu'il arrive.
    if not force and macro_data.get("adx", 0) > ADX_TREND_LIMIT:
        logging.warning(f"⏸️ init_grid bloqué (recalcul périodique) : ADX={macro_data['adx']:.1f} > {ADX_TREND_LIMIT}")
        return

    P0 = price

    quote_bal, base_bal, total_wallet = get_balances(P0)
    CAPITAL_USDC           = total_wallet
    state["capital_usdc"]  = total_wallet

    target_nu, target_nl = adjust_levels_to_balance(quote_bal, base_bal * P0)

    gub, glb, nu, nl, regime = compute_asymmetry(dip, dim, target_nu, target_nl)
    Gul, Gll = compute_dynamic_bounds(P0, atr, stress, gub, glb)
    Gul, Gll, nu, nl = enforce_eq16(P0, atr, Gul, Gll, nu, nl, stress, gub, glb)
    gsu, gsl = compute_grid_ratios(P0, Gul, Gll, nu, nl)

    # Eq.13 — niveaux SELL : P0 * Gsu^i  (Gsu > 1, croissants au-dessus de P0)
    sell_g = sorted([round(P0 * math.pow(gsu, i), PRICE_DECIMALS) for i in range(1, nu + 1)])
    # Eq.14 — niveaux BUY  : Gll * Gsl^(i-1)  (Gsl > 1, croissants depuis Gll vers P0)
    buy_g  = sorted([round(Gll * math.pow(gsl, i - 1), PRICE_DECIMALS) for i in range(1, nl + 1)], reverse=True)

    state.update({
        "grid_ready": True, "P0": P0, "Gul": Gul, "Gll": Gll, "Gsu": gsu, "Gsl": gsl,
        "nu": nu, "nl": nl, "sell_grid": sell_g, "buy_grid": buy_g,
        "Gv": compute_gv(CAPITAL_USDC * ACTIVE_CAPITAL_RATIO, P0, Gll, nu, nl, gsl)
    })
    if state["wallet_peak"] == 0.0: state["wallet_peak"] = CAPITAL_USDC
    save_state(state)

    limit_msg = f"Bridé à {MAX_BUDGET_USDC}$ via terminal" if MAX_BUDGET_USDC else "Solde total du compte"
    logging.info(f"🧮 Grille Initialisée ({regime}) | Mode Allocation: {limit_msg}")
    logging.info(f"💰 Cap Virtuel : {CAPITAL_USDC:.2f} {QUOTE_ASSET} | Gv={state['Gv']:.2f} | Niveaux: BUY={nl} SELL={nu}")

# ═══════════════════════════════════════════════════════════════
# MOTEUR SMART ROUTER
# ═══════════════════════════════════════════════════════════════

def execute_market_fallback(side: str, qty_asset: float, target_price: float, state: dict, operational_reason: str) -> float | None:
    try:
        order = client.create_order(symbol=SYMBOL, side=side, type=Client.ORDER_TYPE_MARKET, quantity=qty_asset)
        if order.get('status') == 'FILLED':
            fills        = order.get('fills', [])
            total_qty    = sum(float(f['qty'])   for f in fills)
            total_cost   = sum(float(f['price']) * float(f['qty']) for f in fills)
            actual_price = total_cost / total_qty if total_qty > 0 else target_price

            slippage = abs(actual_price - target_price) / target_price
            state["total_slippage"] += slippage
            state["total_trades"]   += 1
            state["failed_count"]    = 0
            logging.info(f"💥 [{operational_reason}] Rempli MARKET @ {actual_price:.4f} | Slippage: {slippage:.4%}")
            return actual_price
    except Exception as e:
        logging.error(f"❌ Échec critique du Fallback Market : {e}")
    state["failed_count"] += 1
    return None

def smart_execute_order(side: str, qty_usdc: float, target_price: float, state: dict, current_stress: float) -> float | None:
    # FIX 4 — vérification MIN_NOTIONAL avant tout passage d'ordre
    if qty_usdc < MIN_ORDER_USDC:
        logging.warning(f"⚠️ Ordre ignoré : {qty_usdc:.2f} {QUOTE_ASSET} < MIN_ORDER_USDC ({MIN_ORDER_USDC})")
        return None

    try:
        qty_asset = round(qty_usdc / target_price, QTY_DECIMALS)
        if qty_asset <= 0: return None

        if current_stress > STRESS_LIMIT_FOR_MAKER:
            return execute_market_fallback(side, qty_asset, target_price, state, "CAS 2 - DIRECT SWEEP")

        maker_price = target_price * (1 - 0.0001) if side == Client.SIDE_BUY else target_price * (1 + 0.0001)
        maker_price = round(maker_price, PRICE_DECIMALS)

        logging.info(f"🐢 [CAS 1 - MAKER] Stress faible ({current_stress:.2f}) ➔ Pose LIMIT @ {maker_price:.4f}")
        order = client.create_order(
            symbol=SYMBOL, side=side, type=Client.ORDER_TYPE_LIMIT,
            timeInForce=Client.TIME_IN_FORCE_GTC, quantity=qty_asset, price=f"{maker_price:.{PRICE_DECIMALS}f}"
        )
        order_id = order.get('orderId')

        adaptive_timeout = LIMIT_TIMEOUT_SECONDS * (1.0 + (1.0 - current_stress))
        start_time = time.time()
        while time.time() - start_time < adaptive_timeout:
            time.sleep(0.5)
            check = client.get_order(symbol=SYMBOL, orderId=order_id)

            if check.get('status') == 'FILLED':
                logging.info(f"✅ [MAKER SUCCESS] Ordre LIMIT exécuté @ {maker_price:.4f}")
                state["total_trades"]  += 1
                state["failed_count"]   = 0
                return maker_price

            if check.get('status') in ['CANCELED', 'REJECTED', 'EXPIRED']:
                return None

        logging.warning(f"⏳ [CAS 3 - TIMEOUT] LIMIT en attente depuis {adaptive_timeout:.0f}s ➔ Conversion Market active.")
        try:
            client.cancel_order(symbol=SYMBOL, orderId=order_id)
        except Exception:
            check = client.get_order(symbol=SYMBOL, orderId=order_id)
            if check.get('status') == 'FILLED': return maker_price

        return execute_market_fallback(side, qty_asset, target_price, state, "CAS 3 - MARKET FALLBACK")

    except Exception as e:
        logging.error(f"❌ Erreur critique Smart Router : {e}")
        state["failed_count"] += 1
        return None

def check_and_recycle_buyers(state: dict, current_price: float, atr: float) -> bool:
    """
    FIX 5 — Seuil de recyclage basé sur l'ATR plutôt qu'un pourcentage fixe de 5%.
    Un niveau buy est recyclé seulement s'il est plus loin que 2× ATR sous le prix courant.
    """
    if not state.get("Gsl"):
        return False
    buy_grid = state.get("buy_grid", [])
    if len(buy_grid) < 2:
        return False

    atr_threshold = current_price - 2.0 * atr   # FIX 5 : fenêtre dynamique
    if buy_grid[-1] >= atr_threshold:
        return False

    try:
        buy_grid.pop(-1)
        new_buy = round(current_price / state["Gsl"], PRICE_DECIMALS)  # Gsl>1 → sous le prix courant ✓
        buy_grid.append(new_buy)
        buy_grid.sort(reverse=True)
        state["buy_grid"] = buy_grid
        save_state(state)
        logging.info(f"🚀 RECYCLAGE HAUSSIER (ATR-based) -> Nouveau BUY à {new_buy:.4f}")
        return True
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE FLOTTE DE PRODUCTION V9.8
# ═══════════════════════════════════════════════════════════════

logging.info(f"🚀 Démarrage Moteur Quantitatif V9.8 — Target: {SYMBOL}")
get_symbol_precisions()
state = load_state()

macro_data = get_heavy_indicators()
while not macro_data:
    logging.warning("⚠️ Attente du flux d'indicateurs historiques (Klines API)...")
    time.sleep(10)
    macro_data = get_heavy_indicators()

last_macro_time = time.time()
last_log_time   = time.time()
stress          = 0.20

while True:
    try:
        price = get_instant_price()
        if not price:
            time.sleep(LOOP_SLEEP)
            continue

        # ── MISE À JOUR DES INDICATEURS MACRO (Toutes les 60 secondes réelles) ──
        if time.time() - last_macro_time >= INDICATORS_FREQ:
            last_macro_time = time.time()
            fresh_macro = get_heavy_indicators()
            if fresh_macro:
                macro_data = fresh_macro

        if state["failed_count"] >= FAILED_COOLDOWN:
            time.sleep(60)
            state["failed_count"] = 0
            continue

        quote_bal, base_bal, total_wallet = get_balances(price)
        if state["wallet_peak"] == 0.0 or total_wallet > state["wallet_peak"]:
            state["wallet_peak"] = total_wallet
        drawdown = max(0.0, 1.0 - total_wallet / state["wallet_peak"])

        if drawdown >= GLOBAL_STOP_LOSS:
            logging.critical(f"🚨 STOP-LOSS BOT ACTIF ({drawdown*100:.2f}%)")
            break

        out_of_bounds   = state["grid_ready"] and (price < state["Gll"] or price > state["Gul"])
        grid_empty      = state["grid_ready"] and len(state["sell_grid"]) == 0 and len(state["buy_grid"]) == 0
        state["cycle_recalc"] += 1
        periodic_recalc = (state["cycle_recalc"] >= RECALC_CYCLES * 60)

        # FIX 6 — force=True pour les cas obligatoires (pas de grille, hors bornes, vide)
        #          force=False (défaut) pour le recalcul périodique optionnel → bloquable par ADX
        must_init    = not state["grid_ready"] or out_of_bounds or grid_empty
        if must_init or periodic_recalc:
            # FIX 3 — reset du compteur systématique dans TOUS les cas d'init_grid
            state["cycle_recalc"] = 0
            slip_avg = state["total_slippage"] / max(1, state["total_trades"])
            stress   = compute_stress(macro_data["adx"], macro_data["atr"], price, drawdown, slip_avg, macro_data.get("atr_norm", 0.01))
            init_grid(price, macro_data["atr"], state, stress, macro_data["dip"], macro_data["dim"], force=must_init)
            quote_bal, base_bal, total_wallet = get_balances(price)

        # Grille pas encore prête (ex: init bloquée en cours de démarrage) → attendre
        if not state["grid_ready"]:
            time.sleep(LOOP_SLEEP)
            continue

        sell_grid = state["sell_grid"]
        buy_grid  = state["buy_grid"]

        # FIX 2 — Gv recalculé avec le capital frais à chaque cycle
        capital_effectif = total_wallet * ACTIVE_CAPITAL_RATIO
        Gv = compute_gv(capital_effectif, state["P0"], state["Gll"], state["nu"], state["nl"], state["Gsl"])

        if macro_data["adx"] > ADX_TREND_LIMIT:
            if time.time() - last_log_time >= 10.0:
                last_log_time = time.time()
                logging.info(
                    f"⏸️ Trading suspendu : ADX={macro_data['adx']:.1f} > {ADX_TREND_LIMIT} "
                    f"(tendance trop forte) | Prix={price:.{PRICE_DECIMALS}f} {QUOTE_ASSET}"
                )
            time.sleep(LOOP_SLEEP)
            continue

        # ── TRAITEMENT BUY CLOISONNÉ ──
        while buy_grid and price <= buy_grid[0]:
            # FIX 2 — Gv recalculé avant chaque ordre dans la boucle multi-niveaux
            capital_effectif = (quote_bal + base_bal * price) * ACTIVE_CAPITAL_RATIO
            Gv = compute_gv(capital_effectif, state["P0"], state["Gll"], state["nu"], state["nl"], state["Gsl"])

            if quote_bal >= Gv:
                touched          = buy_grid.pop(0)
                actual_buy_price = smart_execute_order(Client.SIDE_BUY, Gv, price, state, stress)

                if actual_buy_price:
                    qty_bought = Gv / actual_buy_price
                    new_sell_raw = actual_buy_price * state["Gsl"]
                    # Garde-fou zone frontière (page 14) : si new_sell dépasse P0, on bascule en dynamique zone haute
                    if new_sell_raw > state["P0"]:
                        new_sell_raw = actual_buy_price * state["Gsu"]
                    new_sell = round(new_sell_raw, PRICE_DECIMALS)
                    sell_grid.append(new_sell)
                    sell_grid.sort()
                    state["sell_grid"] = sell_grid
                    state["buy_grid"]  = buy_grid

                    # FIX 7 — enregistrement du prix d'achat pour calcul P&L futur
                    state["buy_prices"][str(new_sell)] = actual_buy_price

                    save_state(state)
                    logging.info(f"⚡ [MULTI-CROSS] Entrée synchronisée -> Posé Vente à {new_sell:.4f}$")
                    quote_bal, base_bal, _ = get_balances(price)
                else:
                    buy_grid.insert(0, touched)
                    break
            else:
                break

        # ── TRAITEMENT SELL CLOISONNÉ ──
        while sell_grid and price >= sell_grid[0]:
            # FIX 2 — Gv recalculé avant chaque ordre dans la boucle multi-niveaux
            capital_effectif = (quote_bal + base_bal * price) * ACTIVE_CAPITAL_RATIO
            Gv = compute_gv(capital_effectif, state["P0"], state["Gll"], state["nu"], state["nl"], state["Gsl"])

            if base_bal >= (Gv / price):
                touched           = sell_grid.pop(0)
                actual_sell_price = smart_execute_order(Client.SIDE_SELL, Gv, price, state, stress)

                if actual_sell_price:
                    qty_sold = Gv / actual_sell_price

                    new_buy_raw = actual_sell_price / state["Gsu"]
                    # Garde-fou zone frontière (page 14) : si new_buy passe sous P0, on bascule en dynamique zone basse
                    if new_buy_raw < state["P0"]:
                        new_buy_raw = actual_sell_price / state["Gsl"]
                    new_buy = round(new_buy_raw, PRICE_DECIMALS)  # Eq.13 zone haute : descend avec Gsu depuis le prix de vente
                    buy_grid.insert(0, new_buy)
                    buy_grid.sort(reverse=True)
                    state["sell_grid"] = sell_grid
                    state["buy_grid"]  = buy_grid

                    # FIX 7 — calcul et log du P&L réalisé sur ce trade
                    sell_key    = str(touched)
                    buy_price   = state["buy_prices"].pop(sell_key, None)
                    if buy_price is not None:
                        pnl = (actual_sell_price - buy_price) * qty_sold
                        state["total_pnl"] = state.get("total_pnl", 0.0) + pnl
                        logging.info(
                            f"💰 PnL trade : {'+' if pnl >= 0 else ''}{pnl:.4f} {QUOTE_ASSET} "
                            f"(achat={buy_price:.4f} → vente={actual_sell_price:.4f}) | "
                            f"PnL cumulé={state['total_pnl']:.4f}"
                        )

                    save_state(state)
                    logging.info(f"⚡ [MULTI-CROSS] Sortie synchronisée -> Posé Rachat à {new_buy:.4f}$")
                    quote_bal, base_bal, _ = get_balances(price)
                else:
                    sell_grid.insert(0, touched)
                    break
            else:
                if check_and_recycle_buyers(state, price, macro_data["atr"]):  # FIX 5 — passage ATR
                    buy_grid, sell_grid = state["buy_grid"], state["sell_grid"]
                    quote_bal, base_bal, total_wallet = get_balances(price)
                    continue
                break

        # ── AFFICHAGE ET SAUVEGARDE PÉRIODIQUE (Toutes les 10 secondes réelles) ──
        if time.time() - last_log_time >= 10.0:
            last_log_time = time.time()
            stress = compute_stress(
                macro_data["adx"], macro_data["atr"], price, drawdown,
                state["total_slippage"] / max(1, state["total_trades"]),
                macro_data.get("atr_norm", 0.01)
            )
            save_state(state)
            logging.info(
                f"📊 {price:.4f} | BudgetIsolé={total_wallet:.2f} {QUOTE_ASSET} | Stress={stress:.2f} | "
                f"BUY={len(buy_grid)} SELL={len(sell_grid)} | Gv={Gv:.2f}$ | "
                f"Trades={state['total_trades']} | PnL={state.get('total_pnl', 0.0):.4f}"
            )

        time.sleep(LOOP_SLEEP)

    except Exception as e:
        logging.error(f"❌ Erreur critique boucle principale : {e}")
        time.sleep(5)
