BOT_VERSION = "V108-RN019"

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

import inventory_manager as inv_mgr

from exchange_base import OrderResult
from exchange_gateio import ExchangeGateIO

from inventory_utils import verify_and_resync_inventory_cost
from order_reconciliation import reconcile_open_orders as reconcile_normalized_orders
from calibration_safety import (
    apply_calibration_atomically,
    run_calibration,
)
from startup_sequence import StartupSequence
from process_synchronization import AtomicJsonStateStore, BotLock, LockUnavailableError

from capital_view import CapitalView, CapitalViewBuilder, compute_capital_view

from capital_transition_guard import (
    CapitalTransitionGuard,
    CapitalTransitionJournal,
    TransitionStatus,
)
from bot_capital_sync import StateDictEconomicRepository, build_manual_sync_request
from bot_realized_pnl_sync import build_realized_profit_request, build_realized_loss_request

from history_logger import HistoryLogger
history_logger = HistoryLogger()

exchange = None

try:
    import websocket
    _WS_AVAILABLE = True
except ImportError:
    websocket = None
    _WS_AVAILABLE = False
    print("⚠️  'websocket-client' n'est pas installé.")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  Attention: 'python-dotenv' n'est pas installé.")

try:
    from script_atr import calibrate
    _CALIBRATE_AVAILABLE = True
except ImportError:
    calibrate = None
    _CALIBRATE_AVAILABLE = False
    print("⚠️  'script_atr' non trouvé.")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grid Trading Bot — V108 (RN-019 : architecture économique figée)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "symbol",
        nargs="?",
        default="INJUSDC",
        type=str,
        help="Paire de trading (ex: INJUSDC, EGLDUSDT)",
    )
    parser.add_argument("_p_budget", nargs="?", type=float, default=None, help=argparse.SUPPRESS)

    parser.add_argument("--budget", dest="n_budget", type=float, default=None, metavar="USDC")
    parser.add_argument("--bots", type=int, default=None,
                        help="Nombre de bots se partageant le capital USDC (détection auto si omis)")
    parser.add_argument("--reconcile", action="store_true",
                        help="Réconcilier l'inventaire et les ordres ouverts (ne modifie pas le capital stratégique)")
    parser.add_argument("--sync-capital", action="store_true",
                        help="Recalcule allocated_capital à partir du wallet réel, conserve l'historique (FIFO, PnL, peak)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Niveau de log (défaut: INFO)")

    raw = parser.parse_args()
    raw.symbol = raw.symbol.upper()

    ns = argparse.Namespace()
    ns.symbol          = raw.symbol
    ns.max_budget_usdc = raw.n_budget if raw.n_budget is not None else raw._p_budget
    ns.bots            = raw.bots
    ns.reconcile       = raw.reconcile
    ns.sync_capital    = raw.sync_capital
    ns.log_level       = raw.log_level
    return ns

args = parse_args()

SYMBOL           = args.symbol
CURRENT_SYMBOL   = args.symbol
MAX_BUDGET_USDC  = args.max_budget_usdc
NB_BOTS          = args.bots
AUTO_RECONCILE   = args.reconcile
SYNC_CAPITAL     = args.sync_capital
LOG_LEVEL        = getattr(logging, args.log_level.upper())

if SYMBOL.endswith("USDC"):
    BASE_ASSET = SYMBOL[:-4]
    QUOTE_ASSET = "USDC"
elif SYMBOL.endswith("USDT"):
    BASE_ASSET = SYMBOL[:-4]
    QUOTE_ASSET = "USDT"
else:
    print(f"❌ Paire {SYMBOL} non supportée.")
    sys.exit(1)

# Paramètres par défaut (écrasés par calibration)
if SYMBOL == "INJUSDC":
    DENSITY_ATR_LOW  = 0.0070
    DENSITY_ATR_HIGH = 0.0133
    DENSITY_K_MIN    = 0.50
    DENSITY_K_MAX    = 1.00
elif SYMBOL == "EGLDUSDC":
    DENSITY_ATR_LOW  = 0.0032
    DENSITY_ATR_HIGH = 0.0057
    DENSITY_K_MIN    = 0.50
    DENSITY_K_MAX    = 1.00
elif SYMBOL == "FILUSDC":
    DENSITY_ATR_LOW  = 0.0031
    DENSITY_ATR_HIGH = 0.0087
    DENSITY_K_MIN    = 0.60
    DENSITY_K_MAX    = 1.00
else:
    DENSITY_ATR_LOW  = 0.004
    DENSITY_ATR_HIGH = 0.008
    DENSITY_K_MIN    = 0.50
    DENSITY_K_MAX    = 1.00

NU_MIN = 2
NU_MAX = 8
NL_MIN = 2
NL_MAX = 8

KLINE_INTERVAL = ExchangeGateIO.KLINE_3M
KLINE_LIMIT = 100

SLIPPAGE_EMA_ALPHA = 0.20
STRESS_LIMIT_FOR_MAKER = 0.30

NU_LEVELS = 5
NL_LEVELS = 5

ACTIVE_CAPITAL_RATIO = 0.9
MAX_CELL_RATIO       = 0.8
GV_MULTIPLIER        = 1.0

ATR_BASE_MULT = 7.0

GUL_HARD_MIN_PCT = 0.020
GUL_HARD_MAX_PCT = 0.15
GLL_HARD_MIN_PCT = 0.020
GLL_HARD_MAX_PCT = 0.15

TRADING_FEE_RT    = 0.001
EQ16_MIN_RATIO    = 2.0
EQ16_MAX_RETRIES  = 3

ADX_MAKER_LIMIT        = 40
LIMIT_TIMEOUT_SECONDS  = 15

MIN_ORDER_USDC  = 5.5
KLINE_LIMIT     = 50
LOOP_SLEEP      = 0.2
INDICATORS_FREQ = 60
ADX_TREND_LIMIT = 50
STATE_FILE = f"state_gateio_{SYMBOL.lower()}.json"
STATE_STORE = AtomicJsonStateStore(STATE_FILE)
JOURNAL_FILE = f"journal_gateio_{SYMBOL.lower()}.jsonl"
SNAPSHOT_FILE   = "snapshot_gateio_t0.json"
SNAPSHOT_META_KEYS = {"date_reference", "timestamp_reference", "exchange", "cash_reel_t0"}
FAILED_COOLDOWN_INITIAL = 3
FAILED_COOLDOWN_MAX     = 60
GLOBAL_STOP_LOSS_DD = 0.25
GLOBAL_STOP_LOSS_PNL = -0.10
DRAWDOWN_WARNING_THRESHOLD = 0.30

LOCK_ACQUIRE_TIMEOUT = 10.0
WS_PRICE_MAX_AGE  = 20.0
WS_CHECK_INTERVAL = 60
WS_FORCE_RESTART_AGE = 60.0

FORCE_INIT_TIMEOUT = 600

CAPITAL_CHECK_INTERVAL = 5  # secondes entre les vérifications du state en attente

PRICE_DECIMALS = 4
QTY_DECIMALS   = 2
MIN_NOTIONAL = 0.0
MIN_QTY      = 0.0

METRICS_INTERVAL = 30
METRICS_MAX_SIZE = 100 * 1024 * 1024

_last_get_order_time = 0.0
MIN_GET_ORDER_INTERVAL = 1.0

ws_price      = None
ws_price_time = 0.0
ws_price_lock = threading.Lock()
ws_running    = False
ws_connecting = False
ws_thread     = None
ws_app        = None

ws_retry_count  = 0
_ws_retry_lock  = threading.Lock()
WS_BACKOFF_BASE = 1.0
WS_BACKOFF_MAX  = 60.0

_ws_reconnect_timer = None
_ws_reconnect_lock  = threading.Lock()

failed_consecutive = 0
_capital_initial = None

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

exchange = ExchangeGateIO()

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

