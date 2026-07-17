BOT_VERSION = "V103"

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
from exchange_base import OrderResult
from exchange_binance import ExchangeBinance

exchange = ExchangeBinance()

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



# ── Import du calibrateur ATR/K ────────────────────────────────
try:
    from script_atr import calibrate
    _CALIBRATE_AVAILABLE = True
except ImportError:
    calibrate = None
    _CALIBRATE_AVAILABLE = False
    print("⚠️  'script_atr' non trouvé. Calibration périodique désactivée (paramètres fixes).")


# ── PARSING ─────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grid Trading Bot — Moteur Quantitatif V101f (grille flexible + calibration dynamique)",
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
                        help="Réconcilier l'inventaire UNIQUEMENT au démarrage (manuel)")
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
    ns.log_level       = raw.log_level
    return ns

args = parse_args()

SYMBOL           = args.symbol
MAX_BUDGET_USDC  = args.max_budget_usdc
NB_BOTS          = args.bots
AUTO_RECONCILE   = args.reconcile
LOG_LEVEL        = getattr(logging, args.log_level.upper())

# ── Résolution de la paire ────────────────────────────────────
if SYMBOL.endswith("USDC"):
    BASE_ASSET = SYMBOL[:-4]
    QUOTE_ASSET = "USDC"
elif SYMBOL.endswith("USDT"):
    BASE_ASSET = SYMBOL[:-4]
    QUOTE_ASSET = "USDT"
else:
    print(f"❌ Paire {SYMBOL} non supportée.")
    sys.exit(1)


# ── Paramètres par défaut (seront écrasés par calibration) ────
# Ces constantes servent de repli si script_atr.py est absent.
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
    DENSITY_ATR_LOW  = 0.0041
    DENSITY_ATR_HIGH = 0.0078
    DENSITY_K_MIN    = 0.50
    DENSITY_K_MAX    = 1.00

else:
    DENSITY_ATR_LOW  = 0.004
    DENSITY_ATR_HIGH = 0.008
    DENSITY_K_MIN    = 0.50
    DENSITY_K_MAX    = 1.00

# Paramètres généraux conservés
NU_MIN = 2
NU_MAX = 8
NL_MIN = 2
NL_MAX = 8

KLINE_INTERVAL = exchange.KLINE_3M
KLINE_LIMIT = 100

SLIPPAGE_EMA_ALPHA = 0.20
STRESS_LIMIT_FOR_MAKER = 0.30


# ── Constantes ─────────────────────────────────────────────────
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

TRADING_FEE_RT    = 0.00075
EQ16_MIN_RATIO    = 2.0
EQ16_MAX_RETRIES  = 3

ADX_MAKER_LIMIT        = 40
LIMIT_TIMEOUT_SECONDS  = 15

MIN_ORDER_USDC  = 5.5
KLINE_LIMIT     = 50
LOOP_SLEEP      = 0.2
INDICATORS_FREQ = 60
ADX_TREND_LIMIT = 50
STATE_FILE      = f"state_{SYMBOL.lower()}.json"
JOURNAL_FILE    = f"journal_{SYMBOL.lower()}.jsonl"
SNAPSHOT_FILE   = "snapshot_t0.json"
SNAPSHOT_META_KEYS = {"date_reference", "timestamp_reference", "CASH"}
FAILED_COOLDOWN_INITIAL = 3
FAILED_COOLDOWN_MAX     = 60
GLOBAL_STOP_LOSS_DD = 0.25
GLOBAL_STOP_LOSS_PNL = -0.10
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

