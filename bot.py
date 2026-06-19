import os
import sys
import time
import json
import math
import glob
import signal
import logging
import logging.handlers
import argparse
import threading
import bisect
import pandas as pd
import numpy as np
import ta
from binance.client import Client
from binance.exceptions import BinanceAPIException

try:
    import websocket
    _WS_AVAILABLE = True
except ImportError:
    websocket = None
    _WS_AVAILABLE = False
    print("⚠️  'websocket-client' n'est pas installé. Installez-le avec : pip install websocket-client")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  Attention: 'python-dotenv' n'est pas installé.")

API_KEY    = os.getenv("BINANCE_API_KEY", "...")
API_SECRET = os.getenv("BINANCE_API_SECRET", "...")

# ── PARSING ─────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grid Trading Bot — Moteur Quantitatif V100 (sans auto-réparation wallet_peak)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "symbol",
        nargs="?",
        default="INJUSDC",
        type=str,
        help="Paire de trading (ex: INJUSDC, EGLDUSDT)",
    )
    parser.add_argument("_p_budget",          nargs="?", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("_p_density_atr_low",  nargs="?", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("_p_density_atr_high", nargs="?", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("_p_density_k_min",    nargs="?", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("_p_density_k_max",    nargs="?", type=float, default=None, help=argparse.SUPPRESS)

    parser.add_argument("--budget", dest="n_budget", type=float, default=None, metavar="USDC")
    parser.add_argument("--density-atr-low",  dest="n_density_atr_low",  type=float, default=None)
    parser.add_argument("--density-atr-high", dest="n_density_atr_high", type=float, default=None)
    parser.add_argument("--density-k-min",    dest="n_density_k_min",    type=float, default=None)
    parser.add_argument("--density-k-max",    dest="n_density_k_max",    type=float, default=None)
    parser.add_argument("--bots", type=int, default=None,
                        help="Nombre de bots se partageant le capital USDC (détection auto si omis)")
    parser.add_argument("--reconcile", action="store_true",
                        help="Réconcilier l'inventaire UNIQUEMENT au démarrage (manuel)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Niveau de log (défaut: INFO)")

    raw = parser.parse_args()
    raw.symbol = raw.symbol.upper()

    def _resolve(named, positional, default, label):
        if named is not None and positional is not None:
            parser.error(f"Conflit pour '{label}' : fourni à la fois en positionnel et en nommé.")
        return named if named is not None else (positional if positional is not None else default)

    ns = argparse.Namespace()
    ns.symbol           = raw.symbol
    ns.max_budget_usdc  = _resolve(raw.n_budget,           raw._p_budget,           None,   "--budget")
    ns.density_atr_low  = _resolve(raw.n_density_atr_low,  raw._p_density_atr_low,  0.0060, "--density-atr-low")
    ns.density_atr_high = _resolve(raw.n_density_atr_high, raw._p_density_atr_high, 0.0124, "--density-atr-high")
    ns.density_k_min    = _resolve(raw.n_density_k_min,    raw._p_density_k_min,    0.50,   "--density-k-min")
    ns.density_k_max    = _resolve(raw.n_density_k_max,    raw._p_density_k_max,    1.00,   "--density-k-max")
    ns.bots             = raw.bots
    ns.reconcile        = raw.reconcile
    ns.log_level        = raw.log_level

    if ns.density_atr_low >= ns.density_atr_high:
        parser.error("density-atr-low doit être strictement inférieur à density-atr-high.")
    if ns.density_k_min >= ns.density_k_max:
        parser.error("density-k-min doit être strictement inférieur à density-k-max.")
    return ns

args = parse_args()

SYMBOL           = args.symbol
MAX_BUDGET_USDC  = args.max_budget_usdc
DENSITY_ATR_LOW  = args.density_atr_low
DENSITY_ATR_HIGH = args.density_atr_high
DENSITY_K_MIN    = args.density_k_min
DENSITY_K_MAX    = args.density_k_max
NB_BOTS          = args.bots
AUTO_RECONCILE   = args.reconcile
LOG_LEVEL        = getattr(logging, args.log_level.upper())

# ── Résolution de la paire ────────────────────────────────────
if SYMBOL.endswith("USDC"):
    BASE_ASSET = SYMBOL.removesuffix("USDC") if hasattr(str, "removesuffix") else SYMBOL[:-4]
    QUOTE_ASSET = "USDC"
elif SYMBOL.endswith("USDT"):
    BASE_ASSET = SYMBOL.removesuffix("USDT") if hasattr(str, "removesuffix") else SYMBOL[:-4]
    QUOTE_ASSET = "USDT"
else:
    print(f"❌ Paire {SYMBOL} non supportée.")
    sys.exit(1)

# ── Constantes ─────────────────────────────────────────────────
NU_LEVELS      = 5
NL_LEVELS      = 5
NU_MIN, NU_MAX = 2, 10
NL_MIN, NL_MAX = 2, 10

ACTIVE_CAPITAL_RATIO = 0.9
MAX_CELL_RATIO       = 0.8
GV_MULTIPLIER        = 1.0

ATR_BASE_MULT = 7.0

GUL_HARD_MIN_PCT = 0.020
GUL_HARD_MAX_PCT = 0.15
GLL_HARD_MIN_PCT = 0.020
GLL_HARD_MAX_PCT = 0.15

TRADING_FEE_RT     = 0.00075
SLIPPAGE_EMA_ALPHA = 0.15
EQ16_MIN_RATIO     = 2.0
EQ16_MAX_RETRIES   = 3

STRESS_LIMIT_FOR_MAKER = 0.40
LIMIT_TIMEOUT_SECONDS  = 15

MIN_ORDER_USDC  = 5.5
KLINE_INTERVAL  = Client.KLINE_INTERVAL_3MINUTE
KLINE_LIMIT     = 100
LOOP_SLEEP      = 0.2
INDICATORS_FREQ = 60
ADX_TREND_LIMIT = 50
STATE_FILE      = f"state_{SYMBOL.lower()}.json"
FAILED_COOLDOWN_INITIAL = 3
FAILED_COOLDOWN_MAX     = 60
RECALC_PERIOD_SECONDS = 720.0
GLOBAL_STOP_LOSS_DD = 0.25
GLOBAL_STOP_LOSS_PNL = -0.10
MIN_REBUILD_DELAY = 300
MAX_GRID_AGE      = 86400
REBUILD_RATIO     = 0.97

# Seuil d'alerte drawdown (sans modification du peak)
DRAWDOWN_WARNING_THRESHOLD = 0.30

LOCK_TIMEOUT = 120
WS_PRICE_MAX_AGE  = 20.0
WS_CHECK_INTERVAL = 60
WS_FORCE_RESTART_AGE = 60.0

FORCE_INIT_TIMEOUT = 600

PRICE_DECIMALS = 4
QTY_DECIMALS   = 2
MIN_NOTIONAL = 0.0
MIN_QTY      = 0.0

# ── Rate limit ─────────────────────────────────────────────────
_last_get_order_time = 0.0
MIN_GET_ORDER_INTERVAL = 1.0

# ── Caches ─────────────────────────────────────────────────────
_balance_cache = {"quote": 0.0, "base": 0.0, "timestamp": 0.0}
_BALANCE_CACHE_TTL = 5.0

ws_price      = None
ws_price_time = 0.0
ws_price_lock = threading.Lock()
ws_running    = False
ws_thread     = None
ws_app        = None

ws_retry_count  = 0
_ws_retry_lock  = threading.Lock()
WS_BACKOFF_BASE = 1.0
WS_BACKOFF_MAX  = 60.0

failed_consecutive = 0
_capital_initial = None

# ── Logging ───────────────────────────────────────────────────
log_file = f"bot_{SYMBOL.lower()}.log"
logging.getLogger().handlers.clear()
handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10_485_760,
    backupCount=5
)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

logging.basicConfig(
    level=LOG_LEVEL,
    handlers=[handler, console]
)

logger = logging.getLogger(__name__)

# ── Classe SortedGrid ─────────────────────────────────────────
class SortedGrid:
    def __init__(self, reverse=False, initial=None):
        self._reverse = reverse
        self._list = []
        self._set = set()
        if initial:
            for p in initial:
                self.add(p)

    def __len__(self):
        return len(self._list)

    def __getitem__(self, index):
        return self._list[index]

    def __iter__(self):
        return iter(self._list)

    def __contains__(self, price):
        return price in self._set

    def add(self, price):
        if price not in self._set:
            if self._reverse:
                self._list.append(price)
                self._list.sort(reverse=True)
            else:
                bisect.insort(self._list, price)
            self._set.add(price)

    def pop(self, index=0):
        price = self._list.pop(index)
        self._set.discard(price)
        return price

    def remove(self, price):
        if price in self._set:
            self._list.remove(price)
            self._set.discard(price)

    def clear(self):
        self._list.clear()
        self._set.clear()

    def extend(self, prices):
        for p in prices:
            self.add(p)

    def to_list(self):
        return self._list

    @property
    def list(self):
        return self._list

    @property
    def set(self):
        return self._set

# ── Locks ──────────────────────────────────────────────────────
def get_lock_file(symbol: str) -> str:
    return f"lock_{symbol.lower()}.pid"

def _write_lock_file(symbol: str, action: str = "écrire") -> bool:
    lock_path = get_lock_file(symbol)
    try:
        with open(lock_path, "w") as f:
            f.write(f"{os.getpid()}:{int(time.time())}")
        return True
    except Exception as e:
        logger.error(f"❌ Impossible de {action} le lock {lock_path} : {e}")
        return False

def create_lock(symbol: str) -> bool:
    return _write_lock_file(symbol, "créer")

def update_lock(symbol: str) -> bool:
    return _write_lock_file(symbol, "mettre à jour")

def remove_lock(symbol: str):
    lock_path = get_lock_file(symbol)
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception as e:
        logger.error(f"❌ Erreur suppression lock {lock_path} : {e}")

def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def detect_nb_bots() -> int:
    lock_files = glob.glob("lock_*.pid")
    active_bots = 0
    current_pid = os.getpid()
    current_time = int(time.time())

    for lock_path in lock_files:
        try:
            with open(lock_path, "r") as f:
                content = f.read().strip()
            parts = content.split(":")
            if len(parts) != 2:
                logger.warning(f"⚠️ Lock invalide (format): {lock_path}")
                continue
            pid = int(parts[0])
            timestamp = int(parts[1])

            if pid == current_pid:
                active_bots += 1
                continue

            if is_process_alive(pid):
                if current_time - timestamp > LOCK_TIMEOUT:
                    logger.warning(f"⚠️ Lock {lock_path} : PID {pid} toujours actif mais timestamp trop vieux ({(current_time - timestamp)}s) -> nettoyage")
                    os.remove(lock_path)
                else:
                    active_bots += 1
            else:
                logger.info(f"🧹 Lock obsolète supprimé: {lock_path} (PID {pid} mort)")
                os.remove(lock_path)
        except Exception as e:
            logger.error(f"❌ Erreur lecture lock {lock_path} : {e}")
            try:
                os.remove(lock_path)
            except:
                pass

    return max(1, active_bots)

if NB_BOTS is None:
    if not create_lock(SYMBOL):
        logger.error("❌ Échec de création du lock, arrêt.")
        sys.exit(1)
    NB_BOTS = detect_nb_bots()
    logger.info(f"🔍 Détection automatique : {NB_BOTS} bot(s) actifs (lock_*.pid)")
else:
    create_lock(SYMBOL)
    logger.info(f"🤖 Partage forcé : {NB_BOTS} bot(s) (argument --bots)")

logger.info(f"⚙️ Density params : ATR_LOW={DENSITY_ATR_LOW}  ATR_HIGH={DENSITY_ATR_HIGH}  K_MIN={DENSITY_K_MIN}  K_MAX={DENSITY_K_MAX}")
if MAX_BUDGET_USDC:
    logger.info(f"💰 Budget max USDC (limite soft) : {MAX_BUDGET_USDC} (ne réduit pas le stock crypto)")

client = Client(API_KEY, API_SECRET)
if hasattr(client, "session"):
    client.session.request_timeout = 15

_shutdown_requested = False
def _handle_signal(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ═══════════════════════════════════════════════════════════════
# FONCTIONS DE BASE
# ═══════════════════════════════════════════════════════════════

def get_instant_price() -> float | None:
    try:
        ticker = client.get_symbol_ticker(symbol=SYMBOL)
        return float(ticker["price"])
    except Exception as e:
        logger.error(f"❌ Erreur prix {SYMBOL} : {e}")
        return None

def start_price_websocket(symbol: str):
    global ws_price, ws_price_time, ws_running, ws_thread, ws_app, ws_retry_count
    if not _WS_AVAILABLE:
        logger.warning("⚠️ websocket-client non installé — mode REST uniquement")
        return
    if ws_running:
        with _ws_retry_lock:
            ws_retry_count = 0
        return

    delay = min(WS_BACKOFF_BASE * (2 ** ws_retry_count), WS_BACKOFF_MAX)
    if ws_retry_count > 0:
        logger.info(f"⏳ Backoff WebSocket : attente {delay:.1f}s avant tentative {ws_retry_count+1}")
        time.sleep(delay)

    ws_url = f"wss://stream.binance.com/ws/{symbol.lower()}@trade"
    logger.info(f"🔌 Connexion à {ws_url} (tentative {ws_retry_count+1})")

    def on_message(ws, message):
        global ws_price, ws_price_time
        try:
            data = json.loads(message)
            price = float(data.get('p', 0))
            if price > 0:
                with ws_price_lock:
                    ws_price = price
                    ws_price_time = time.time()
        except Exception as e:
            logger.error(f"Erreur callback WS : {e}")

    def on_error(ws, error):
        logger.error(f"Erreur WebSocket : {error}")

    def on_close(ws, close_status_code, close_msg):
        global ws_running
        ws_running = False
        logger.warning("WebSocket fermé")

    def on_open(ws):
        global ws_running, ws_retry_count
        ws_running = True
        with _ws_retry_lock:
            ws_retry_count = 0
        logger.info(f"🌐 WebSocket connecté pour {symbol} (stream @trade)")

    ws_app = websocket.WebSocketApp(ws_url,
                                    on_open=on_open,
                                    on_message=on_message,
                                    on_error=on_error,
                                    on_close=on_close)
    def run_ws():
        ws_app.run_forever()
    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()

def stop_price_websocket():
    global ws_running, ws_app
    if ws_running and ws_app:
        try:
            ws_app.close()
        except Exception as e:
            logger.error(f"Erreur fermeture WS : {e}")
    ws_running = False
    logger.info("🛑 WebSocket arrêté")

def get_ws_price() -> float | None:
    with ws_price_lock:
        p = ws_price
        age = time.time() - ws_price_time
    if p is None or age > WS_PRICE_MAX_AGE:
        if p is not None and age > WS_PRICE_MAX_AGE:
            if not hasattr(get_ws_price, "_last_warn_time") or time.time() - get_ws_price._last_warn_time > 60:
                get_ws_price._last_warn_time = time.time()
                logger.warning(f"⚠️ Prix WS périmé ({age:.1f}s > {WS_PRICE_MAX_AGE}s) — fallback REST")
        return get_instant_price()
    return p

def invalidate_balance_cache():
    global _balance_cache
    _balance_cache["timestamp"] = 0.0

def get_real_balances() -> tuple[float, float]:
    global _balance_cache
    now = time.time()
    if now - _balance_cache["timestamp"] < _BALANCE_CACHE_TTL:
        return _balance_cache["quote"], _balance_cache["base"]

    try:
        acc = client.get_account()
        bals = {b["asset"]: float(b["free"]) for b in acc["balances"]}
        quote = bals.get(QUOTE_ASSET, 0.0)
        base = bals.get(BASE_ASSET, 0.0)
        _balance_cache["quote"] = quote
        _balance_cache["base"] = base
        _balance_cache["timestamp"] = now
        return quote, base
    except Exception as e:
        logger.error(f"❌ Erreur récupération soldes réels : {e}")
        return _balance_cache["quote"], _balance_cache["base"]

def get_balances(price: float) -> tuple[float, float, float, float]:
    global _capital_initial
    quote_bal_real, base_bal_real = get_real_balances()
    quote_bal_virt = quote_bal_real / NB_BOTS
    base_bal_virt  = base_bal_real

    total_wallet = quote_bal_virt + base_bal_real * price
    base_for_grid = base_bal_real
    capital_for_grid = quote_bal_virt + base_for_grid * price

    if MAX_BUDGET_USDC is not None:
        if capital_for_grid > MAX_BUDGET_USDC:
            ratio = MAX_BUDGET_USDC / capital_for_grid
            quote_bal_virt = quote_bal_virt * ratio
            capital_for_grid = MAX_BUDGET_USDC
            total_wallet = quote_bal_virt + base_bal_real * price

    if _capital_initial is None:
        _capital_initial = total_wallet
        logger.info(f"💰 Capital initial : {_capital_initial:.2f} {QUOTE_ASSET}")

    return quote_bal_virt, base_bal_virt, total_wallet, capital_for_grid

def adjust_levels_to_balance(quote_bal: float, base_bal_in_quote: float) -> tuple[int, int]:
    total = quote_bal + base_bal_in_quote
    if total == 0:
        return NU_LEVELS, NL_LEVELS
    crypto_ratio = base_bal_in_quote / total
    total_budget = NU_LEVELS + NL_LEVELS
    nu_raw = round(total_budget * crypto_ratio)
    nu_adjusted = max(NU_MIN, min(NU_MAX, nu_raw))
    nl_raw = total_budget - nu_adjusted
    nl_adjusted = max(NL_MIN, min(NL_MAX, nl_raw))
    if nl_adjusted != nl_raw:
        nu_adjusted = max(NU_MIN, min(NU_MAX, total_budget - nl_adjusted))
    logger.info(f"⚖️ Répartition virtuelle : {BASE_ASSET}={crypto_ratio*100:.1f}% | {QUOTE_ASSET}={(1-crypto_ratio)*100:.1f}%")
    logger.info(f"⚖️ Cibles : BUY={nl_adjusted} | SELL={nu_adjusted}")
    return nu_adjusted, nl_adjusted

def get_symbol_precisions():
    global PRICE_DECIMALS, QTY_DECIMALS, MIN_NOTIONAL, MIN_QTY
    try:
        info = client.get_symbol_info(SYMBOL)
        if info is None:
            raise ValueError(f"Symbole {SYMBOL} introuvable")
        found_price = False
        found_qty = False
        for f in info["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
                if tick_size > 0:
                    PRICE_DECIMALS = int(round(-math.log10(tick_size)))
                    found_price = True
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                min_qty_raw = float(f.get("minQty", 0.0))
                if step_size > 0:
                    QTY_DECIMALS = int(round(-math.log10(step_size)))
                    found_qty = True
                if min_qty_raw > 0:
                    MIN_QTY = min_qty_raw
            if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                val = float(f.get("minNotional", 0.0))
                if val > 0:
                    MIN_NOTIONAL = val
        if not found_price:
            raise ValueError("Filtre PRICE_FILTER absent ou tickSize invalide")
        if not found_qty:
            raise ValueError("Filtre LOT_SIZE absent ou stepSize invalide")
        logger.info(f"⚙️ Précisions -> Prix: {PRICE_DECIMALS} déc. | Qté: {QTY_DECIMALS} déc. | MIN_QTY: {MIN_QTY} | MIN_NOTIONAL: {MIN_NOTIONAL}")
    except Exception as e:
        logger.error(f"❌ Précisions non chargées : {e} — arrêt")
        sys.exit(1)

def migrate_old_state(state: dict) -> dict:
    migrated = False
    if "open_positions" in state and state["open_positions"]:
        old_positions = state["open_positions"]
        new_lots = []
        for pos in old_positions:
            if isinstance(pos, dict) and "qty" in pos and "buy_price" in pos:
                new_lots.append({
                    "qty": pos["qty"],
                    "buy_price": pos["buy_price"],
                    "timestamp": pos.get("timestamp", time.time())
                })
        if new_lots:
            state["inventory_lots"] = new_lots
            state["total_base_qty"] = sum(lot["qty"] for lot in new_lots)
            logger.info(f"🔄 Migration {len(new_lots)} lots depuis open_positions")
            migrated = True
        del state["open_positions"]

    for old_key in ["buy_prices", "avg_buy_price"]:
        if old_key in state:
            del state[old_key]
            migrated = True

    if "inventory_lots" not in state:
        state["inventory_lots"] = []
        state["total_base_qty"] = 0.0
        migrated = True

    computed_qty = sum(lot.get("qty", 0.0) for lot in state["inventory_lots"])
    if abs(computed_qty - state.get("total_base_qty", 0.0)) > 1e-8:
        logger.warning(f"⚠️ Correction incohérence stock: {state['total_base_qty']} -> {computed_qty}")
        state["total_base_qty"] = computed_qty
        migrated = True

    if migrated:
        logger.info("✅ Migration d'état effectuée")
    return state

# ═══════════════════════════════════════════════════════════════
# load_state avec conversion SortedGrid
# ═══════════════════════════════════════════════════════════════

def load_state() -> dict:
    defaults = {
        "grid_ready": False,
        "P0": None, "Gul": None, "Gll": None, "Gsu": None, "Gsl": None, "Gv": None,
        "sell_grid": [], "buy_grid": [],
        "nu": NU_LEVELS, "nl": NL_LEVELS,
        "wallet_peak": 0.0, "total_trades": 0, "failed_count": 0,
        "total_slippage": 0.0, "cycle_recalc": 0,
        "ema_slippage_buy": 0.0, "ema_slippage_sell": 0.0,
        "capital_usdc": 0.0,
        "total_pnl": 0.0,
        "density_k": 0.65,
        "last_rebuild_price": 0.0,
        "inventory_lots": [],
        "total_base_qty": 0.0,
        "last_grid_rebuild_ts": time.time(),
        "last_grid_init_attempt": 0.0,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = {**defaults, **json.load(f)}
            state = migrate_old_state(state)
            if "last_grid_rebuild_ts" not in state:
                state["last_grid_rebuild_ts"] = time.time()
            if "last_grid_init_attempt" not in state:
                state["last_grid_init_attempt"] = 0.0
            if state.get("grid_ready", False):
                if isinstance(state.get("sell_grid"), list):
                    state["sell_grid"] = SortedGrid(reverse=False, initial=state["sell_grid"])
                if isinstance(state.get("buy_grid"), list):
                    state["buy_grid"] = SortedGrid(reverse=True, initial=state["buy_grid"])
            return state
        except Exception as e:
            logger.warning(f"⚠️ Impossible de lire {STATE_FILE} : {e}")
    defaults["sell_grid"] = SortedGrid(reverse=False)
    defaults["buy_grid"] = SortedGrid(reverse=True)
    return defaults

def save_state(state: dict):
    state_copy = state.copy()
    if isinstance(state_copy.get("sell_grid"), SortedGrid):
        state_copy["sell_grid"] = state_copy["sell_grid"].to_list()
    if isinstance(state_copy.get("buy_grid"), SortedGrid):
        state_copy["buy_grid"] = state_copy["buy_grid"].to_list()
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state_copy, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"❌ Erreur sauvegarde : {e}")

def reconcile_inventory(state: dict, price: float):
    invalidate_balance_cache()
    _, real_base_bal = get_real_balances()
    local_qty = state.get("total_base_qty", 0.0)
    diff = real_base_bal - local_qty
    if abs(diff) < 1e-8:
        logger.info("✅ Inventaire cohérent (écart < 1e-8)")
        return

    logger.warning(f"⚠️ Écart d'inventaire détecté : local={local_qty:.6f}, Binance={real_base_bal:.6f}, diff={diff:+.6f}")
    if diff > 0:
        buy_price = state.get("P0", price)
        if buy_price is None:
            buy_price = price
            logger.warning("⚠️ P0 non disponible, prix marché utilisé pour lot réconcilié")
        state["inventory_lots"].append({
            "qty": diff,
            "buy_price": buy_price,
            "timestamp": time.time(),
            "reconciled": True
        })
        logger.info(f"➕ Ajout d'un lot réconcilié de {diff:.6f} à {buy_price:.4f}")
    else:
        to_remove = -diff
        new_lots = []
        for lot in state["inventory_lots"]:
            if to_remove <= 0:
                new_lots.append(lot)
                continue
            if lot["qty"] <= to_remove:
                to_remove -= lot["qty"]
                logger.info(f"🗑️ Suppression lot de {lot['qty']:.6f} à {lot['buy_price']:.4f} (réconciliation)")
            else:
                lot["qty"] -= to_remove
                logger.info(f"✂️ Réduction lot de {to_remove:.6f} à {lot['buy_price']:.4f} (réconciliation)")
                new_lots.append(lot)
                to_remove = 0
        state["inventory_lots"] = new_lots
    state["total_base_qty"] = sum(lot["qty"] for lot in state["inventory_lots"])
    logger.info(f"✅ Inventaire réconcilié : nouveau total={state['total_base_qty']:.6f}")
    save_state(state)

def remove_from_inventory_fifo(state: dict, qty: float):
    remaining = qty
    new_lots = []
    for lot in state.get("inventory_lots", []):
        if remaining <= 0:
            new_lots.append(lot)
            continue
        if lot["qty"] <= remaining:
            remaining -= lot["qty"]
        else:
            lot["qty"] -= remaining
            new_lots.append(lot)
            remaining = 0
    state["inventory_lots"] = new_lots
    state["total_base_qty"] = sum(lot["qty"] for lot in state["inventory_lots"])
    logger.info(f"📉 Retrait FIFO de {qty:.6f} tokens, nouveau stock = {state['total_base_qty']:.6f}")

def reconcile_open_orders(state: dict):
    try:
        open_orders = client.get_open_orders(symbol=SYMBOL)
        if not open_orders:
            logger.info("✅ Aucun ordre ouvert trouvé sur Binance.")
            return

        logger.info(f"🔍 {len(open_orders)} ordre(s) ouvert(s) détecté(s) sur Binance.")
        for order in open_orders:
            order_id = order["orderId"]
            side = order["side"]
            orig_qty = float(order["origQty"])
            executed_qty = float(order.get("executedQty", 0.0))
            price = float(order["price"])
            status = order["status"]
            if status in ("NEW", "PARTIALLY_FILLED"):
                if executed_qty > 0:
                    if side == "BUY":
                        logger.warning(f"⚠️ Ordre BUY {order_id} partiellement exécuté ({executed_qty} sur {orig_qty}) — ajout inventaire")
                        state["inventory_lots"].append({
                            "qty": executed_qty,
                            "buy_price": price,
                            "timestamp": time.time(),
                            "reconciled": True
                        })
                        state["total_base_qty"] = state.get("total_base_qty", 0.0) + executed_qty
                    else:
                        logger.warning(f"⚠️ Ordre SELL {order_id} partiellement exécuté ({executed_qty} sur {orig_qty}) — retrait inventaire")
                        remove_from_inventory_fifo(state, executed_qty)
                try:
                    client.cancel_order(symbol=SYMBOL, orderId=order_id)
                    logger.info(f"🗑️ Ordre {order_id} annulé.")
                except Exception as e:
                    logger.error(f"❌ Erreur annulation ordre {order_id} : {e}")
        save_state(state)
        logger.info("✅ Réconciliation des ordres ouverts terminée.")
    except Exception as e:
        logger.error(f"❌ Erreur réconciliation des ordres ouverts : {e}")

# ═══════════════════════════════════════════════════════════════
# INDICATEURS ET MATHÉMATIQUES
# ═══════════════════════════════════════════════════════════════

def get_heavy_indicators() -> dict | None:
    try:
        klines_3m = client.get_klines(symbol=SYMBOL, interval=KLINE_INTERVAL, limit=KLINE_LIMIT)
        df_3m = pd.DataFrame(klines_3m, columns=["time","open","high","low","close","volume","ct","qav","trades","tbb","tbq","i"])
        for col in ["open","high","low","close"]:
            df_3m[col] = df_3m[col].astype(float)
        atr_series_3m = ta.volatility.average_true_range(df_3m["high"], df_3m["low"], df_3m["close"], window=14)
        atr = float(atr_series_3m.iloc[-1])
        atr_norm = float(atr_series_3m.iloc[-1]) / float(df_3m["close"].iloc[-1]) if float(df_3m["close"].iloc[-1]) > 0 else 0.01
        dip = ta.trend.adx_pos(df_3m["high"], df_3m["low"], df_3m["close"], window=14).iloc[-1]
        dim = ta.trend.adx_neg(df_3m["high"], df_3m["low"], df_3m["close"], window=14).iloc[-1]

        klines_15m = client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_15MINUTE, limit=50)
        df_15m = pd.DataFrame(klines_15m, columns=["time","open","high","low","close","volume","ct","qav","trades","tbb","tbq","i"])
        for col in ["open","high","low","close"]:
            df_15m[col] = df_15m[col].astype(float)
        adx_15m = ta.trend.adx(df_15m["high"], df_15m["low"], df_15m["close"], window=14).iloc[-1]
        atr_series_15m = ta.volatility.average_true_range(df_15m["high"], df_15m["low"], df_15m["close"], window=14)
        atr_norm_15m = float(atr_series_15m.iloc[-1]) / float(df_15m["close"].iloc[-1]) if float(df_15m["close"].iloc[-1]) > 0 else 0.01
        return {
            "atr": atr,
            "adx": float(adx_15m),
            "dip": float(dip),
            "dim": float(dim),
            "atr_norm": atr_norm,
            "atr_norm_15m": atr_norm_15m,
            "close": float(df_3m["close"].iloc[-1])
        }
    except Exception as e:
        logger.error(f"❌ Erreur indicateurs : {e}")
        return None

def compute_asymmetry(dip, dim, target_nu, target_nl):
    ratio = dip / max(dim, 0.001)
    if ratio >= 1.2:
        strength = min((ratio - 1.2) / 1.8, 1.0)
        return (
            1.0 + 0.10 * strength,
            1.0 - 0.05 * strength,
            target_nu,
            target_nl,
            f"BULLISH (ratio={ratio:.2f})"
        )
    elif ratio <= 0.8:
        strength = min((0.8 - ratio) / 0.8, 1.0)
        return (
            1.0 - 0.05 * strength,
            1.0 + 0.10 * strength,
            target_nu,
            target_nl,
            f"BEARISH (ratio={ratio:.2f})"
        )
    return 1.0, 1.0, target_nu, target_nl, f"NEUTRAL (ratio={ratio:.2f})"

def compute_stress(adx, atr_norm_current, atr_norm_ref, drawdown, slippage_avg):
    ms = min(1.0, (adx / 60.0) * 0.6)
    ref = max(atr_norm_ref, 1e-8)
    vol_ratio = min(2.0, atr_norm_current / ref)
    ms += min(1.0, vol_ratio * 0.4 * 0.33)
    ms = min(1.0, ms)
    ds = min(1.0, drawdown * 5.0)
    es = min(1.0, slippage_avg * 100.0)
    return ms * 0.50 + ds * 0.30 + es * 0.20

def compute_dynamic_bounds(P0, atr, stress, gul_bias, gll_bias):
    base = ATR_BASE_MULT * (atr / P0)
    stressed = base * (1.0 + stress)
    sup = max(GUL_HARD_MIN_PCT, min(stressed * gul_bias, GUL_HARD_MAX_PCT))
    inf = max(GLL_HARD_MIN_PCT, min(stressed / gll_bias, GLL_HARD_MAX_PCT))
    return (P0 * (1 + sup), P0 * (1 - inf))

def compute_gv(capital, P0, Gul, Gll, nu, nl, density_k):
    gsu = Gul / P0 if P0 > 0 else 1.0
    gsl = P0 / Gll if Gll > 0 else 1.0
    sell_denom = sum(P0 * math.pow(gsu, math.pow(i/nu, density_k)) for i in range(1, nu+1))
    buy_denom  = sum(P0 / math.pow(gsl, math.pow(i/nl, density_k)) for i in range(1, nl+1))
    denom = sell_denom + buy_denom
    gv_raw = (capital / denom) * GV_MULTIPLIER if denom > 0 else (capital / (nu+nl)) * GV_MULTIPLIER
    gv_usdc = gv_raw * P0
    return max(MIN_ORDER_USDC, min(gv_usdc, capital * MAX_CELL_RATIO))

def compute_density_k(atr_norm_15m):
    ratio = max(0.0, min(1.0, (atr_norm_15m - DENSITY_ATR_LOW) / (DENSITY_ATR_HIGH - DENSITY_ATR_LOW)))
    return DENSITY_K_MAX - (DENSITY_K_MAX - DENSITY_K_MIN) * ratio

def compute_min_gap(state):
    fee_buy = TRADING_FEE_RT + state.get("ema_slippage_buy", 0.0)
    fee_sell = TRADING_FEE_RT + state.get("ema_slippage_sell", 0.0)
    total_cost = fee_buy + fee_sell
    min_gap = total_cost * 1.5
    base_min = TRADING_FEE_RT * EQ16_MIN_RATIO
    return max(min_gap, base_min)

def enforce_eq16(P0, atr, Gul, Gll, nu, nl, stress, gub, glb, density_k, state):
    for attempt in range(EQ16_MAX_RETRIES+1):
        gsu_tmp = Gul / P0
        sell_exp_n = math.pow(nu/nu, density_k)
        sell_exp_nm1 = math.pow((nu-1)/nu, density_k)
        g_n_sell = P0 * math.pow(gsu_tmp, sell_exp_n)
        g_nm1_sell = P0 * math.pow(gsu_tmp, sell_exp_nm1)
        gap_sell = g_n_sell - g_nm1_sell
        min_gap = compute_min_gap(state)
        ok_sell = gap_sell > min_gap

        gsl_tmp = P0 / Gll if Gll > 0 else 1.01
        buy_exp_n = math.pow(nl/nl, density_k)
        buy_exp_nm1 = math.pow((nl-1)/nl, density_k)
        g_n_buy = P0 / math.pow(gsl_tmp, buy_exp_n)
        g_nm1_buy = P0 / math.pow(gsl_tmp, buy_exp_nm1)
        gap_buy = g_nm1_buy - g_n_buy
        ok_buy = gap_buy > min_gap

        if ok_sell and ok_buy:
            return Gul, Gll, nu, nl
        if attempt < EQ16_MAX_RETRIES:
            Gul = min(Gul * 1.03, P0 * (1 + GUL_HARD_MAX_PCT))
            Gll = max(Gll * 0.97, P0 * (1 - GLL_HARD_MAX_PCT))
    return Gul, Gll, nu, nl

# ═══════════════════════════════════════════════════════════════
# INITIALISATION DE LA GRILLE
# ═══════════════════════════════════════════════════════════════

def init_grid(price, atr, state, stress, dip, dim, adx, atr_norm_15m, force=False):
    if not force and adx > ADX_TREND_LIMIT:
        state["last_grid_init_attempt"] = time.time()
        save_state(state)
        logger.warning(f"⏸️ init_grid bloqué (ADX={adx:.1f} > {ADX_TREND_LIMIT})")
        return

    state["last_grid_init_attempt"] = time.time()
    save_state(state)

    P0 = price
    quote_bal, _, total_wallet, capital_for_grid = get_balances(P0)
    state["capital_usdc"] = total_wallet
    if total_wallet <= 0:
        logger.error("❌ Capital nul — init_grid annulée")
        return
    target_nu, target_nl = adjust_levels_to_balance(quote_bal, (capital_for_grid - quote_bal))
    gub, glb, nu, nl, regime = compute_asymmetry(dip, dim, target_nu, target_nl)
    Gul, Gll = compute_dynamic_bounds(P0, atr, stress, gub, glb)
    density_k = compute_density_k(atr_norm_15m)
    state["density_k"] = density_k

    Gul, Gll, nu, nl = enforce_eq16(P0, atr, Gul, Gll, nu, nl, stress, gub, glb, density_k, state)

    min_gap = compute_min_gap(state)
    gsu = max(Gul / P0, 1.0 + min_gap)
    gsl = max(P0 / Gll, 1.0 + min_gap) if Gll > 0 else 1.0 + min_gap

    sell_g = [round(P0 * math.pow(gsu, math.pow(i/nu, density_k)), PRICE_DECIMALS) for i in range(1, nu+1)]
    buy_g  = [round(P0 / math.pow(gsl, math.pow(i/nl, density_k)), PRICE_DECIMALS) for i in range(1, nl+1)]

    state["sell_grid"] = SortedGrid(reverse=False, initial=sell_g)
    state["buy_grid"] = SortedGrid(reverse=True, initial=buy_g)

    Gv = compute_gv(capital_for_grid * ACTIVE_CAPITAL_RATIO, P0, Gul, Gll, nu, nl, density_k)
    state["Gv"] = Gv

    state.update({
        "grid_ready": True,
        "P0": P0, "Gul": Gul, "Gll": Gll, "Gsu": gsu, "Gsl": gsl,
        "nu": nu, "nl": nl,
    })

    if state["wallet_peak"] == 0.0:
        state["wallet_peak"] = total_wallet
    save_state(state)

    logger.info(f"🧮 Grille Initialisée ({regime}) | k={density_k:.3f} | BUY={nl} SELL={nu}")
    logger.info(f"💰 Capital virtuel : {total_wallet:.2f} {QUOTE_ASSET}")
    logger.info(f"📐 Capital pour Gv : {capital_for_grid:.2f} {QUOTE_ASSET}")
    logger.info(f"📐 Gv = {Gv:.2f} {QUOTE_ASSET}")

# ═══════════════════════════════════════════════════════════════
# SMART ROUTER
# ═══════════════════════════════════════════════════════════════

def rate_limited_get_order(symbol: str, order_id: int):
    global _last_get_order_time
    now = time.time()
    elapsed = now - _last_get_order_time
    if elapsed < MIN_GET_ORDER_INTERVAL:
        time.sleep(MIN_GET_ORDER_INTERVAL - elapsed)
    try:
        result = client.get_order(symbol=symbol, orderId=order_id)
        _last_get_order_time = time.time()
        return result
    except BinanceAPIException as e:
        if e.code == -1003:
            logger.warning(f"⏳ Rate limit atteint, pause 5s...")
            time.sleep(5)
            return client.get_order(symbol=symbol, orderId=order_id)
        raise

def execute_market_fallback(side, qty_asset, target_price, state, operational_reason) -> tuple[float | None, float]:
    try:
        order = client.create_order(symbol=SYMBOL, side=side, type=Client.ORDER_TYPE_MARKET, quantity=qty_asset)
        if order.get("status") == "FILLED":
            fills = order.get("fills", [])
            total_qty = sum(float(f["qty"]) for f in fills)
            total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            actual_price = total_cost / total_qty if total_qty > 0 else target_price
            filled_qty = total_qty
            slippage = abs(actual_price - target_price) / target_price
            state["total_slippage"] += slippage
            if side == Client.SIDE_BUY:
                prev = state.get("ema_slippage_buy", 0.0)
                state["ema_slippage_buy"] = SLIPPAGE_EMA_ALPHA * slippage + (1 - SLIPPAGE_EMA_ALPHA) * prev
            else:
                prev = state.get("ema_slippage_sell", 0.0)
                state["ema_slippage_sell"] = SLIPPAGE_EMA_ALPHA * slippage + (1 - SLIPPAGE_EMA_ALPHA) * prev
            state["total_trades"] += 1
            state["failed_count"] = 0
            logger.info(f"💥 [{operational_reason}] MARKET @ {actual_price:.4f} | Slippage: {slippage:.4%} | Qté={filled_qty:.4f}")
            return actual_price, filled_qty
        else:
            logger.warning(f"⚠️ Ordre market non FILLED (status={order.get('status')})")
            state["failed_count"] += 1
            return None, 0.0
    except Exception as e:
        logger.error(f"❌ Fallback échoué : {e}")
        state["failed_count"] += 1
        return None, 0.0

def smart_execute_order(side, qty_usdc, target_price, state, current_stress, macro=None) -> tuple[float | None, float]:
    if macro is None:
        macro = {}
    if qty_usdc < MIN_ORDER_USDC:
        logger.warning(f"⚠️ Ordre ignoré : {qty_usdc:.2f} < MIN_ORDER_USDC")
        return None, 0.0

    try:
        qty_asset = round(qty_usdc / target_price, QTY_DECIMALS)
        if qty_asset <= 0:
            return None, 0.0

        if MIN_QTY > 0 and qty_asset < MIN_QTY:
            logger.warning(f"⚠️ Ordre ignoré : qty {qty_asset} < MIN_QTY {MIN_QTY}")
            return None, 0.0
        notional = qty_asset * target_price
        if MIN_NOTIONAL > 0 and notional < MIN_NOTIONAL:
            logger.warning(f"⚠️ Ordre ignoré : notional {notional:.4f} < MIN_NOTIONAL {MIN_NOTIONAL:.4f}")
            return None, 0.0

        if current_stress > STRESS_LIMIT_FOR_MAKER:
            return execute_market_fallback(side, qty_asset, target_price, state, "CAS 2 - DIRECT SWEEP")

        maker_price = target_price * (1 - 0.0001) if side == Client.SIDE_BUY else target_price * (1 + 0.0001)
        maker_price = round(maker_price, PRICE_DECIMALS)
        logger.info(f"🐢 [MAKER] Stress={current_stress:.2f} -> LIMIT @ {maker_price:.4f} qté={qty_asset:.4f}")

        order = client.create_order(symbol=SYMBOL, side=side, type=Client.ORDER_TYPE_LIMIT,
                                    timeInForce=Client.TIME_IN_FORCE_GTC,
                                    quantity=qty_asset, price=f"{maker_price:.{PRICE_DECIMALS}f}")
        order_id = order.get("orderId")

        adx_factor = 0.5 if macro.get("adx",0) > 30 else 1.0
        adaptive_timeout = LIMIT_TIMEOUT_SECONDS * (1.0 + (1.0 - current_stress)) * adx_factor
        MAX_DRIFT_PCT = 0.0015
        start_time = time.time()
        limit_filled_qty = 0.0
        limit_avg_price = 0.0

        while time.time() - start_time < adaptive_timeout:
            time.sleep(1.0)
            current_market_price = get_ws_price()
            if current_market_price:
                drift = abs(current_market_price - target_price) / target_price
                if drift > MAX_DRIFT_PCT:
                    logger.warning(f"⚠️ Dérive {drift:.4%} > {MAX_DRIFT_PCT:.4%} -> annulation")
                    break

            check = rate_limited_get_order(SYMBOL, order_id)
            status = check.get("status")
            if status == "FILLED":
                executed_qty = float(check.get("executedQty", 0.0))
                cum_quote = float(check.get("cummulativeQuoteQty", 0.0))
                avg_p = cum_quote / executed_qty if executed_qty > 0 else maker_price
                logger.info(f"✅ LIMIT exécuté @ {avg_p:.4f} qté={executed_qty:.4f}")
                state["total_trades"] += 1
                state["failed_count"] = 0
                return avg_p, executed_qty
            elif status in ["CANCELED", "REJECTED", "EXPIRED"]:
                return None, 0.0

        # Si on arrive ici : timeout ou drift → récupérer l'état final de l'ordre
        final_order = rate_limited_get_order(SYMBOL, order_id)
        final_status = final_order.get("status")
        executed_qty = float(final_order.get("executedQty", 0.0))
        cum_quote = float(final_order.get("cummulativeQuoteQty", 0.0))

        # Annuler l'ordre s'il est encore actif
        if final_status not in ["FILLED", "CANCELED", "REJECTED", "EXPIRED"]:
            try:
                client.cancel_order(symbol=SYMBOL, orderId=order_id)
            except Exception:
                pass
            final_order = rate_limited_get_order(SYMBOL, order_id)
            executed_qty = float(final_order.get("executedQty", 0.0))
            cum_quote = float(final_order.get("cummulativeQuoteQty", 0.0))

        if executed_qty > 0:
            limit_avg_price = cum_quote / executed_qty
            limit_filled_qty = executed_qty
            logger.info(f"📦 Exécution partielle limit : qté={limit_filled_qty:.4f} prix={limit_avg_price:.4f}")
            slippage = abs(limit_avg_price - target_price) / target_price
            if side == Client.SIDE_BUY:
                prev = state.get("ema_slippage_buy", 0.0)
                state["ema_slippage_buy"] = SLIPPAGE_EMA_ALPHA * slippage + (1 - SLIPPAGE_EMA_ALPHA) * prev
            else:
                prev = state.get("ema_slippage_sell", 0.0)
                state["ema_slippage_sell"] = SLIPPAGE_EMA_ALPHA * slippage + (1 - SLIPPAGE_EMA_ALPHA) * prev
            state["total_trades"] += 1
        else:
            logger.info("⚠️ Aucune exécution limitée, passage en market pour la totalité")

        remaining_qty = qty_asset - limit_filled_qty
        market_price = None
        market_filled_qty = 0.0
        if remaining_qty > 0:
            logger.info(f"🔄 Exécution market pour le solde : {remaining_qty:.4f}")
            market_price, market_filled_qty = execute_market_fallback(side, remaining_qty, target_price, state, "CAS 3 - MARKET FALLBACK (partial)")
            if market_price is None:
                if limit_filled_qty > 0:
                    logger.warning("⚠️ Le market a échoué, mais la partie limitée est conservée.")
                    return limit_avg_price, limit_filled_qty
                else:
                    return None, 0.0
        else:
            return limit_avg_price, limit_filled_qty

        total_filled_qty = limit_filled_qty + market_filled_qty
        if total_filled_qty > 0:
            total_cost = (limit_filled_qty * limit_avg_price) + (market_filled_qty * market_price)
            final_avg_price = total_cost / total_filled_qty
        else:
            final_avg_price = market_price if market_price is not None else target_price
            total_filled_qty = market_filled_qty

        logger.info(f"✅ Exécution mixte limit+market : prix moyen={final_avg_price:.4f} qté totale={total_filled_qty:.4f}")
        return final_avg_price, total_filled_qty

    except Exception as e:
        logger.error(f"❌ Erreur Smart Router : {e}")
        state["failed_count"] += 1
        return None, 0.0

# ═══════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE (sans auto-réparation wallet_peak)
# ═══════════════════════════════════════════════════════════════

logger.info(f"🚀 Démarrage Moteur Quantitatif V100 — Target: {SYMBOL}")

start_price_websocket(SYMBOL)
get_symbol_precisions()
state = load_state()

reconcile_open_orders(state)

macro_data = get_heavy_indicators()
while not macro_data:
    logger.warning("⚠️ Attente indicateurs...")
    time.sleep(10)
    macro_data = get_heavy_indicators()

price0 = get_ws_price()
if price0:
    _, _, total_wallet0, _ = get_balances(price0)
    if AUTO_RECONCILE:
        reconcile_inventory(state, price0)
    else:
        logger.info("ℹ️ Réconciliation manuelle désactivée. Utilisez --reconcile au démarrage si nécessaire.")
else:
    total_wallet0 = 0.0
    logger.warning("⚠️ Impossible de réconcilier l'inventaire au démarrage (prix manquant)")

last_macro_time       = time.time()
last_log_time         = time.time()
last_lock_update      = time.time()
last_ws_check         = time.time()
last_periodic_recalc  = time.time()
stress                = 0.20
with _ws_retry_lock:
    ws_retry_count = 0

failed_consecutive = 0
last_exposure_state = None

while not _shutdown_requested:
    try:
        if failed_consecutive > 0:
            backoff = min(
                FAILED_COOLDOWN_INITIAL * (2 ** (failed_consecutive - 1)),
                FAILED_COOLDOWN_MAX
            )
            logger.warning(
                f"⏳ Circuit breaker : pause de {backoff}s "
                f"après {failed_consecutive} échecs consécutifs"
            )
            time.sleep(backoff)

        if time.time() - last_ws_check >= WS_CHECK_INTERVAL:
            last_ws_check = time.time()
            with ws_price_lock:
                age = time.time() - ws_price_time
            if ws_running and ws_price is not None and age > WS_FORCE_RESTART_AGE:
                logger.warning(f"⚠️ Prix WS périmé depuis {age:.1f}s > {WS_FORCE_RESTART_AGE}s — redémarrage WebSocket forcé")
                stop_price_websocket()
                with _ws_retry_lock:
                    ws_retry_count += 1
                start_price_websocket(SYMBOL)
            elif not ws_running:
                logger.warning("⚠️ WebSocket inactif, tentative de redémarrage...")
                with _ws_retry_lock:
                    ws_retry_count += 1
                start_price_websocket(SYMBOL)

        if time.time() - last_lock_update >= 30:
            update_lock(SYMBOL)
            last_lock_update = time.time()

        price = get_ws_price()
        if not price:
            failed_consecutive += 1
            time.sleep(LOOP_SLEEP)
            continue

        if time.time() - last_macro_time >= INDICATORS_FREQ:
            last_macro_time = time.time()
            fresh = get_heavy_indicators()
            if fresh:
                macro_data = fresh

        failed_consecutive = 0

        quote_bal, base_bal, total_wallet, capital_for_grid = get_balances(price)

        if total_wallet <= 0:
            failed_consecutive += 1
            time.sleep(LOOP_SLEEP)
            continue

        # Mise à jour du wallet_peak (global high-water mark)
        if state["wallet_peak"] == 0.0 or total_wallet > state["wallet_peak"]:
            state["wallet_peak"] = total_wallet

        drawdown_dd = max(0.0, 1.0 - total_wallet / state["wallet_peak"])

        # Alerte si drawdown dépasse le seuil (sans modification du peak)
        if drawdown_dd > DRAWDOWN_WARNING_THRESHOLD:
            logger.warning(
                f"⚠️ Drawdown important : {drawdown_dd:.1%} (peak={state['wallet_peak']:.2f}, capital={total_wallet:.2f})"
            )

        inventory_qty = state.get("total_base_qty", 0.0)
        unrealized_pnl = 0.0
        if inventory_qty > 0:
            total_cost = sum(lot["qty"] * lot["buy_price"] for lot in state.get("inventory_lots", []))
            avg_cost = total_cost / inventory_qty
            unrealized_pnl = (price - avg_cost) * inventory_qty
        total_pnl = state.get("total_pnl", 0.0) + unrealized_pnl
        pnl_pct = total_pnl / _capital_initial if _capital_initial and _capital_initial > 0 else 0.0

        # Stop-loss basé sur le drawdown (wallet_peak) et le PnL total
        if drawdown_dd >= GLOBAL_STOP_LOSS_DD:
            logger.critical(f"🚨 STOP-LOSS (drawdown) : DD={drawdown_dd*100:.2f}% ≥ {GLOBAL_STOP_LOSS_DD*100:.0f}%")
            break
        if pnl_pct < GLOBAL_STOP_LOSS_PNL:
            logger.critical(f"🚨 STOP-LOSS (PnL total) : PnL={pnl_pct*100:.2f}% < {GLOBAL_STOP_LOSS_PNL*100:.0f}%")
            break

        slip_avg = max(state.get("ema_slippage_buy", 0.0), state.get("ema_slippage_sell", 0.0))

        stress = compute_stress(
            macro_data["adx"],
            macro_data["atr_norm"],
            macro_data["atr_norm_15m"],
            drawdown_dd,
            slip_avg
        )

        # ── Facteurs d'exposition asymétriques ──────────────────
        adx = macro_data["adx"]
        dip = macro_data["dip"]
        dim = macro_data["dim"]
        trend_ratio = dip / max(dim, 0.001)

        buy_exposure_factor = 1.0
        sell_exposure_factor = 1.0

        if adx > 70:
            if trend_ratio > 1.2:
                buy_exposure_factor = 0.35
                sell_exposure_factor = 1.0
            elif trend_ratio < 0.8:
                buy_exposure_factor = 1.0
                sell_exposure_factor = 0.35
            else:
                buy_exposure_factor = 0.35
                sell_exposure_factor = 0.35
        elif adx > 50:
            if trend_ratio > 1.2:
                buy_exposure_factor = 0.60
                sell_exposure_factor = 1.0
            elif trend_ratio < 0.8:
                buy_exposure_factor = 1.0
                sell_exposure_factor = 0.60
            else:
                buy_exposure_factor = 0.60
                sell_exposure_factor = 0.60
        else:
            buy_exposure_factor = 1.0
            sell_exposure_factor = 1.0

        regime = (
        "BULLISH" if trend_ratio > 1.2
        else "BEARISH" if trend_ratio < 0.8
        else "NEUTRAL"
        )

        current_exposure_state = (
            regime,
            "ADX70+" if adx > 70 else "ADX50+" if adx > 50 else "ADX<50",
            round(buy_exposure_factor, 2),
            round(sell_exposure_factor, 2),
        )

        if current_exposure_state != last_exposure_state:
            logger.info(
                f"🛡️ Exposition ADX={adx:.1f} | "
                f"Régime={regime} | "
                f"BUY_factor={buy_exposure_factor:.2f} "
                f"SELL_factor={sell_exposure_factor:.2f}"
            )
            last_exposure_state = current_exposure_state

        out_of_bounds = state["grid_ready"] and (price < state["Gll"] or price > state["Gul"])
        grid_empty = state["grid_ready"] and (len(state["sell_grid"])==0 and len(state["buy_grid"])==0)
        periodic_recalc = (time.time() - last_periodic_recalc) >= RECALC_PERIOD_SECONDS
        must_init = not state["grid_ready"] or out_of_bounds or grid_empty

        force_init = must_init
        if must_init and not state["grid_ready"]:
            last_attempt = state.get("last_grid_init_attempt", 0.0)
            if time.time() - last_attempt > FORCE_INIT_TIMEOUT:
                logger.warning(f"⏰ Timeout d'initialisation dépassé ({FORCE_INIT_TIMEOUT}s) — forcing init_grid même si ADX élevé")
                force_init = True

        if must_init or periodic_recalc:
            if periodic_recalc:
                last_periodic_recalc = time.time()
                logger.info(f"🔄 Recalcul périodique déclenché (intervalle={RECALC_PERIOD_SECONDS:.0f}s)")
            init_grid(price, macro_data["atr"], state, stress,
                      macro_data["dip"], macro_data["dim"],
                      macro_data["adx"], macro_data.get("atr_norm_15m",0.015),
                      force=force_init)
            state["last_grid_rebuild_ts"] = time.time()
            state["last_rebuild_price"] = price
            save_state(state)
            quote_bal, base_bal, total_wallet, capital_for_grid = get_balances(price)

        if not state["grid_ready"]:
            time.sleep(LOOP_SLEEP)
            continue

        sell_grid = state["sell_grid"]
        buy_grid = state["buy_grid"]

        last_grid_rebuild = state.get("last_grid_rebuild_ts", time.time())
        rebuild_eligible = (time.time() - last_grid_rebuild >= MIN_REBUILD_DELAY)
        grid_age = time.time() - last_grid_rebuild
        force_rebuild_by_age = grid_age > MAX_GRID_AGE

        nearest_buy = buy_grid[0] if len(buy_grid) > 0 else None
        nearest_buy_ratio = nearest_buy / price if price > 0 else 0.0
        last_rebuild_price = state.get("last_rebuild_price", 0.0)
        if last_rebuild_price <= 0 or state.get("P0") is None:
            last_rebuild_price = price
            state["last_rebuild_price"] = price
            save_state(state)

        price_progress = price / last_rebuild_price if last_rebuild_price > 0 else 1.0
        atr = macro_data.get("atr", 0.0)

        price_change = price - last_rebuild_price
        atr_filter = abs(price_change) > 2.0 * atr
        hysteresis_ok = abs(price_progress - 1.0) > 0.02

        if (rebuild_eligible and nearest_buy is not None and nearest_buy_ratio < REBUILD_RATIO
            and hysteresis_ok and atr_filter) or force_rebuild_by_age:
            logger.info(f"♻️ Recalcul grille (ratio BUY={nearest_buy_ratio:.4f}, age={grid_age/3600:.1f}h)")
            last_periodic_recalc = time.time()
            init_grid(price, macro_data["atr"], state, stress,
                      macro_data["dip"], macro_data["dim"],
                      macro_data["adx"], macro_data.get("atr_norm_15m",0.015),
                      force=True)
            state["last_grid_rebuild_ts"] = time.time()
            state["last_rebuild_price"] = price
            save_state(state)
            sell_grid = state["sell_grid"]
            buy_grid = state["buy_grid"]
            continue

        fee_buffer_buy = (TRADING_FEE_RT + state.get("ema_slippage_buy",0.0)) * EQ16_MIN_RATIO
        fee_buffer_sell = (TRADING_FEE_RT + state.get("ema_slippage_sell",0.0)) * EQ16_MIN_RATIO

        # ── TRAITEMENT BUY ──────────────────────────────────────
        while len(buy_grid) > 0 and price <= buy_grid[0]:
            quote_bal_virt_local = quote_bal
            base_for_grid_local = base_bal
            capital_for_grid_local = quote_bal_virt_local + base_for_grid_local * price
            capital_effectif = capital_for_grid_local * ACTIVE_CAPITAL_RATIO
            capital_effectif *= buy_exposure_factor
            Gv_local = compute_gv(capital_effectif, state["P0"], state["Gul"], state["Gll"],
                                  state["nu"], state["nl"], state["density_k"])
            state["Gv"] = Gv_local
            if quote_bal >= Gv_local:
                touched = buy_grid.pop(0)
                actual_buy_price, filled_qty = smart_execute_order(Client.SIDE_BUY, Gv_local, price, state, stress, macro_data)
                if actual_buy_price is not None and filled_qty > 0:
                    state["inventory_lots"].append({
                        "qty": filled_qty,
                        "buy_price": actual_buy_price,
                        "timestamp": time.time()
                    })
                    state["total_base_qty"] = state.get("total_base_qty", 0.0) + filled_qty

                    _gsl = state["Gsl"]
                    _gsu = state["Gsu"]
                    _dk  = state.get("density_k", 0.65)
                    _nl  = state["nl"]
                    _nu  = state["nu"]
                    _P0  = state["P0"]

                    if _gsl > 1.0 + 1e-9 and actual_buy_price < _P0:
                        ratio_buy = math.log(_P0 / actual_buy_price) / math.log(_gsl)
                        i_buy = _nl * math.pow(ratio_buy, 1.0 / _dk)
                        i_buy = max(1.0, min(float(_nl), i_buy))
                        new_sell_raw = _P0 * math.pow(_gsu, math.pow(i_buy / _nl, _dk))
                    else:
                        new_sell_raw = actual_buy_price * (1 + fee_buffer_sell)

                    min_sell = actual_buy_price * (1 + fee_buffer_sell)
                    new_sell_raw = max(new_sell_raw, min_sell)
                    new_sell = round(new_sell_raw, PRICE_DECIMALS)

                    sell_grid.add(new_sell)
                    state["sell_grid"] = sell_grid
                    state["buy_grid"] = buy_grid
                    save_state(state)
                    logger.info(f"⚡ ACHAT @ {actual_buy_price:.4f} | Qté={filled_qty:.4f} | Lots={len(state['inventory_lots'])}")
                    invalidate_balance_cache()
                    quote_bal = max(0.0, quote_bal - (filled_qty * actual_buy_price))
                    base_bal += filled_qty
                else:
                    buy_grid.add(touched)
                    break
            else:
                break

        # ── TRAITEMENT SELL ──────────────────────────────────────
        while len(sell_grid) > 0 and price >= sell_grid[0]:
            quote_bal_virt_local = quote_bal
            base_for_grid_local = base_bal
            capital_for_grid_local = quote_bal_virt_local + base_for_grid_local * price
            capital_effectif = capital_for_grid_local * ACTIVE_CAPITAL_RATIO
            capital_effectif *= sell_exposure_factor
            Gv_local = compute_gv(capital_effectif, state["P0"], state["Gul"], state["Gll"],
                                  state["nu"], state["nl"], state["density_k"])
            state["Gv"] = Gv_local
            qty_needed = Gv_local / price

            if state.get("total_base_qty", 0.0) >= qty_needed:
                touched = sell_grid.pop(0)
                actual_sell_price, filled_qty = smart_execute_order(Client.SIDE_SELL, Gv_local, price, state, stress, macro_data)
                if actual_sell_price is not None and filled_qty > 0:
                    remaining = filled_qty
                    pnl_trade = 0.0
                    new_lots = []
                    fee_buy = TRADING_FEE_RT + state.get("ema_slippage_buy", 0.0)
                    fee_sell = TRADING_FEE_RT + state.get("ema_slippage_sell", 0.0)

                    for lot in state.get("inventory_lots", []):
                        if remaining <= 0:
                            new_lots.append(lot)
                            continue
                        if lot["qty"] <= remaining:
                            qty_sold = lot["qty"]
                            pnl_trade += (actual_sell_price - lot["buy_price"]) * qty_sold
                            pnl_trade -= (actual_sell_price * qty_sold * fee_sell) + (lot["buy_price"] * qty_sold * fee_buy)
                            remaining -= lot["qty"]
                        else:
                            qty_sold = remaining
                            pnl_trade += (actual_sell_price - lot["buy_price"]) * qty_sold
                            pnl_trade -= (actual_sell_price * qty_sold * fee_sell) + (lot["buy_price"] * qty_sold * fee_buy)
                            lot["qty"] -= remaining
                            new_lots.append(lot)
                            remaining = 0
                    actually_sold_from_lots = filled_qty - remaining
                    if remaining > 1e-8:
                        logger.warning(
                            f"⚠️ Désynchronisation FIFO vente : {remaining:.6f} {BASE_ASSET} "
                            f"manquants dans les lots (qty_to_sell={filled_qty:.6f}, "
                            f"trouvé={actually_sold_from_lots:.6f}) — total_base_qty corrigé"
                        )
                    state["inventory_lots"] = new_lots
                    state["total_base_qty"] = max(0.0, state.get("total_base_qty", 0.0) - actually_sold_from_lots)
                    state["total_pnl"] = state.get("total_pnl", 0.0) + pnl_trade
                    logger.info(f"💰 PnL trade (net de frais) : {pnl_trade:+.4f} {QUOTE_ASSET} (vente @ {actual_sell_price:.4f}) | Cumulé={state['total_pnl']:.4f}")

                    _gsl = state["Gsl"]
                    _gsu = state["Gsu"]
                    _dk  = state.get("density_k", 0.65)
                    _nu  = state["nu"]
                    _nl  = state["nl"]
                    _P0  = state["P0"]

                    if _gsu > 1.0 + 1e-9 and actual_sell_price > _P0:
                        ratio_sell = math.log(actual_sell_price / _P0) / math.log(_gsu)
                        i_sell = _nu * math.pow(ratio_sell, 1.0 / _dk)
                        i_sell = max(1.0, min(float(_nu), i_sell))
                        new_buy_raw = _P0 / math.pow(_gsl, math.pow(i_sell / _nu, _dk))
                    else:
                        new_buy_raw = actual_sell_price * (1 - fee_buffer_buy)

                    max_buy = actual_sell_price * (1 - fee_buffer_buy)
                    new_buy_raw = min(new_buy_raw, max_buy)
                    new_buy = round(new_buy_raw, PRICE_DECIMALS)

                    buy_grid.add(new_buy)
                    state["sell_grid"] = sell_grid
                    state["buy_grid"] = buy_grid
                    save_state(state)
                    logger.info(f"⚡ VENTE @ {actual_sell_price:.4f} | Qté vendue={filled_qty:.4f} | Lots restants={len(state['inventory_lots'])}")
                    invalidate_balance_cache()
                    proceeds = filled_qty * actual_sell_price
                    quote_bal += proceeds / NB_BOTS
                    base_bal = max(0.0, base_bal - filled_qty)
                else:
                    sell_grid.add(touched)
                    break
            else:
                break

        # ── LOG PÉRIODIQUE ──────────────────────────────────────
        if time.time() - last_log_time >= 10.0:
            last_log_time = time.time()
            
            save_state(state)

            inventory_qty = state.get("total_base_qty", 0.0)
            nb_lots = len(state.get("inventory_lots", []))
            avg_cost = 0.0
            unrealized_pnl = 0.0
            if inventory_qty > 0:
                total_cost = sum(lot["qty"] * lot["buy_price"] for lot in state["inventory_lots"])
                avg_cost = total_cost / inventory_qty
                unrealized_pnl = (price - avg_cost) * inventory_qty

            gv_display = state.get("Gv", 0.0)
            pnl_pct = (state.get("total_pnl", 0.0) + unrealized_pnl) / _capital_initial if _capital_initial and _capital_initial > 0 else 0.0

            logger.info(
                f"📊 {price:.4f} | Capital={total_wallet:.2f} | CapitalGrid={capital_for_grid:.2f} | Stress={stress:.2f} | "
                f"BUY={len(buy_grid)} SELL={len(sell_grid)} | Gv={gv_display:.2f} | k={state['density_k']:.2f} | "
                f"Trades={state['total_trades']} | PnL réalisé={state.get('total_pnl',0.0):.4f} | UPnL={unrealized_pnl:+.4f} | PnL total={pnl_pct*100:+.2f}% | "
                f"Stock={inventory_qty:.4f} (moy={avg_cost:.4f}) | Lots={nb_lots} | "
                f"EMA_B={state.get('ema_slippage_buy',0.0):.4%} EMA_S={state.get('ema_slippage_sell',0.0):.4%}"
            )

        time.sleep(LOOP_SLEEP)

    except Exception as e:
        logger.error(f"❌ Erreur boucle : {e}")
        failed_consecutive += 1
        time.sleep(5)

stop_price_websocket()
remove_lock(SYMBOL)
logger.info("🛑 Arrêt propre — sauvegarde finale...")
save_state(state)
logger.info("✅ Bot arrêté.")