class TradeJournal:
    def __init__(self, symbol: str):
        self.path = JOURNAL_FILE
        self._lock = threading.Lock()
        logger.info(f"📒 Journal des transactions : {self.path}")

    def _write(self, entry: dict):
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"❌ Erreur écriture journal : {e}")

    def _base(self, event: str) -> dict:
        now = time.time()
        ms = int((now % 1) * 1000)
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{ms:03d}Z"
        return {"ts": round(now, 3), "ts_iso": ts_iso, "event": event, "symbol": SYMBOL}

    def _grid_ctx(self, state: dict, stress: float, adx: float) -> dict:
        return {
            "P0":           round(state.get("P0", 0.0),        PRICE_DECIMALS),
            "Gv":           round(state.get("Gv", 0.0),        2),
            "density_k":    round(state.get("density_k", 0.0), 3),
            "stress":       round(stress,                       3),
            "adx":          round(adx,                          1),
            "total_trades": state.get("total_trades", 0),
        }

    def log_startup(self, *, state: dict, capital_usdc: float, nb_bots: int):
        entry = self._base("STARTUP")
        entry.update({
            "capital_usdc":   round(capital_usdc, 2),
            "nb_bots":        nb_bots,
            "grid_ready":     state.get("grid_ready", False),
            "total_trades":   state.get("total_trades", 0),
            "total_pnl":      round(state.get("total_pnl", 0.0), 4),
            "total_base_qty": round(state.get("total_base_qty", 0.0), QTY_DECIMALS),
            "nb_lots":        len(state.get("inventory_lots", [])),
            "wallet_peak":    round(state.get("wallet_peak", 0.0), 2),
            "DENSITY_ATR_LOW":  state.get("DENSITY_ATR_LOW",  DENSITY_ATR_LOW),
            "DENSITY_ATR_HIGH": state.get("DENSITY_ATR_HIGH", DENSITY_ATR_HIGH),
        })
        self._write(entry)

    def log_grid_init(self, *, state: dict, regime: str, reason: str,
                      sell_grid_list: list, buy_grid_list: list):
        entry = self._base("GRID_INIT")
        entry.update({
            "reason":     reason,
            "regime":     regime,
            "P0":         round(state.get("P0", 0.0),   PRICE_DECIMALS),
            "Gul":        round(state.get("Gul", 0.0),  PRICE_DECIMALS),
            "Gll":        round(state.get("Gll", 0.0),  PRICE_DECIMALS),
            "nu":         state.get("nu", 0),
            "nl":         state.get("nl", 0),
            "Gv":         round(state.get("Gv", 0.0),   2),
            "density_k":  round(state.get("density_k", 0.0), 3),
            "sell_grid":  [round(p, PRICE_DECIMALS) for p in sell_grid_list],
            "buy_grid":   [round(p, PRICE_DECIMALS) for p in buy_grid_list],
            "DENSITY_ATR_LOW":  state.get("DENSITY_ATR_LOW",  DENSITY_ATR_LOW),
            "DENSITY_ATR_HIGH": state.get("DENSITY_ATR_HIGH", DENSITY_ATR_HIGH),
        })
        self._write(entry)

    def log_buy(self, *, grid_level: float, trigger_price: float,
                exec_price: float, qty_base: float, new_sell_level: float,
                state: dict, stress: float, adx: float):
        entry = self._base("BUY")
        slippage = abs(exec_price - trigger_price) / trigger_price if trigger_price > 0 else 0.0
        entry.update({
            "grid_level":          round(grid_level,    PRICE_DECIMALS),
            "trigger_price":       round(trigger_price, PRICE_DECIMALS),
            "exec_price":          round(exec_price,    PRICE_DECIMALS),
            "qty_base":            round(qty_base,      QTY_DECIMALS),
            "qty_quote":           round(exec_price * qty_base, 4),
            "slippage_pct":        round(slippage * 100, 4),
            "new_sell_level":      round(new_sell_level, PRICE_DECIMALS),
            "inventory_qty_after": round(state.get("total_base_qty", 0.0), QTY_DECIMALS),
            "nb_lots_after":       len(state.get("inventory_lots", [])),
            "total_pnl_after":     round(state.get("total_pnl", 0.0), 4),
            **self._grid_ctx(state, stress, adx),
        })
        self._write(entry)

    def log_sell(self, *, grid_level: float, trigger_price: float,
                 exec_price: float, qty_base: float, pnl_trade: float,
                 lots_consumed: list, new_buy_level: float,
                 state: dict, stress: float, adx: float):
        entry = self._base("SELL")
        slippage = abs(exec_price - trigger_price) / trigger_price if trigger_price > 0 else 0.0
        fee_buy  = TRADING_FEE_RT + state.get("ema_slippage_buy",  0.0)
        fee_sell = TRADING_FEE_RT + state.get("ema_slippage_sell", 0.0)
        lots_detail = []
        for lot in lots_consumed:
            gross = (exec_price - lot["buy_price"]) * lot["qty"]
            fees  = (exec_price * lot["qty"] * fee_sell) + (lot["buy_price"] * lot["qty"] * fee_buy)
            lots_detail.append({
                "qty":       round(lot["qty"],       QTY_DECIMALS),
                "buy_price": round(lot["buy_price"], PRICE_DECIMALS),
                "pnl_gross": round(gross,       4),
                "pnl_net":   round(gross - fees, 4),
            })
        entry.update({
            "grid_level":          round(grid_level,    PRICE_DECIMALS),
            "trigger_price":       round(trigger_price, PRICE_DECIMALS),
            "exec_price":          round(exec_price,    PRICE_DECIMALS),
            "qty_base":            round(qty_base,      QTY_DECIMALS),
            "qty_quote":           round(exec_price * qty_base, 4),
            "slippage_pct":        round(slippage * 100, 4),
            "pnl_trade":           round(pnl_trade, 4),
            "total_pnl_after":     round(state.get("total_pnl", 0.0), 4),
            "lots_consumed":       lots_detail,
            "new_buy_level":       round(new_buy_level, PRICE_DECIMALS),
            "inventory_qty_after": round(state.get("total_base_qty", 0.0), QTY_DECIMALS),
            "nb_lots_after":       len(state.get("inventory_lots", [])),
            **self._grid_ctx(state, stress, adx),
        })
        self._write(entry)

    def log_stop_loss(self, *, reason: str, drawdown: float, pnl_pct: float,
                      total_pnl: float, total_wallet: float, wallet_peak: float):
        entry = self._base("STOP_LOSS")
        entry.update({
            "reason":       reason,
            "drawdown_pct": round(drawdown * 100, 2),
            "pnl_pct":      round(pnl_pct * 100,  2),
            "total_pnl":    round(total_pnl,       4),
            "total_wallet": round(total_wallet,    2),
            "wallet_peak":  round(wallet_peak,     2),
        })
        self._write(entry)

    def log_capital_sync(self, old_allocated: float, new_allocated: float, wallet_real: float, reason: str):
        entry = self._base("CAPITAL_SYNC")
        entry.update({
            "old_allocated": round(old_allocated, 2),
            "new_allocated": round(new_allocated, 2),
            "wallet_real": round(wallet_real, 2),
            "reason": reason,
        })
        self._write(entry)

def get_lock_file(symbol: str) -> str:
    return f"lock_{symbol.lower()}.pid"

bot_process_lock = None

def create_lock(symbol: str) -> bool:
    global bot_process_lock
    if bot_process_lock is not None and bot_process_lock.held:
        return True
    try:
        bot_process_lock = BotLock(
            get_lock_file(symbol), timeout=LOCK_ACQUIRE_TIMEOUT, version=BOT_VERSION
        )
        bot_process_lock.acquire()
        logger.info(f"🔒 Verrou inter-processus acquis pour {symbol}")
        return True
    except LockUnavailableError as e:
        logger.error(f"❌ Impossible d'acquérir le verrou de {symbol} : {e}")
        bot_process_lock = None
        return False
    except Exception as e:
        logger.exception(f"❌ Erreur verrou inter-processus {symbol} : {e}")
        bot_process_lock = None
        return False

def update_lock(symbol: str) -> bool:
    if bot_process_lock is None or not bot_process_lock.held:
        return False
    try:
        bot_process_lock.refresh_metadata()
        return True
    except Exception as e:
        logger.error(f"❌ Erreur actualisation métadonnées lock {symbol} : {e}")
        return False

def remove_lock(symbol: str):
    global bot_process_lock
    if bot_process_lock is not None:
        bot_process_lock.release()
        bot_process_lock = None
        logger.info(f"🔓 Verrou inter-processus libéré pour {symbol}")

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
    for lock_path in lock_files:
        try:
            with open(lock_path, "r") as f:
                content = f.read().strip()
            metadata = json.loads(content)
            pid = int(metadata["pid"])
            if pid <= 0:
                logger.warning(f"⚠️ Lock invalide (format): {lock_path}")
                continue
            if pid == current_pid:
                active_bots += 1
                continue
            if is_process_alive(pid):
                active_bots += 1
        except Exception as e:
            logger.error(f"❌ Erreur lecture lock {lock_path} : {e}")
    return max(1, active_bots)

def get_snapshot_bot_count() -> int:
    if not os.path.exists(SNAPSHOT_FILE):
        return 1
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snap = json.load(f)
        tokens = [k for k, v in snap.items() if isinstance(v, dict) and "stock" in v]
        return max(1, len(tokens))
    except Exception as e:
        logger.warning(f"⚠️ Impossible de lire {SNAPSHOT_FILE} pour compter les bots : {e}")
        return 1

if NB_BOTS is None:
    if not create_lock(SYMBOL):
        logger.error("❌ Échec de création du lock, arrêt.")
        sys.exit(1)
    NB_BOTS = get_snapshot_bot_count()
    logger.info(f"🤖 {NB_BOTS} bot(s) actif(s) détecté(s) (fichiers lock)")
else:
    if not create_lock(SYMBOL):
        logger.error("❌ Échec de création du lock, arrêt.")
        sys.exit(1)
    logger.info(f"🤖 Partage forcé : {NB_BOTS} bot(s) (argument --bots)")

_shutdown_requested = False
_startup_ready = False