# ── Journal des transactions ──────────────────────────────────
class TradeJournal:
    """
    Journal JSONL des transactions du bot de grille.

    Chaque ligne est un objet JSON autonome (append-only).
    Thread-safe (verrou d'écriture).
    Compatible jq, pandas, et tout outil de parsing JSON lines.

    Événements : STARTUP | GRID_INIT | BUY | SELL | STOP_LOSS
    Fichier    : journal_{symbol}.jsonl  (ex: journal_injusdc.jsonl)
    """

    def __init__(self, symbol: str):
        self.path = JOURNAL_FILE
        self._lock = threading.Lock()
        logger.info(f"📒 Journal des transactions : {self.path}")

    # ── Écriture atomique ─────────────────────────────────────
    def _write(self, entry: dict):
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"❌ Erreur écriture journal : {e}")

    # ── Champs communs ────────────────────────────────────────
    def _base(self, event: str) -> dict:
        now = time.time()
        ms = int((now % 1) * 1000)
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{ms:03d}Z"
        return {"ts": round(now, 3), "ts_iso": ts_iso, "event": event, "symbol": SYMBOL}

    # ── Contexte grille réutilisable ──────────────────────────
    def _grid_ctx(self, state: dict, stress: float, adx: float) -> dict:
        return {
            "P0":           round(state.get("P0", 0.0),        PRICE_DECIMALS),
            "Gv":           round(state.get("Gv", 0.0),        2),
            "density_k":    round(state.get("density_k", 0.0), 3),
            "stress":       round(stress,                       3),
            "adx":          round(adx,                          1),
            "total_trades": state.get("total_trades", 0),
        }

    # ── STARTUP ───────────────────────────────────────────────
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

    # ── GRID_INIT ─────────────────────────────────────────────
    def log_grid_init(self, *, state: dict, regime: str, reason: str,
                      sell_grid_list: list, buy_grid_list: list):
        """reason : first_init | out_of_bounds | grid_empty | calibration"""
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

    # ── BUY ───────────────────────────────────────────────────
    def log_buy(self, *, grid_level: float, trigger_price: float,
                exec_price: float, qty_base: float, new_sell_level: float,
                state: dict, stress: float, adx: float):
        """
        grid_level    : niveau grille consommé (buy_grid[0] avant pop)
        trigger_price : prix marché au moment du déclenchement
        exec_price    : prix réel d'exécution
        """
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

    # ── SELL ──────────────────────────────────────────────────
    def log_sell(self, *, grid_level: float, trigger_price: float,
                 exec_price: float, qty_base: float, pnl_trade: float,
                 lots_consumed: list, new_buy_level: float,
                 state: dict, stress: float, adx: float):
        """
        lots_consumed : liste de dicts {"qty": float, "buy_price": float}
                        capturés AVANT mise à jour de state["inventory_lots"]
        """
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

    # ── STOP_LOSS ─────────────────────────────────────────────
    def log_stop_loss(self, *, reason: str, drawdown: float, pnl_pct: float,
                      total_pnl: float, total_wallet: float, wallet_peak: float):
        """reason : drawdown | pnl_pct"""
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

def get_snapshot_bot_count() -> int:
    if not os.path.exists(SNAPSHOT_FILE):
        return 1
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snap = json.load(f)
        tokens = [k for k in snap if k not in SNAPSHOT_META_KEYS]
        return max(1, len(tokens))
    except Exception as e:
        logger.warning(f"⚠️ Impossible de lire {SNAPSHOT_FILE} pour compter les bots : {e}")
        return 1

if NB_BOTS is None:
    if not create_lock(SYMBOL):
        logger.error("❌ Échec de création du lock, arrêt.")
        sys.exit(1)
    NB_BOTS = get_snapshot_bot_count()
    logger.info(f"🔍 Répartition cash T0 : {NB_BOTS} bot(s) d'après {SNAPSHOT_FILE}")
else:
    create_lock(SYMBOL)
    logger.info(f"🤖 Partage forcé : {NB_BOTS} bot(s) (argument --bots)")


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
    return exchange.get_ticker_price(SYMBOL)

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


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _inventory_cost(state: dict) -> float:
    total = 0.0
    for lot in state.get("inventory_lots", []):
        total += _num(lot.get("qty")) * _num(lot.get("buy_price"))
    return total

def _cash_from_snapshot(cash: dict) -> float:
    if not isinstance(cash, dict):
        return 0.0
    for key in (QUOTE_ASSET, QUOTE_ASSET.lower(), QUOTE_ASSET.upper()):
        if key in cash:
            return _num(cash.get(key))
    return 0.0

def _snapshot_capital_usdc(state: dict, price: float) -> float | None:
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snap = json.load(f)
        tokens = [k for k in snap if k not in SNAPSHOT_META_KEYS]
        if BASE_ASSET not in snap or not tokens:
            return None

        token = snap.get(BASE_ASSET, {})
        base_qty = _num(token.get("stock"))
        cash_share = _cash_from_snapshot(snap.get("CASH", {})) / max(1, len(tokens))

        ref_price = _num(state.get("P0")) or price
        for key in ("price", "prix", "price_usdc", "usdc_price", "lastPrice"):
            if key in token and _num(token.get(key)) > 0:
                ref_price = _num(token.get(key))
                break

        if base_qty > 0 and not state.get("inventory_lots") and _num(state.get("total_base_qty")) <= 0:
            state["inventory_lots"] = [{
                "qty": base_qty,
                "buy_price": ref_price,
                "timestamp": time.time(),
                "reconciled": True,
                "source": "snapshot_t0",
            }]
            state["total_base_qty"] = base_qty
            logger.info(f"📦 Inventaire initialisé depuis {SNAPSHOT_FILE} : {base_qty:.6f} {BASE_ASSET}")

        return base_qty * ref_price + cash_share
    except Exception as e:
        logger.warning(f"⚠️ Lecture {SNAPSHOT_FILE} impossible pour capital_usdc : {e}")
        return None

def ensure_capital_usdc(state: dict, price: float, quote_bal_real: float, base_bal_real: float) -> float:
    capital_usdc = _num(state.get("capital_usdc"))
    if capital_usdc > 0:
        return capital_usdc

    snapshot_capital = _snapshot_capital_usdc(state, price)
    if snapshot_capital and snapshot_capital > 0:
        state["capital_usdc"] = snapshot_capital
        logger.info(f"💰 capital_usdc initialisé depuis {SNAPSHOT_FILE} : {snapshot_capital:.2f} {QUOTE_ASSET}")
        return snapshot_capital

    if MAX_BUDGET_USDC is not None and MAX_BUDGET_USDC > 0:
        state["capital_usdc"] = MAX_BUDGET_USDC
        logger.info(f"💰 capital_usdc initialisé depuis --budget : {MAX_BUDGET_USDC:.2f} {QUOTE_ASSET}")
        return MAX_BUDGET_USDC

    inventory_cost = _inventory_cost(state)
    if inventory_cost <= 0 and base_bal_real > 0 and price > 0:
        inventory_cost = base_bal_real * price
    fallback_capital = max(0.0, inventory_cost + quote_bal_real)
    state["capital_usdc"] = fallback_capital
    logger.warning(
        f"⚠️ capital_usdc absent : fallback compatibilité = {fallback_capital:.2f} {QUOTE_ASSET}"
    )
    return fallback_capital

def compute_capital_view(state: dict, price: float,
                         quote_bal_real: float | None = None,
                         base_bal_real: float | None = None) -> dict:
    if quote_bal_real is None or base_bal_real is None:
        quote_bal_real, base_bal_real = exchange.get_balances(
            QUOTE_ASSET,
            BASE_ASSET,
        )

    capital_usdc = ensure_capital_usdc(state, price, quote_bal_real, base_bal_real)
    inventory_qty = _num(state.get("total_base_qty"))
    if inventory_qty <= 0:
        inventory_qty = sum(_num(lot.get("qty")) for lot in state.get("inventory_lots", []))

    inventory_cost = _inventory_cost(state)
    if inventory_qty > 0 and inventory_cost <= 0:
        inventory_cost = inventory_qty * (_num(state.get("P0")) or price)

    inventory_value = inventory_qty * price
    unrealized_pnl = inventory_value - inventory_cost
    total_pnl = _num(state.get("total_pnl"))
    total_wallet = capital_usdc + total_pnl + unrealized_pnl
    capital_for_grid = max(0.0, min(total_wallet, capital_usdc))
    quote_virtual_usdc = capital_usdc + total_pnl - inventory_cost
    quote_available = max(0.0, min(quote_virtual_usdc, quote_bal_real))
    wallet_peak = _num(state.get("wallet_peak"))
    drawdown = max(0.0, 1.0 - total_wallet / wallet_peak) if wallet_peak > 0 else 0.0
    pnl_pct = (total_wallet - capital_usdc) / capital_usdc if capital_usdc > 0 else 0.0

    return {
        "capital_usdc": capital_usdc,
        "inventory_qty": inventory_qty,
        "inventory_cost": inventory_cost,
        "inventory_value": inventory_value,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "total_wallet": total_wallet,
        "capital_for_grid": capital_for_grid,
        "quote_virtual_usdc": quote_virtual_usdc,
        "quote_available": quote_available,
        "base_available": inventory_qty,
        "drawdown": drawdown,
        "pnl_pct": pnl_pct,
    }