def _handle_signal(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

def get_instant_price() -> float | None:
    return exchange.get_ticker_price(SYMBOL)

def start_price_websocket(symbol: str):
    global ws_price, ws_price_time, ws_running, ws_connecting, ws_thread, ws_app, ws_retry_count
    if not _WS_AVAILABLE:
        logger.warning("⚠️ websocket-client non installé — mode REST uniquement")
        return
    if ws_running or ws_connecting:
        with _ws_retry_lock:
            ws_retry_count = 0
        return

    ws_connecting = True
    ws_url = exchange.get_ws_stream_url(symbol)
    logger.info(f"🔌 Connexion à {ws_url} (tentative {ws_retry_count+1})")

    def on_message(ws, message):
        global ws_price, ws_price_time
        price = exchange.parse_ws_trade_price(message)
        if price:
            with ws_price_lock:
                ws_price = price
                ws_price_time = time.time()

    def on_error(ws, error):
        logger.error(f"Erreur WebSocket : {error}")
        try:
            ws.close()
        except Exception:
            pass

    def on_close(ws, close_status_code, close_msg):
        global ws_running, ws_connecting
        if not ws_running:
            return
        ws_running = False
        ws_connecting = False
        logger.warning("WebSocket fermé")
        if _startup_ready:
            _schedule_ws_reconnect(symbol)

    def on_open(ws):
        global ws_running, ws_connecting, ws_retry_count
        ws_running = True
        ws_connecting = False
        with _ws_retry_lock:
            ws_retry_count = 0
        ws.send(json.dumps(exchange.get_ws_subscribe_message(symbol)))
        logger.info(f"🌐 WebSocket connecté pour {symbol} (stream @trade)")

    ws_app = websocket.WebSocketApp(ws_url,
                                    on_open=on_open,
                                    on_message=on_message,
                                    on_error=on_error,
                                    on_close=on_close)
    def run_ws():
        global ws_running, ws_connecting
        try:
            ws_app.run_forever(
                ping_interval=20,
                ping_timeout=10,
            )
        finally:
            logger.warning("⚠️ Thread WebSocket terminé")
            ws_running = False
            ws_connecting = False
            if not _shutdown_requested and _startup_ready:
                _schedule_ws_reconnect(symbol)

    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()

def stop_price_websocket():
    global ws_running, ws_connecting, ws_app, _ws_reconnect_timer
    with _ws_reconnect_lock:
        if _ws_reconnect_timer is not None:
            _ws_reconnect_timer.cancel()
            _ws_reconnect_timer = None
    if ws_running and ws_app:
        try:
            ws_app.close()
        except Exception as e:
            logger.error(f"Erreur fermeture WS : {e}")
    ws_running = False
    ws_connecting = False
    logger.info("🛑 WebSocket arrêté")

def _schedule_ws_reconnect(symbol: str):
    global _ws_reconnect_timer, ws_retry_count
    if _shutdown_requested or not _startup_ready:
        return
    with _ws_reconnect_lock:
        if _ws_reconnect_timer is not None:
            _ws_reconnect_timer.cancel()
        with _ws_retry_lock:
            ws_retry_count += 1
            retry_n = ws_retry_count
        delay = min(WS_BACKOFF_BASE * (2 ** (retry_n - 1)), WS_BACKOFF_MAX)
        logger.info(f"⏳ Backoff WebSocket : attente {delay:.1f}s avant tentative {retry_n}")
        t = threading.Timer(delay, _ws_do_reconnect, args=(symbol,))
        t.daemon = True
        t.start()
        _ws_reconnect_timer = t

def _ws_do_reconnect(symbol: str):
    global _ws_reconnect_timer
    with _ws_reconnect_lock:
        _ws_reconnect_timer = None
    if _startup_ready and not _shutdown_requested and not ws_running:
        start_price_websocket(symbol)

def get_ws_price() -> float | None:
    with ws_price_lock:
        p = ws_price
        age = time.time() - ws_price_time
    if p is None or age > WS_PRICE_MAX_AGE:
        if p is not None and age > WS_PRICE_MAX_AGE:
            if not hasattr(get_ws_price, "_last_warn_time") or time.time() - get_ws_price._last_warn_time > 60:
                get_ws_price._last_warn_time = time.time()
                logger.warning(f"⚠️ Prix WS périmé ({age:.1f}s > {WS_PRICE_MAX_AGE}s) — fallback REST")
        try:
            return get_instant_price()
        except Exception as e:
            logger.warning(f"⚠️ Fallback REST échoué ({type(e).__name__}) : {e}")
            return None
    return p

def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _get_capital_from_snapshot(token: str) -> float | None:
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snap = json.load(f)
        token_data = snap.get(token, {})
        if isinstance(token_data, dict):
            return token_data.get("capital")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Lecture {SNAPSHOT_FILE} impossible pour capital de {token} : {e}")
        return None

# ============================================================
# NOUVELLE FONCTION ensure_capital_usdc (lecture seule)
# ============================================================
def ensure_capital_usdc(state: dict, price: float, quote_bal_real: float, base_bal_real: float) -> float:
    """
    Retourne le capital stratégique alloué (allocated_capital).
    Ne recalcule plus rien à partir des soldes.
    Cette fonction est conservée pour compatibilité, mais son rôle est réduit.
    """
    return state.get("allocated_capital", 0.0)

# ============================================================
# compute_capital_view : importée depuis capital_view.py (RN-020)
# Single Source of Economic Truth — voir capital_view.compute_capital_view
# ============================================================

def get_balances(price: float, state: dict | None = None, symbol: str = None) -> tuple[float, float, float, float]:
    global _capital_initial

    quote_bal_real, base_bal_real = exchange.get_balances(QUOTE_ASSET, BASE_ASSET)

    if state is None:
        total_wallet = quote_bal_real + base_bal_real * price
        capital_for_grid = min(total_wallet, MAX_BUDGET_USDC) if MAX_BUDGET_USDC else total_wallet
        if _capital_initial is None and total_wallet > 0:
            _capital_initial = total_wallet
            logger.info(f"💰 Capital de référence (wallet) : {_capital_initial:.2f} {QUOTE_ASSET}")
        return quote_bal_real, base_bal_real, total_wallet, capital_for_grid

    view = compute_capital_view(state, price, quote_bal_real, base_bal_real, update_peak=False)

    if _capital_initial is None and view["allocated_capital"] > 0:
        _capital_initial = view["allocated_capital"]
        logger.info(f"💰 Budget stratégique : {_capital_initial:.2f} {QUOTE_ASSET}")

    if history_logger and symbol:
        history_logger.log_capital_view(view, symbol)

    return (view["quote_available"],
            view["base_available"],
            view["wallet_real"],
            view["capital_for_grid"])

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
        info = exchange.get_symbol_info(SYMBOL)
        PRICE_DECIMALS = info.price_decimals
        QTY_DECIMALS   = info.qty_decimals
        MIN_QTY        = info.min_qty
        MIN_NOTIONAL   = info.min_notional
        logger.info(f"⚙️ Précisions -> Prix: {PRICE_DECIMALS} déc. | Qté: {QTY_DECIMALS} déc. | MIN_QTY: {MIN_QTY} | MIN_NOTIONAL: {MIN_NOTIONAL}")
    except Exception as e:
        logger.error(f"❌ Précisions non chargées : {e} — arrêt")
        sys.exit(1)

def migrate_old_state(state: dict) -> dict:
    migrated = False
    # Migration des positions vers lots
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

    # Migration capital_usdc -> allocated_capital
    if "capital_usdc" in state and "allocated_capital" not in state:
        state["allocated_capital"] = state["capital_usdc"]
        logger.info("🔄 Migration : capital_usdc -> allocated_capital")
        migrated = True
    elif "allocated_capital" not in state:
        state["allocated_capital"] = 0.0

    if migrated:
        logger.info("✅ Migration d'état effectuée")
    return state

def load_state() -> dict:
    defaults = {
        "grid_ready": False,
        "P0": None, "Gul": None, "Gll": None, "Gsu": None, "Gsl": None, "Gv": None,
        "sell_grid": [], "buy_grid": [],
        "nu": NU_LEVELS, "nl": NL_LEVELS,
        "wallet_peak": 0.0, "total_trades": 0, "failed_count": 0,
        "total_slippage": 0.0, "cycle_recalc": 0,
        "ema_slippage_buy": 0.0, "ema_slippage_sell": 0.0,
        "allocated_capital": 0.0,   # Nouveau nom
        "total_pnl": 0.0,
        "density_k": 0.65,
        "last_rebuild_price": 0.0,
        "inventory_lots": [],
        "inventory_cost": 0.0,
        "total_base_qty": 0.0,
        "last_grid_rebuild_ts": time.time(),
        "last_grid_init_attempt": 0.0,
        "DENSITY_ATR_LOW": DENSITY_ATR_LOW,
        "DENSITY_ATR_HIGH": DENSITY_ATR_HIGH,
        "DENSITY_K_MIN": DENSITY_K_MIN,
        "DENSITY_K_MAX": DENSITY_K_MAX,
        "CALIB_REF_ATR_LOW": DENSITY_ATR_LOW,
        "CALIB_REF_ATR_HIGH": DENSITY_ATR_HIGH,
        "CALIB_REF_K_MIN": DENSITY_K_MIN,
        "last_calibration_time": 0.0,
        "reconciled_orders": {},
    }
    try:
        persisted_state = STATE_STORE.read()
        if persisted_state is not None:
            state = {**defaults, **persisted_state}
            state = migrate_old_state(state)

            state, corrected = verify_and_resync_inventory_cost(state)
            if corrected:
                logger.warning(
                    "⚠️ inventory_cost incohérent détecté dans le state. "
                    "Valeur recalculée automatiquement à partir des inventory_lots."
                )

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
        elif os.path.exists(STATE_FILE):
            logger.warning(f"⚠️ State illisible et aucune sauvegarde exploitable : {STATE_FILE}")
    except Exception as e:
        logger.warning(f"⚠️ Impossible de lire {STATE_FILE} : {e}")
    defaults["sell_grid"] = SortedGrid(reverse=False)
    defaults["buy_grid"] = SortedGrid(reverse=True)
    return defaults

def save_state(state: dict):
    state, _ = verify_and_resync_inventory_cost(state)
    state_copy = state.copy()
    if isinstance(state_copy.get("sell_grid"), SortedGrid):
        state_copy["sell_grid"] = state_copy["sell_grid"].to_list()
    if isinstance(state_copy.get("buy_grid"), SortedGrid):
        state_copy["buy_grid"] = state_copy["buy_grid"].to_list()
    try:
        if bot_process_lock is None or not bot_process_lock.held:
            raise RuntimeError("sauvegarde refusée sans verrou inter-processus du bot")
        STATE_STORE.write(state_copy)
    except Exception as e:
        logger.error(f"❌ Erreur sauvegarde : {e}")

def reset_economic_period(state, wallet_real, reason):
    """
    Réinitialise volontairement la référence de drawdown en ouvrant une nouvelle période économique.

    Préconditions (à garantir par l'appelant) :
    - balances relues depuis l'exchange
    - wallet_real validé (valeur cohérente, issue de compute_capital_view)
    - ordres ouverts réconciliés
    - opération explicitement demandée par l'utilisateur

    Ne jamais appeler depuis la boucle principale.
    Ne jamais appeler automatiquement.
    """
    old_peak = float(state.get("wallet_peak", wallet_real))

    # Idempotence : si le peak est déjà égal à wallet_real, on ne fait rien (log DEBUG)
    if abs(old_peak - wallet_real) < 1e-9:
        logger.debug(
            "ECONOMIC_PERIOD_RESET (ignoré - idempotent) "
            f"peak={old_peak:.2f} wallet_real={wallet_real:.2f} reason={reason}"
        )
        return

    # Log explicite (INFO, car opération normale et volontaire)
    logger.info(f"🟡 Ouverture d'une nouvelle période économique (reason={reason})")

    # Modification unique
    state["wallet_peak"] = wallet_real

    # Sauvegarde atomique immédiate (UNIQUE sauvegarde)
    save_state(state)

    # Journalisation
    logger.info(
        "ECONOMIC_PERIOD_RESET "
        f"reason={reason} "
        f"old_wallet_peak={old_peak:.2f} "
        f"new_wallet_peak={wallet_real:.2f}"
    )

def reconcile_inventory(state: dict, price: float):
    exchange.invalidate_balance_cache()
    _, real_base_bal = exchange.get_balances(QUOTE_ASSET, BASE_ASSET)
    acquisition_price = state.get("P0") or price
    changed = inv_mgr.reconcile(
        state,
        real_balance=real_base_bal,
        acquisition_price=acquisition_price,
        source="exchange_reconcile",
    )
    if changed:
        state["total_base_qty"] = inv_mgr.inventory_qty(state)
        logger.info(f"✅ Inventaire réconcilié : {state['total_base_qty']:.6f}")
        save_state(state)
    else:
        logger.info("✅ Inventaire cohérent")

def reconcile_open_orders(state: dict):
    try:
        open_orders = exchange.get_open_orders(SYMBOL)
        if not open_orders:
            logger.warning(f"⚠️ Aucun ordre ouvert trouvé sur {exchange.NAME}.")
            if state.get("grid_ready", False):
                logger.warning("⚠️ Le state indique une grille active mais l'exchange n'a aucun ordre.")
                logger.warning("🔄 Réinitialisation de la grille demandée.")
                state["grid_ready"] = False
                state["buy_grid"] = []
                state["sell_grid"] = []
                save_state(state)
            return True

        logger.info(f"🔍 {len(open_orders)} ordre(s) ouvert(s) détecté(s) sur {exchange.NAME}.")

        def cancel_normalized_order(order_id: str | int):
            exchange.cancel_order(SYMBOL, order_id)
            logger.info(f"🗑️ Ordre {order_id} annulé.")

        result = reconcile_normalized_orders(
            state,
            open_orders,
            buy_side=exchange.SIDE_BUY,
            sell_side=exchange.SIDE_SELL,
            cancel_order=cancel_normalized_order,
            persist_state=lambda: save_state(state),
        )
        save_state(state)
        logger.info("✅ Réconciliation des ordres ouverts terminée "
                    f"({result['deltas_applied']} delta(s) appliqué(s)).")
        if result["cancel_failures"]:
            logger.warning(f"⚠️ {result['cancel_failures']} annulation(s) échouée(s).")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur réconciliation des ordres ouverts : {e}")
        return False

def get_heavy_indicators() -> dict | None:
    try:
        df_3m  = exchange.get_klines(SYMBOL, exchange.KLINE_3M,  KLINE_LIMIT)
        df_15m = exchange.get_klines(SYMBOL, exchange.KLINE_15M, 50)
        atr_series_3m = ta.volatility.average_true_range(df_3m["high"], df_3m["low"], df_3m["close"], window=14)
        atr = float(atr_series_3m.iloc[-1])
        atr_norm = float(atr_series_3m.iloc[-1]) / float(df_3m["close"].iloc[-1]) if float(df_3m["close"].iloc[-1]) > 0 else 0.01
        dip = ta.trend.adx_pos(df_3m["high"], df_3m["low"], df_3m["close"], window=14).iloc[-1]
        dim = ta.trend.adx_neg(df_3m["high"], df_3m["low"], df_3m["close"], window=14).iloc[-1]
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

CapitalViewBuilder.compute_gv_fn = compute_gv

def compute_density_k(atr_norm_15m, state):
    atr_low = state.get("DENSITY_ATR_LOW", DENSITY_ATR_LOW)
    atr_high = state.get("DENSITY_ATR_HIGH", DENSITY_ATR_HIGH)
    k_min = state.get("DENSITY_K_MIN", DENSITY_K_MIN)
    k_max = state.get("DENSITY_K_MAX", DENSITY_K_MAX)
    ratio = max(0.0, min(1.0, (atr_norm_15m - atr_low) / (atr_high - atr_low)))
    return k_max - (k_max - k_min) * ratio

def compute_min_gap(state):
    fee_buy = TRADING_FEE_RT + state.get("ema_slippage_buy", 0.0)
    fee_sell = TRADING_FEE_RT + state.get("ema_slippage_sell", 0.0)
    total_cost = fee_buy + fee_sell
    min_gap = total_cost * 1.5
    base_min = TRADING_FEE_RT * EQ16_MIN_RATIO
    return max(min_gap, base_min)

def format_capital_view(view: CapitalView) -> str:
    lines = []
    lines.append("═══════════════════════════════════════════════════════════════")
    lines.append(f"  CapitalView  [{view.symbol}]  @ {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(view.timestamp))}")
    lines.append("───────────────────────────────────────────────────────────────")
    lines.append(f"  Wallet réel  : {view.wallet_balance:>8.2f} USDT")
    lines.append(f"  Alloué       : {view.reference_budget:>8.2f}")
    lines.append(f"  Alpha        : {view.alpha:>+8.2f}")
    lines.append(f"  Grid Budget  : {view.grid_budget:>8.2f}")
    lines.append(f"  BUY Expo     : {view.buy_exposure:>8.2f}")
    lines.append(f"  SELL Expo    : {view.sell_exposure:>8.2f}")
    lines.append(f"  BUY Gv       : {view.gv_buy:>8.2f}")
    lines.append(f"  SELL Gv      : {view.gv_sell:>8.2f}")
    lines.append(f"  Stress       : {view.stress:>8.3f}")
    lines.append(f"  ADX          : {view.adx:>6.1f}  [{view.regime}]")
    lines.append(f"  Ordres ouverts: {view.open_orders:>8d}")
    lines.append(f"  Capital engagé: {view.engaged_capital:>8.2f}")
    status_symbol = "🟢" if view.health_status == "HEALTHY" else "🟡" if view.health_status == "WARNING" else "🔴"
    lines.append(f"  Health       : {status_symbol} {view.health_status}")
    lines.append("═══════════════════════════════════════════════════════════════")
    return "\n".join(lines)

def _should_log_info(new_view: CapitalView, prev_view: Optional[CapitalView]) -> bool:
    if prev_view is None:
        return True
    if prev_view.strategic_budget > 0 and abs(new_view.strategic_budget - prev_view.strategic_budget) / abs(prev_view.strategic_budget) > 0.02:
        return True
    if new_view.regime != prev_view.regime:
        return True
    return False

def enforce_eq16(P0, atr, Gul, Gll, nu, nl, stress, gub, glb, density_k, state):
    for attempt in range(EQ16_MAX_RETRIES+1):
        gsu_tmp = Gul / P0
        sell_exp_n = math.pow(nu/nu, density_k)
        sell_exp_nm1 = math.pow((nu-1)/nu, density_k)
        g_n_sell = P0 * math.pow(gsu_tmp, sell_exp_n)
        g_nm1_sell = P0 * math.pow(gsu_tmp, sell_exp_nm1)
        gap_sell = g_n_sell - g_nm1_sell
        min_gap = compute_min_gap(state)
        ok_sell = (gap_sell / g_n_sell) > min_gap if g_n_sell > 0 else False

        gsl_tmp = P0 / Gll if Gll > 0 else 1.01
        buy_exp_n = math.pow(nl/nl, density_k)
        buy_exp_nm1 = math.pow((nl-1)/nl, density_k)
        g_n_buy = P0 / math.pow(gsl_tmp, buy_exp_n)
        g_nm1_buy = P0 / math.pow(gsl_tmp, buy_exp_nm1)
        gap_buy = g_nm1_buy - g_n_buy
        ok_buy = (gap_buy / g_nm1_buy) > min_gap if g_nm1_buy > 0 else False

        if ok_sell and ok_buy:
            return Gul, Gll, nu, nl
        if attempt < EQ16_MAX_RETRIES:
            Gul = min(Gul * 1.03, P0 * (1 + GUL_HARD_MAX_PCT))
            Gll = max(Gll * 0.97, P0 * (1 - GLL_HARD_MAX_PCT))
    return Gul, Gll, nu, nl

def init_grid(price, atr, state, stress, dip, dim, adx, atr_norm_15m, force=False, reason="first_init"):
    if not force and adx > ADX_TREND_LIMIT:
        state["last_grid_init_attempt"] = time.time()
        save_state(state)
        logger.warning(f"⏸️ init_grid bloqué (ADX={adx:.1f} > {ADX_TREND_LIMIT})")
        return False

    state["last_grid_init_attempt"] = time.time()
    save_state(state)

    P0 = price

    # ---- Récupération des soldes RÉELS (API) ----
    quote_bal_real, base_bal_real = exchange.get_balances(QUOTE_ASSET, BASE_ASSET)
    base_value_real = base_bal_real * P0

    # Répartition basée sur les soldes réels
    target_nu, target_nl = adjust_levels_to_balance(quote_bal_real, base_value_real)

    # ---- Récupération du capital pour la grille (à partir du state) ----
    # On utilise le même calcul que get_balances, mais on peut aussi appeler get_balances pour obtenir capital_for_grid
    _, _, total_wallet, capital_for_grid = get_balances(P0, state)

    if total_wallet <= 0:
        logger.error("❌ Capital nul — init_grid annulée")
        return False

    # ---- Suite de l'initialisation (inchangée) ----
    gub, glb, nu, nl, regime = compute_asymmetry(dip, dim, target_nu, target_nl)
    Gul, Gll = compute_dynamic_bounds(P0, atr, stress, gub, glb)
    density_k = compute_density_k(atr_norm_15m, state)
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

    journal.log_grid_init(
        state=state,
        regime=regime,
        reason=reason,
        sell_grid_list=state["sell_grid"].to_list(),
        buy_grid_list=state["buy_grid"].to_list(),
    )

    return True

def rate_limited_get_order(symbol: str, order_id: int) -> OrderResult:
    global _last_get_order_time
    now = time.time()
    elapsed = now - _last_get_order_time
    if elapsed < MIN_GET_ORDER_INTERVAL:
        time.sleep(MIN_GET_ORDER_INTERVAL - elapsed)
    try:
        result = exchange.get_order(symbol, order_id)
        _last_get_order_time = time.time()
        return result
    except Exception as e:
        if exchange.is_rate_limit_error(e):
            logger.warning(f"⏳ Rate limit atteint, pause 5s...")
            time.sleep(5)
            return exchange.get_order(symbol, order_id)
        raise

def execute_market_fallback(side, qty_asset, target_price, state, operational_reason) -> tuple[float | None, float]:
    try:
        result = exchange.create_market_order(
            SYMBOL,
            side,
            qty_asset,
            reference_price=target_price,
        )
        if result.is_filled:
            actual_price = result.avg_price if result.avg_price > 0 else target_price
            filled_qty   = result.executed_qty
            slippage = abs(actual_price - target_price) / target_price
            state["total_slippage"] += slippage
            if side == exchange.SIDE_BUY:
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
            logger.warning(f"⚠️ Ordre market non FILLED (status={result.status})")
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

        maker_price = target_price * (1 - 0.0001) if side == exchange.SIDE_BUY else target_price * (1 + 0.0001)
        maker_price = round(maker_price, PRICE_DECIMALS)
        logger.info(f"🐢 [MAKER] Stress={current_stress:.2f} -> LIMIT @ {maker_price:.4f} qté={qty_asset:.4f}")

        init_result = exchange.create_limit_order(SYMBOL, side, qty_asset, maker_price, PRICE_DECIMALS)
        order_id = init_result.order_id

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
            if check.is_filled:
                avg_p = check.avg_price if check.avg_price > 0 else maker_price
                logger.info(f"✅ LIMIT exécuté @ {avg_p:.4f} qté={check.executed_qty:.4f}")
                state["total_trades"] += 1
                state["failed_count"] = 0
                return avg_p, check.executed_qty
            elif check.is_terminal:
                return None, 0.0

        final_order = rate_limited_get_order(SYMBOL, order_id)
        executed_qty = final_order.executed_qty
        cum_quote    = final_order.cum_quote_qty

        if not final_order.is_terminal:
            try:
                exchange.cancel_order(SYMBOL, order_id)
            except Exception:
                pass
            final_order  = rate_limited_get_order(SYMBOL, order_id)
            executed_qty = final_order.executed_qty
            cum_quote    = final_order.cum_quote_qty

        if executed_qty > 0:
            limit_avg_price = cum_quote / executed_qty
            limit_filled_qty = executed_qty
            logger.info(f"📦 Exécution partielle limit : qté={limit_filled_qty:.4f} prix={limit_avg_price:.4f}")
            slippage = abs(limit_avg_price - target_price) / target_price
            if side == exchange.SIDE_BUY:
                prev = state.get("ema_slippage_buy", 0.0)
                state["ema_slippage_buy"] = SLIPPAGE_EMA_ALPHA * slippage + (1 - SLIPPAGE_EMA_ALPHA) * prev
            else:
                prev = state.get("ema_slippage_sell", 0.0)
                state["ema_slippage_sell"] = SLIPPAGE_EMA_ALPHA * slippage + (1 - SLIPPAGE_EMA_ALPHA) * prev
            state["total_trades"] += 1
        else:
            logger.info("⚠️ Aucune exécution limitée, passage en market pour la totalité")

        remaining_qty = max(0.0, qty_asset - limit_filled_qty)

        market_price = None
        market_filled_qty = 0.0

        if remaining_qty > 0:

            remaining_notional = remaining_qty * target_price

            if MIN_NOTIONAL > 0 and remaining_notional < MIN_NOTIONAL:
                logger.info(
                    f"ℹ️ Reliquat {remaining_qty:.4f} {BASE_ASSET} "
                    f"({remaining_notional:.2f} {QUOTE_ASSET}) "
                    f"< MIN_NOTIONAL ({MIN_NOTIONAL:.2f} {QUOTE_ASSET}) "
                    "→ fallback Market ignoré, reliquat conservé dans l'inventaire."
                )

                if limit_filled_qty > 0:
                    return limit_avg_price, limit_filled_qty
                else:
                    return None, 0.0

            logger.info(
                f"🔄 Exécution market pour le solde : "
                f"{remaining_qty:.4f} {BASE_ASSET}"
            )

            market_price, market_filled_qty = execute_market_fallback(
                side,
                remaining_qty,
                target_price,
                state,
                "CAS 3 - MARKET FALLBACK (partial)"
            )

            if market_price is None:
                if limit_filled_qty > 0:
                    logger.warning(
                        "⚠️ Le fallback Market a échoué, "
                        "mais l'exécution partielle LIMIT est conservée."
                    )
                    return limit_avg_price, limit_filled_qty
                else:
                    return None, 0.0

            if limit_filled_qty > 0:
                total_qty = limit_filled_qty + market_filled_qty
                avg_price = (
                    limit_avg_price * limit_filled_qty
                    + market_price * market_filled_qty
                ) / total_qty
                return avg_price, total_qty
            else:
                return market_price, market_filled_qty

        else:
            return limit_avg_price, limit_filled_qty

    except Exception as e:
        logger.error(f"❌ Erreur Smart Router : {e}")
        state["failed_count"] += 1
        return None, 0.0

CALIBRATION_INTERVAL = 7200
THRESHOLD_ATR_CHANGE = 0.30
THRESHOLD_K_MIN_CHANGE = 0.10

def try_calibrate_params(state: dict):
    if not _CALIBRATE_AVAILABLE:
        return
    try:
        new_params, duration = run_calibration(True, exchange, SYMBOL, calibrate)

        ref_low = state.get("CALIB_REF_ATR_LOW", DENSITY_ATR_LOW)
        ref_high = state.get("CALIB_REF_ATR_HIGH", DENSITY_ATR_HIGH)
        ref_kmin = state.get("CALIB_REF_K_MIN", DENSITY_K_MIN)

        if ref_low > 0:
            delta_low = abs(new_params["atr_low"] - ref_low) / ref_low
        else:
            delta_low = 1.0 if new_params["atr_low"] > 0 else 0.0
        if ref_high > 0:
            delta_high = abs(new_params["atr_high"] - ref_high) / ref_high
        else:
            delta_high = 1.0 if new_params["atr_high"] > 0 else 0.0
        delta_kmin = abs(new_params["k_min"] - ref_kmin)

        need_rebuild = (delta_low > THRESHOLD_ATR_CHANGE or
                       delta_high > THRESHOLD_ATR_CHANGE or
                       delta_kmin > THRESHOLD_K_MIN_CHANGE)

        if need_rebuild:
            logger.info(
                f"🔧 Calibration : changements significatifs -> "
                f"ATR_LOW={new_params['atr_low']:.4f} (Δ={delta_low:.1%}), "
                f"ATR_HIGH={new_params['atr_high']:.4f} (Δ={delta_high:.1%}), "
                f"K_MIN={new_params['k_min']:.2f} (Δ={delta_kmin:.2f})"
            )
            apply_calibration_atomically(state, new_params)
            state["grid_ready"] = False
            logger.info(
                "✅ Calibration appliquée en "
                f"{duration:.3f}s ; reconstruction de grille programmée (nouveaux paramètres ATR/K)"
            )
        else:
            logger.debug(
                f"Calibration : écarts non significatifs (ATR_LOW={delta_low:.1%}, "
                f"ATR_HIGH={delta_high:.1%}, K_MIN={delta_kmin:.2f}) ; "
                f"calibration valide en {duration:.3f}s, paramètres existants conservés"
            )
    except Exception as e:
        logger.error(
            f"❌ Calibration périodique rejetée : {e}. "
            "Anciens paramètres conservés sans modification."
        )

def get_metrics_filepath(symbol: str) -> str:
    base_pattern = f"metrics_gateio_{symbol.lower()}_*.jsonl"
    existing = glob.glob(base_pattern)
    if not existing:
        ts = time.strftime("%Y%m%d_%H%M%S")
        return f"metrics_gateio_{symbol.lower()}_{ts}.jsonl"
    existing.sort()
    latest = existing[-1]
    if os.path.getsize(latest) > METRICS_MAX_SIZE:
        ts = time.strftime("%Y%m%d_%H%M%S")
        return f"metrics_gateio_{symbol.lower()}_{ts}.jsonl"
    return latest

# ============================================================
# DÉMARRAGE
# ============================================================

logger.info(f"🚀 Démarrage Moteur Quantitatif {BOT_VERSION} — Target: {SYMBOL}")

startup = StartupSequence()
startup.configuration_loaded = True
get_symbol_precisions()
startup.exchange_connected = True
state = load_state()
startup.state_loaded = True
journal = TradeJournal(SYMBOL)

# ============================================================
# CapitalTransitionGuard — premiere integration reelle (RN-022/023)
# ============================================================
# Perimetre strictement limite a --sync-capital pour cette etape.
# Le Repository opere directement sur le state dict deja charge, afin
# de ne pas introduire une deuxieme source de verite pour
# allocated_capital tant que le reste du bot (compute_capital_view,
# CapitalViewBuilder, logs, metriques) continue de lire ce champ
# directement depuis ce meme state dict.
capital_guard_repository = StateDictEconomicRepository(state, CURRENT_SYMBOL, save_state)
capital_guard_journal = CapitalTransitionJournal()
capital_guard = CapitalTransitionGuard(
    repository=capital_guard_repository,
    journal=capital_guard_journal,
)

# Calibration initiale
if _CALIBRATE_AVAILABLE and not state.get("grid_ready"):
    try:
        init_params, duration = run_calibration(True, exchange, SYMBOL, calibrate)
        apply_calibration_atomically(state, init_params)
        save_state(state)
        logger.info(
            f"✅ Calibration initiale appliquée pour {SYMBOL} en {duration:.3f}s : "
            f"ATR=[{init_params['atr_low']:.4f}, {init_params['atr_high']:.4f}], "
            f"K=[{init_params['k_min']:.2f}, {init_params['k_max']:.2f}]"
        )
    except Exception as e:
        logger.warning(
            f"❌ Calibration initiale rejetée : {e}. "
            "Paramètres existants conservés sans modification."
        )
else:
    logger.info("ℹ️ Calibration initiale désactivée ou grille déjà initialisée.")
startup.calibration_done = True

# Réconciliation des ordres ouverts et inventaire (si --reconcile)
if AUTO_RECONCILE:
    startup.reconciliation_done = reconcile_open_orders(state)
    # On réconcilie l'inventaire plus tard après avoir obtenu le prix
else:
    startup.reconciliation_done = True

# Capital initial pour le journal
_capital_for_startup = state.get("allocated_capital", 0.0) or state.get("capital_usdc", 0.0)
journal.log_startup(state=state, capital_usdc=_capital_for_startup, nb_bots=NB_BOTS)

macro_data = get_heavy_indicators()
while not macro_data:
    logger.warning("⚠️ Attente indicateurs...")
    time.sleep(10)
    macro_data = get_heavy_indicators()

price0 = get_ws_price()
if price0:
    # Réconcilier l'inventaire si demandé
    if AUTO_RECONCILE:
        reconcile_inventory(state, price0)
    # Sauvegarder après réconciliation
    save_state(state)

    # ============================================================
    # GESTION DE --sync-capital (après réconciliation)
    # ============================================================
    if SYNC_CAPITAL:
        # Récupérer les soldes réels à jour
        quote_bal, base_bal = exchange.get_balances(QUOTE_ASSET, BASE_ASSET)

        # Lecture pure du CapitalView.
        # update_peak=False est indispensable ici : nous devons connaître
        # wallet_real AVANT d'ouvrir une nouvelle période économique,
        # sans modifier le High Water Mark historique.
        capital_view = compute_capital_view(
            state,
            price0,
            quote_bal,
            base_bal,
            update_peak=False,
        )
        wallet_real = capital_view["wallet_real"]

        # Calculer unrealized_pnl à partir du FIFO existant
        unrealized_pnl = inv_mgr.inventory_unrealized_pnl(state, price0)
        total_pnl = state.get("total_pnl", 0.0)

        # Nouveau capital alloué
        new_allocated = wallet_real - total_pnl - unrealized_pnl
        if new_allocated > 0:
            old_allocated = state.get("allocated_capital", 0.0)

            sync_request = build_manual_sync_request(
                bot_id=CURRENT_SYMBOL,
                old_allocated=old_allocated,
                new_allocated=new_allocated,
                justification="--sync-capital : recalcul depuis le wallet reel",
            )
            sync_result = capital_guard.submit_transition(sync_request)

            if sync_result.status is TransitionStatus.ACCEPTED:
                # StateDictEconomicRepository.save() a deja mis a jour
                # state["allocated_capital"] et persiste le state.
                journal.log_capital_sync(old_allocated, state["allocated_capital"], wallet_real, "sync_capital")
                logger.info(f"🔄 Capital synchronisé : {old_allocated:.2f} → {state['allocated_capital']:.2f} (wallet réel={wallet_real:.2f})")

                # Réinitialisation du peak économique (ouvre une nouvelle période)
                # La sauvegarde est faite à l'intérieur de reset_economic_period
                reset_economic_period(state, wallet_real, "sync_capital")
            else:
                logger.warning(f"⚠️ Synchronisation refusée par le CapitalTransitionGuard : {sync_result.reason}")
        else:
            logger.warning(f"⚠️ Nouveau capital calculé <= 0 ({new_allocated:.2f}), garde l'ancien.")
else:
    total_wallet0 = 0.0
    logger.warning("⚠️ Impossible de réconcilier l'inventaire au démarrage (prix manquant)")

# ============================================================
# ATTENTE PASSIVE SI allocated_capital == 0 (RN-019D)
# ============================================================
if state.get("allocated_capital", 0.0) <= 0:
    logger.warning("⏳ Aucun budget stratégique (allocated_capital) disponible.")
    logger.warning("En attente d'une consigne... Le bot reste opérationnel (WS, state, logs) mais ne trade pas.")
    logger.warning("Pour fournir un budget, utilisez --budget, --allocated-capital, ou --sync-capital au prochain redémarrage.")

    _wait_start_time = time.time()
    last_capital_check = _wait_start_time
    while not _shutdown_requested:
        now = time.time()
        if now - last_capital_check >= CAPITAL_CHECK_INTERVAL:
            # Recharger le state pour détecter une mise à jour externe (MetaController)
            try:
                fresh_state = STATE_STORE.read()
                if fresh_state is not None:
                    new_capital = fresh_state.get("allocated_capital", 0.0)
                    if new_capital > 0:
                        # Mettre à jour le state courant
                        state.update(fresh_state)
                        wait_duration = now - _wait_start_time
                        logger.info(f"💰 Budget stratégique reçu : {new_capital:.2f} {QUOTE_ASSET}")
                        logger.info(f"⏳ Temps d'attente total : {wait_duration:.1f}s")
                        logger.info("🚀 Activation du moteur de trading. Construction de la grille immédiate.")

                        # On repart d'un chrono propre pour l'initialisation : le temps passé en
                        # WAITING ne doit pas être comptabilisé comme du "temps d'initialisation".
                        state["last_grid_init_attempt"] = now
                        startup = StartupSequence()
                        startup.configuration_loaded  = True
                        startup.exchange_connected     = True
                        startup.state_loaded           = True
                        startup.calibration_done        = True
                        startup.reconciliation_done     = True

                        # Forcer la réinitialisation de la grille dès la prochaine itération
                        state["grid_ready"] = False
                        save_state(state)
                        break  # Sort de la boucle d'attente
                    else:
                        # Toujours en attente : log toutes les 60 secondes
                        if int(now) % 60 == 0:
                            logger.debug(f"⏳ Toujours en attente de budget (allocated_capital={new_capital})")
                else:
                    logger.warning("⚠️ Impossible de lire le state pendant l'attente")
            except Exception as e:
                logger.error(f"❌ Erreur lors de la lecture du state en attente : {e}")

            last_capital_check = now

        # Pause courte pour ne pas saturer le CPU
        time.sleep(1)

    # Si on est sorti de la boucle d'attente à cause d'un shutdown, on arrête proprement
    if _shutdown_requested:
        logger.info("🛑 Arrêt demandé pendant l'attente.")
        stop_price_websocket()
        save_state(state)
        remove_lock(SYMBOL)
        sys.exit(0)

    logger.info("🚀 Budget stratégique validé, activation du moteur de trading.")
# ---- Fin de l'attente passive ----

last_macro_time       = time.time()
last_log_time         = time.time()
last_info_log         = time.time()
last_lock_update      = time.time()
last_ws_check         = time.time()
last_startup_ws_attempt = 0.0
last_metrics_time     = time.time()
stress                = 0.20
with _ws_retry_lock:
    ws_retry_count = 0

failed_consecutive = 0
last_exposure_state = None

_prev_capital_view = None
_force_log_event = True

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

        if _startup_ready and time.time() - last_ws_check >= WS_CHECK_INTERVAL:
            last_ws_check = time.time()
            with ws_price_lock:
                age = time.time() - ws_price_time

            if not ws_running:
                with _ws_reconnect_lock:
                    reconnect_pending = _ws_reconnect_timer is not None
                if not reconnect_pending:
                    logger.warning("⚠️ WS fermé sans reconnexion planifiée — déclenchement forcé")
                    _schedule_ws_reconnect(SYMBOL)
            elif ws_price is not None and age > WS_FORCE_RESTART_AGE:
                logger.warning(f"⚠️ Prix WS périmé depuis {age:.1f}s > {WS_FORCE_RESTART_AGE}s — redémarrage WebSocket forcé")
                with _ws_retry_lock:
                    ws_retry_count = 0
                stop_price_websocket()

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

        # Calibration périodique
        if _CALIBRATE_AVAILABLE and (time.time() - state.get("last_calibration_time", 0) >= CALIBRATION_INTERVAL):
            try_calibrate_params(state)
            state["last_calibration_time"] = time.time()
            save_state(state)

        quote_bal, base_bal, total_wallet, capital_for_grid = get_balances(price, state, CURRENT_SYMBOL)

        if total_wallet <= 0:
            failed_consecutive += 1
            time.sleep(LOOP_SLEEP)
            continue

        capital_view = compute_capital_view(state, price, quote_bal, base_bal, update_peak=True)

        if state["wallet_peak"] == 0.0 or total_wallet > state["wallet_peak"]:
            state["wallet_peak"] = total_wallet
            save_state(state)

        drawdown_dd = max(0.0, 1.0 - total_wallet / state["wallet_peak"])

        if drawdown_dd > DRAWDOWN_WARNING_THRESHOLD:
            logger.warning(
                f"⚠️ Drawdown important : {drawdown_dd:.1%} (peak={state['wallet_peak']:.2f}, capital={total_wallet:.2f})"
            )

        inventory_qty = capital_view["inventory_qty"]
        unrealized_pnl = capital_view["unrealized_pnl"]
        pnl_pct = capital_view["pnl_pct"]

        if drawdown_dd >= GLOBAL_STOP_LOSS_DD:
            logger.critical(f"🚨 STOP-LOSS (drawdown) : DD={drawdown_dd*100:.2f}% ≥ {GLOBAL_STOP_LOSS_DD*100:.0f}%")
            journal.log_stop_loss(
                reason="drawdown",
                drawdown=drawdown_dd,
                pnl_pct=pnl_pct,
                total_pnl=state.get("total_pnl", 0.0),
                total_wallet=total_wallet,
                wallet_peak=state["wallet_peak"],
            )
            break
        if pnl_pct < GLOBAL_STOP_LOSS_PNL:
            logger.critical(f"🚨 STOP-LOSS (PnL total) : PnL={pnl_pct*100:.2f}% < {GLOBAL_STOP_LOSS_PNL*100:.0f}%")
            journal.log_stop_loss(
                reason="drawdown",
                drawdown=drawdown_dd,
                pnl_pct=pnl_pct,
                total_pnl=state.get("total_pnl", 0.0),
                total_wallet=total_wallet,
                wallet_peak=state["wallet_peak"],
            )
            break

        slip_avg = max(state.get("ema_slippage_buy", 0.0), state.get("ema_slippage_sell", 0.0))

        stress = compute_stress(
            macro_data["adx"],
            macro_data["atr_norm"],
            macro_data["atr_norm_15m"],
            drawdown_dd,
            slip_avg
        )

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
        grid_empty    = state["grid_ready"] and (len(state["sell_grid"])==0 and len(state["buy_grid"])==0)
        must_init = not state["grid_ready"] or out_of_bounds or grid_empty

        force_init = False
        if must_init and not state["grid_ready"]:
            last_attempt = state.get("last_grid_init_attempt", 0.0)
            if (time.time() - last_attempt > FORCE_INIT_TIMEOUT) or AUTO_RECONCILE or SYNC_CAPITAL:
                logger.warning(f"⏰ Timeout d'initialisation dépassé ({FORCE_INIT_TIMEOUT}s) — forcing init_grid même si ADX élevé")
                force_init = True

        if must_init:
            if not state["grid_ready"]:
                init_reason = "first_init"
            elif out_of_bounds:
                init_reason = "out_of_bounds"
            elif grid_empty:
                init_reason = "grid_empty"
            else:
                init_reason = "calibration"

            rebuilt = init_grid(price, macro_data["atr"], state, stress,
                      macro_data["dip"], macro_data["dim"],
                      macro_data["adx"], macro_data.get("atr_norm_15m",0.015),
                      force=force_init, reason=init_reason)
            if rebuilt:
                state["last_grid_rebuild_ts"] = time.time()
                state["last_rebuild_price"] = price
                save_state(state)
                _force_log_event = True
            quote_bal, base_bal, total_wallet, capital_for_grid = get_balances(price, state, CURRENT_SYMBOL)

        if not state["grid_ready"]:
            time.sleep(LOOP_SLEEP)
            continue

        if not startup.grid_initialized:
            startup.grid_initialized = True
            logger.info("✅ Grille initialisée — démarrage de la connexion WebSocket")

        if not ws_running and not ws_connecting and time.time() - last_startup_ws_attempt >= 5.0:
            last_startup_ws_attempt = time.time()
            start_price_websocket(SYMBOL)

        if ws_running and not startup.capital_target_active and startup.reconciliation_done:
            # Étape 9 (suppression de l'intégration CapitalTargetController) :
            # ces deux flags conditionnent startup.ready ; ils sont conservés
            # avec le même déclencheur (WS up + réconciliation faite) afin de
            # ne pas modifier le comportement de démarrage du bot, bien que
            # CapitalTargetController n'existe plus.
            startup.websocket_connected = True
            startup.capital_target_active = True
            _startup_ready = startup.ready
            if _startup_ready:
                logger.info(startup.ready_report())

        if not _startup_ready:
            time.sleep(LOOP_SLEEP)
            continue

        sell_grid = state["sell_grid"]
        buy_grid = state["buy_grid"]

        # TRAITEMENT BUY
        while len(buy_grid) > 0 and price <= buy_grid[0]:
            # Étape 8 (désactivation fonctionnelle de CapitalTargetController) :
            # capital_ratio n'est plus multiplié ici (cf. audit étape 7 et 8).
            capital_effectif = capital_for_grid * ACTIVE_CAPITAL_RATIO
            capital_effectif *= buy_exposure_factor
            Gv_local = compute_gv(capital_effectif, state["P0"], state["Gul"], state["Gll"],
                                  state["nu"], state["nl"], state["density_k"])
            state["Gv"] = Gv_local
            if quote_bal >= Gv_local:
                touched = buy_grid.pop(0)
                actual_buy_price, filled_qty = smart_execute_order(exchange.SIDE_BUY, Gv_local, price, state, stress, macro_data)
                if actual_buy_price is not None and filled_qty > 0:

                    inv_mgr.add_buy_lot(
                        state,
                        qty=filled_qty,
                        price=actual_buy_price,
                        source="trade",
                    )

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
                        new_sell_raw = actual_buy_price * (1 + compute_min_gap(state))

                    min_gap = compute_min_gap(state)
                    min_sell = actual_buy_price / (1 - min_gap)
                    new_sell_raw = max(new_sell_raw, min_sell)
                    new_sell = round(new_sell_raw, PRICE_DECIMALS)

                    sell_grid.add(new_sell)
                    state["sell_grid"] = sell_grid
                    state["buy_grid"] = buy_grid
                    save_state(state)
                    logger.info(f"⚡ ACHAT @ {actual_buy_price:.4f} | Qté={filled_qty:.4f} | Lots={len(state['inventory_lots'])}")
                    journal.log_buy(
                        grid_level=touched,
                        trigger_price=price,
                        exec_price=actual_buy_price,
                        qty_base=filled_qty,
                        new_sell_level=new_sell,
                        state=state,
                        stress=stress,
                        adx=macro_data["adx"],
                    )
                    exchange.invalidate_balance_cache()
                    quote_bal = max(0.0, quote_bal - (filled_qty * actual_buy_price))
                    base_bal += filled_qty
                else:
                    buy_grid.add(touched)
                    break
            else:
                break

        # TRAITEMENT SELL
        while len(sell_grid) > 0 and price >= sell_grid[0]:
            # Étape 8 (désactivation fonctionnelle de CapitalTargetController) :
            # capital_ratio n'est plus multiplié ici (cf. audit étape 7 et 8).
            capital_effectif = capital_for_grid * ACTIVE_CAPITAL_RATIO
            capital_effectif *= sell_exposure_factor
            Gv_local = compute_gv(capital_effectif, state["P0"], state["Gul"], state["Gll"],
                                  state["nu"], state["nl"], state["density_k"])
            state["Gv"] = Gv_local

            qty_asset = round(Gv_local / price, QTY_DECIMALS)

            if inv_mgr.inventory_qty(state) >= qty_asset:

                touched = sell_grid.pop(0)
                actual_sell_price, filled_qty = smart_execute_order(exchange.SIDE_SELL, Gv_local, price, state, stress, macro_data)

                if actual_sell_price is not None and filled_qty > 0:

                    lots_consumed = inv_mgr.consume_fifo(state, filled_qty)

                    consumed_qty = sum(lot["qty"] for lot in lots_consumed)
                    if abs(consumed_qty - filled_qty) > 1e-9:
                        raise RuntimeError(
                            f"Invariant FIFO violé : vendu={filled_qty:.6f}, consommé={consumed_qty:.6f}"
                        )

                    fee_buy = TRADING_FEE_RT + state.get("ema_slippage_buy", 0.0)
                    fee_sell = TRADING_FEE_RT + state.get("ema_slippage_sell", 0.0)

                    pnl_trade = 0.0

                    for lot in lots_consumed:
                        qty_sold = lot["qty"]
                        pnl_trade += (actual_sell_price - lot["buy_price"]) * qty_sold
                        pnl_trade -= (
                            actual_sell_price * qty_sold * fee_sell
                            + lot["buy_price"] * qty_sold * fee_buy
                        )

                    state["total_pnl"] = state.get("total_pnl", 0.0) + pnl_trade

                    logger.info(
                        f"💰 PnL trade (net de frais) : {pnl_trade:+.4f} {QUOTE_ASSET} "
                        f"(vente @ {actual_sell_price:.4f}) | "
                        f"Cumulé={state['total_pnl']:.4f}"
                    )

                    # ============================================================
                    # CapitalTransitionGuard — profit / perte realises (RN-022/023)
                    # ============================================================
                    # Le montant transmis est exactement pnl_trade, deja calcule
                    # ci-dessus, sans aucune transformation. pnl_trade == 0 (trade
                    # strictement neutre) ne declenche aucune des deux branches :
                    # aucune transition economique n'est necessaire dans ce cas.
                    if pnl_trade > 0:
                        profit_request = build_realized_profit_request(
                            bot_id=CURRENT_SYMBOL,
                            amount=pnl_trade,
                            justification="Vente exécutée : profit réalisé (FIFO)",
                        )
                        profit_result = capital_guard.submit_transition(profit_request)
                        if profit_result.status is TransitionStatus.ACCEPTED:
                            logger.info(
                                f"📈 Profit réalisé appliqué au capital alloué : "
                                f"+{pnl_trade:.4f} → allocated_capital={state['allocated_capital']:.2f}"
                            )
                        else:
                            logger.warning(
                                f"⚠️ Profit réalisé non appliqué par le CapitalTransitionGuard : "
                                f"{profit_result.reason}"
                            )
                    elif pnl_trade < 0:
                        loss_request = build_realized_loss_request(
                            bot_id=CURRENT_SYMBOL,
                            amount=pnl_trade,
                            justification="Vente exécutée : perte réalisée (FIFO)",
                        )
                        loss_result = capital_guard.submit_transition(loss_request)
                        if loss_result.status is TransitionStatus.ACCEPTED:
                            logger.info(
                                f"📉 Perte réalisée appliquée au capital alloué : "
                                f"{pnl_trade:.4f} → allocated_capital={state['allocated_capital']:.2f}"
                            )
                        else:
                            logger.warning(
                                f"⚠️ Perte réalisée non appliquée par le CapitalTransitionGuard : "
                                f"{loss_result.reason}"
                            )

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
                        new_buy_raw = actual_sell_price * (1 - compute_min_gap(state))

                    min_gap = compute_min_gap(state)
                    max_buy = actual_sell_price * (1 - min_gap)
                    new_buy_raw = min(new_buy_raw, max_buy)
                    new_buy = round(new_buy_raw, PRICE_DECIMALS)

                    buy_grid.add(new_buy)
                    state["sell_grid"] = sell_grid
                    state["buy_grid"] = buy_grid
                    save_state(state)
                    logger.info(f"⚡ VENTE @ {actual_sell_price:.4f} | Qté vendue={filled_qty:.4f} | Lots restants={len(state['inventory_lots'])}")
                    exchange.invalidate_balance_cache()
                    proceeds = filled_qty * actual_sell_price
                    quote_bal += proceeds
                    base_bal = max(0.0, base_bal - filled_qty)
                else:
                    sell_grid.add(touched)
                    break
            else:
                break

        # LOG PÉRIODIQUE
        if time.time() - last_log_time >= 10.0:
            last_log_time = time.time()
            save_state(state)

            capital_view = compute_capital_view(state, price, quote_bal, base_bal, update_peak=False)
            inventory_qty = capital_view["inventory_qty"]
            nb_lots = len(state.get("inventory_lots", []))
            avg_cost = 0.0
            unrealized_pnl = capital_view["unrealized_pnl"]
            if inventory_qty > 0:
                total_cost = capital_view["inventory_cost"]
                avg_cost = total_cost / inventory_qty

            gv_display = state.get("Gv", 0.0)
            pnl_pct = capital_view["pnl_pct"]

            logger.debug(
                f"📊 {price:.4f} | Wallet réel={capital_view['wallet_real']:.2f} | Alloué={capital_view['allocated_capital']:.2f} | Alpha={capital_view['alpha']:+.2f} | "
                f"Grid Budget={capital_view['capital_for_grid']:.2f} | Stress={stress:.2f} | "
                f"BUY={len(buy_grid)} SELL={len(sell_grid)} | Gv={gv_display:.2f} | k={state['density_k']:.2f} | "
                f"Trades={state['total_trades']} | PnL réalisé={state.get('total_pnl',0.0):.4f} | UPnL={unrealized_pnl:+.4f} | "
                f"PnL total={pnl_pct*100:+.2f}% | Stock={inventory_qty:.4f} (moy={avg_cost:.4f}) | "
                f"Lots={nb_lots} | EMA_B={state.get('ema_slippage_buy',0.0):.4%} EMA_S={state.get('ema_slippage_sell',0.0):.4%}"
            )

        # CAPITALVIEW
        if LOG_LEVEL <= logging.DEBUG or _force_log_event or (time.time() - last_info_log >= 60):
            capital_view_aggregates = compute_capital_view(state, price, quote_bal, base_bal, update_peak=False)

            view = CapitalViewBuilder.build(
                symbol=SYMBOL,
                state=state,
                price=price,
                capital_view_aggregates=capital_view_aggregates,
                macro_data=macro_data,
                stress=stress,
                buy_exposure=buy_exposure_factor,
                sell_exposure=sell_exposure_factor,
                regime=regime,
                grid_sell_len=len(sell_grid),
                grid_buy_len=len(buy_grid),
            )

            if history_logger:
                history_logger.log_capital_view(view, CURRENT_SYMBOL)

            if LOG_LEVEL <= logging.INFO:
                if _should_log_info(view, _prev_capital_view) or _force_log_event:
                    logger.info("\n" + format_capital_view(view))
                    _prev_capital_view = view
                    _force_log_event = False

            if LOG_LEVEL <= logging.DEBUG:
                logger.debug(f"CapitalView (debug): {view}")

            last_info_log = time.time()

        # MÉTRIQUES
        if time.time() - last_metrics_time >= METRICS_INTERVAL:
            last_metrics_time = time.time()

            entry = {
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "symbol": SYMBOL,
                "price": price,
                "wallet_real": capital_view_aggregates["wallet_real"],
                "allocated_capital": capital_view_aggregates["allocated_capital"],
                "alpha": capital_view_aggregates["alpha"],
                "capital_for_grid": capital_view_aggregates["capital_for_grid"],
                "Gv": state.get("Gv", 0.0),
                "stress": stress,
                "adx": macro_data.get("adx", 0.0),
                "trend_ratio": dip / max(dim, 0.001),
                "buy_exposure_factor": buy_exposure_factor,
                "sell_exposure_factor": sell_exposure_factor,
                "drawdown_dd": drawdown_dd,
                "total_wallet": total_wallet,
                "quote_bal": quote_bal,
                "inventory_qty": capital_view_aggregates.get("inventory_qty", 0.0),
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": state.get("total_pnl", 0.0),
                "pnl_pct": pnl_pct,
                "total_trades": state.get("total_trades", 0),
            }

            metrics_file = get_metrics_filepath(SYMBOL)
            with open(metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        time.sleep(LOOP_SLEEP)

    except Exception as e:
        logger.error(f"❌ Erreur boucle : {e}")
        failed_consecutive += 1
        time.sleep(5)

stop_price_websocket()
logger.info("🛑 Arrêt propre — sauvegarde finale...")
save_state(state)
remove_lock(SYMBOL)
logger.info("✅ Bot arrêté.")