def get_balances(price: float, state: dict | None = None) -> tuple[float, float, float, float]:
    global _capital_initial
    
    quote_bal_real, base_bal_real = exchange.get_balances(
        QUOTE_ASSET,
        BASE_ASSET,
    )
    if state is None:
        quote_bal_virt = quote_bal_real
        total_wallet = quote_bal_real + base_bal_real * price
        capital_for_grid = min(total_wallet, MAX_BUDGET_USDC) if MAX_BUDGET_USDC else total_wallet
        if _capital_initial is None and total_wallet > 0:
            _capital_initial = total_wallet
            logger.info(f"💰 Capital initial : {_capital_initial:.2f} {QUOTE_ASSET}")
        return quote_bal_virt, base_bal_real, total_wallet, capital_for_grid

    view = compute_capital_view(state, price, quote_bal_real, base_bal_real)
    
    if _capital_initial is None and view["capital_usdc"] > 0:
        _capital_initial = view["capital_usdc"]
        logger.info(f"💰 Capital initial : {_capital_initial:.2f} {QUOTE_ASSET}")

    return view["quote_available"], view["base_available"], view["total_wallet"], view["capital_for_grid"]

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
# load_state avec conversion SortedGrid + paramètres dynamiques
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
        # Paramètres dynamiques ATR/K
        "DENSITY_ATR_LOW": DENSITY_ATR_LOW,
        "DENSITY_ATR_HIGH": DENSITY_ATR_HIGH,
        "DENSITY_K_MIN": DENSITY_K_MIN,
        "DENSITY_K_MAX": DENSITY_K_MAX,
        # Référence de calibration (pour détecter les changements)
        "CALIB_REF_ATR_LOW": DENSITY_ATR_LOW,
        "CALIB_REF_ATR_HIGH": DENSITY_ATR_HIGH,
        "CALIB_REF_K_MIN": DENSITY_K_MIN,
        "last_calibration_time": 0.0,
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
    exchange.invalidate_balance_cache()
    __, real_base_bal = exchange.get_balances(
        QUOTE_ASSET,
        BASE_ASSET,
    )
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
        open_orders = exchange.get_open_orders(SYMBOL)
        if not open_orders:
            logger.info("✅ Aucun ordre ouvert trouvé sur Binance.")
            return

        logger.info(f"🔍 {len(open_orders)} ordre(s) ouvert(s) détecté(s) sur Binance.")
        for order in open_orders:
            order_id     = order["order_id"]
            side         = order["side"]
            orig_qty     = order["orig_qty"]
            executed_qty = order["executed_qty"]
            price        = order["price"]
            status       = order["status"]
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
                    exchange.cancel_order(SYMBOL, order_id)
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

# ═══════════════════════════════════════════════════════════════
# INITIALISATION DE LA GRILLE
# ═══════════════════════════════════════════════════════════════

def init_grid(price, atr, state, stress, dip, dim, adx, atr_norm_15m, force=False, reason="first_init"):
    if not force and adx > ADX_TREND_LIMIT:
        state["last_grid_init_attempt"] = time.time()
        save_state(state)
        logger.warning(f"⏸️ init_grid bloqué (ADX={adx:.1f} > {ADX_TREND_LIMIT})")
        return False

    state["last_grid_init_attempt"] = time.time()
    save_state(state)

    P0 = price
    quote_bal, _, total_wallet, capital_for_grid = get_balances(P0, state)
    if total_wallet <= 0:
        logger.error("❌ Capital nul — init_grid annulée")
        return False
    target_nu, target_nl = adjust_levels_to_balance(quote_bal, (capital_for_grid - quote_bal))
    gub, glb, nu, nl, regime = compute_asymmetry(dip, dim, target_nu, target_nl)
    Gul, Gll = compute_dynamic_bounds(P0, atr, stress, gub, glb)
    density_k = compute_density_k(atr_norm_15m, state)   # utilise les params dynamiques
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

    # Journal — reconstruction de grille
    journal.log_grid_init(
        state=state,
        regime=regime,
        reason=reason,
        sell_grid_list=state["sell_grid"].to_list(),
        buy_grid_list=state["buy_grid"].to_list(),
    )

    return True

# ═══════════════════════════════════════════════════════════════
# SMART ROUTER
# ═══════════════════════════════════════════════════════════════

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
        result = exchange.create_market_order(SYMBOL, side, qty_asset)
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
# CALIBRATION PÉRIODIQUE DES PARAMÈTRES ATR/K
# ═══════════════════════════════════════════════════════════════

CALIBRATION_INTERVAL = 7200   # 2 heures en secondes
THRESHOLD_ATR_CHANGE = 0.30   # variation relative max avant rebuild
THRESHOLD_K_MIN_CHANGE = 0.10 # écart absolu max sur K_MIN

def try_calibrate_params(state: dict):
    """Appelle script_atr.calibrate() et met à jour les paramètres si nécessaire."""
    if not _CALIBRATE_AVAILABLE:
        return
    try:
        new_params = calibrate(SYMBOL)
        if not new_params or not all(k in new_params for k in ["atr_low", "atr_high", "k_min", "k_max"]):
            logger.warning("Calibration : résultat incomplet, ignoré.")
            return

        # Valeurs de référence (fixées au démarrage ou après un rebuild dû à calibration)
        ref_low = state.get("CALIB_REF_ATR_LOW", DENSITY_ATR_LOW)
        ref_high = state.get("CALIB_REF_ATR_HIGH", DENSITY_ATR_HIGH)
        ref_kmin = state.get("CALIB_REF_K_MIN", DENSITY_K_MIN)

        # Calcul des écarts
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
            # Mise à jour des paramètres courants
            state["DENSITY_ATR_LOW"] = new_params["atr_low"]
            state["DENSITY_ATR_HIGH"] = new_params["atr_high"]
            state["DENSITY_K_MIN"] = new_params["k_min"]
            state["DENSITY_K_MAX"] = new_params["k_max"]
            # Mise à jour des références pour la prochaine comparaison
            state["CALIB_REF_ATR_LOW"] = new_params["atr_low"]
            state["CALIB_REF_ATR_HIGH"] = new_params["atr_high"]
            state["CALIB_REF_K_MIN"] = new_params["k_min"]
            # Forcer la reconstruction de la grille
            state["grid_ready"] = False
            logger.info("🔄 Reconstruction de la grille programmée (nouveaux paramètres ATR/K)")
        else:
            logger.debug(
                f"Calibration : écarts non significatifs (ATR_LOW={delta_low:.1%}, "
                f"ATR_HIGH={delta_high:.1%}, K_MIN={delta_kmin:.2f})"
            )
    except Exception as e:
        logger.error(f"Erreur calibration périodique : {e}")

# ═══════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════

logger.info(f"🚀 Démarrage Moteur Quantitatif {BOT_VERSION} — Target: {SYMBOL}")

start_price_websocket(SYMBOL)
get_symbol_precisions()
state = load_state()
journal = TradeJournal(SYMBOL)

# ── Calibration initiale (si disponible) ──────────────────────
if _CALIBRATE_AVAILABLE and not state.get("grid_ready"):
    try:
        init_params = calibrate(SYMBOL)
        if init_params and all(k in init_params for k in ["atr_low", "atr_high", "k_min", "k_max"]):
            state["DENSITY_ATR_LOW"] = init_params["atr_low"]
            state["DENSITY_ATR_HIGH"] = init_params["atr_high"]
            state["DENSITY_K_MIN"] = init_params["k_min"]
            state["DENSITY_K_MAX"] = init_params["k_max"]
            state["CALIB_REF_ATR_LOW"] = init_params["atr_low"]
            state["CALIB_REF_ATR_HIGH"] = init_params["atr_high"]
            state["CALIB_REF_K_MIN"] = init_params["k_min"]
            logger.info(f"📏 Paramètres ATR/K calibrés au démarrage pour {SYMBOL}")
    except Exception as e:
        logger.warning(f"Échec calibration initiale : {e}")

reconcile_open_orders(state)

# Journal — démarrage
_capital_for_startup = state.get("capital_usdc", 0.0) or (MAX_BUDGET_USDC or 0.0)
journal.log_startup(state=state, capital_usdc=_capital_for_startup, nb_bots=NB_BOTS)

macro_data = get_heavy_indicators()
while not macro_data:
    logger.warning("⚠️ Attente indicateurs...")
    time.sleep(10)
    macro_data = get_heavy_indicators()

price0 = get_ws_price()
if price0:
    _, _, total_wallet0, _ = get_balances(price0, state)
    if AUTO_RECONCILE:
        reconcile_inventory(state, price0)
    else:
        logger.info("ℹ️ Réconciliation manuelle désactivée. Utilisez --reconcile au démarrage si nécessaire.")
else:
    total_wallet0 = 0.0
    logger.warning("⚠️ Impossible de réconcilier l'inventaire au démarrage (prix manquant)")

last_macro_time       = time.time()
last_log_time         = time.time()
last_info_log         = time.time()
last_lock_update      = time.time()
last_ws_check         = time.time()
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

        # ── Calibration périodique (toutes les 2 heures) ────────
        if _CALIBRATE_AVAILABLE and (time.time() - state.get("last_calibration_time", 0) >= CALIBRATION_INTERVAL):
            try_calibrate_params(state)
            state["last_calibration_time"] = time.time()
            save_state(state)

        quote_bal, base_bal, total_wallet, capital_for_grid = get_balances(price, state)

        if total_wallet <= 0:
            failed_consecutive += 1
            time.sleep(LOOP_SLEEP)
            continue

        capital_view = compute_capital_view(state, price)

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
            if time.time() - last_attempt > FORCE_INIT_TIMEOUT:
                logger.warning(f"⏰ Timeout d'initialisation dépassé ({FORCE_INIT_TIMEOUT}s) — forcing init_grid même si ADX élevé")
                force_init = True

        if must_init:
            # Déterminer la raison du rebuild pour le journal
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
            quote_bal, base_bal, total_wallet, capital_for_grid = get_balances(price, state)

        if not state["grid_ready"]:
            time.sleep(LOOP_SLEEP)
            continue

        sell_grid = state["sell_grid"]
        buy_grid = state["buy_grid"]

        # ── TRAITEMENT BUY ──────────────────────────────────────
        while len(buy_grid) > 0 and price <= buy_grid[0]:
            capital_effectif = capital_for_grid * ACTIVE_CAPITAL_RATIO
            capital_effectif *= buy_exposure_factor
            Gv_local = compute_gv(capital_effectif, state["P0"], state["Gul"], state["Gll"],
                                  state["nu"], state["nl"], state["density_k"])
            state["Gv"] = Gv_local
            if quote_bal >= Gv_local:
                touched = buy_grid.pop(0)
                actual_buy_price, filled_qty = smart_execute_order(exchange.SIDE_BUY, Gv_local, price, state, stress, macro_data)
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
                        new_sell_raw = actual_buy_price * (1 + compute_min_gap(state))

                    # Utilisation de min_gap cohérent avec la grille
                    min_gap = compute_min_gap(state)
                    min_sell = actual_buy_price / (1 - min_gap)
                    new_sell_raw = max(new_sell_raw, min_sell)
                    new_sell = round(new_sell_raw, PRICE_DECIMALS)

                    sell_grid.add(new_sell)
                    state["sell_grid"] = sell_grid
                    state["buy_grid"] = buy_grid
                    save_state(state)
                    logger.info(f"⚡ ACHAT @ {actual_buy_price:.4f} | Qté={filled_qty:.4f} | Lots={len(state['inventory_lots'])}")
                    # Journal — achat
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

        # ── TRAITEMENT SELL ──────────────────────────────────────
        while len(sell_grid) > 0 and price >= sell_grid[0]:
            capital_effectif = capital_for_grid * ACTIVE_CAPITAL_RATIO
            capital_effectif *= sell_exposure_factor
            Gv_local = compute_gv(capital_effectif, state["P0"], state["Gul"], state["Gll"],
                                  state["nu"], state["nl"], state["density_k"])
            state["Gv"] = Gv_local
            qty_needed = Gv_local / price

            if state.get("total_base_qty", 0.0) >= qty_needed:
                touched = sell_grid.pop(0)
                actual_sell_price, filled_qty = smart_execute_order(exchange.SIDE_SELL, Gv_local, price, state, stress, macro_data)
                if actual_sell_price is not None and filled_qty > 0:
                    remaining = filled_qty
                    pnl_trade = 0.0
                    new_lots = []
                    lots_consumed = []                          # ← journal FIFO tracking
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
                            lots_consumed.append({"qty": qty_sold, "buy_price": lot["buy_price"]})
                        else:
                            qty_sold = remaining
                            pnl_trade += (actual_sell_price - lot["buy_price"]) * qty_sold
                            pnl_trade -= (actual_sell_price * qty_sold * fee_sell) + (lot["buy_price"] * qty_sold * fee_buy)
                            lots_consumed.append({"qty": qty_sold, "buy_price": lot["buy_price"]})
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
                        new_buy_raw = actual_sell_price * (1 - compute_min_gap(state))

                    # Utilisation de min_gap cohérent avec la grille
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

        # ── LOG PÉRIODIQUE ──────────────────────────────────────
        if time.time() - last_log_time >= 10.0:
            last_log_time = time.time()
            save_state(state)

            capital_view = compute_capital_view(state, price)
            inventory_qty = capital_view["inventory_qty"]
            nb_lots = len(state.get("inventory_lots", []))
            avg_cost = 0.0
            unrealized_pnl = capital_view["unrealized_pnl"]
            if inventory_qty > 0:
                total_cost = capital_view["inventory_cost"]
                avg_cost = total_cost / inventory_qty

            gv_display = state.get("Gv", 0.0)
            pnl_pct = capital_view["pnl_pct"]

            # Détail complet en DEBUG
            logger.debug(
                f"📊 {price:.4f} | Capital={total_wallet:.2f} | CapitalGrid={capital_for_grid:.2f} | Stress={stress:.2f} | "
                f"BUY={len(buy_grid)} SELL={len(sell_grid)} | Gv={gv_display:.2f} | k={state['density_k']:.2f} | "
                f"Trades={state['total_trades']} | PnL réalisé={state.get('total_pnl',0.0):.4f} | UPnL={unrealized_pnl:+.4f} | "
                f"PnL total={pnl_pct*100:+.2f}% | Stock={inventory_qty:.4f} (moy={avg_cost:.4f}) | "
                f"Lots={nb_lots} | EMA_B={state.get('ema_slippage_buy',0.0):.4%} EMA_S={state.get('ema_slippage_sell',0.0):.4%}"
            )

            # Résumé léger en INFO toutes les 5 minutes
            if time.time() - last_info_log >= 300:
                last_info_log = time.time()
                logger.info(
                    f"📊 Résumé {price:.4f} | Wallet={total_wallet:.2f} | PnL total={pnl_pct*100:+.2f}% | "
                    f"Stock={inventory_qty:.4f} | Trades={state['total_trades']} | Stress={stress:.2f}"
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
