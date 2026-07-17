#!/usr/bin/env python3
"""
AUDIT BOT GRID - V12s3 (multi‑exchange, snapshot par exchange, capital par token + cash réel T0)
Analyse complète du patrimoine vs stratégie Hold.
PnL incrémental via curseur last_trade_id -> ne relit jamais les vieux trades.

Le choix de l'exchange se fait par :
- variable d'environnement EXCHANGE (binance, gateio, coinbase)
- ou argument --exchange

Chaque exchange a son propre snapshot T0 indépendant :
  snapshot_binance_t0.json, snapshot_gateio_t0.json, snapshot_coinbase_t0.json, ...

FORMAT DU SNAPSHOT (V12s3) :
  {
    "exchange": "Gate.io",
    "cash_reel_t0": 225.0,          # solde réel en USDC à T0 (portefeuille de référence)
    "EGLD": { "stock": 103.868913, "capital": 296.00, "price": 2.551 },
    "INJ":  { "stock": 28.443466, "capital": 46.00,   "price": 4.123 }
  }

- cash_reel_t0  : solde réel à T0 (utilisé pour le calcul du patrimoine Hold)
- stock         : quantité détenue à T0
- capital       : capital initial attribué à ce bot (décision stratégique, jamais recalculé)
- price         : prix de référence au moment du bootstrap (documentation)

RÈGLE DE BOOTSTRAP D'UN NOUVEAU TOKEN (pour le capital) :
  capital_nouveau = cash_disponible / (nb_tokens_existants + 1)
  (si cash_disponible <= 0, capital = 0)
Aucune autre logique de répartition n'est utilisée.
Les capitaux déjà présents dans le snapshot ne sont jamais modifiés.

Le patrimoine Hold est calculé avec le cash réel T0, pas avec la somme des capitaux.
Les budgets des bots sont affichés séparément, à titre informatif.

Pour Binance, l'ancien snapshot_t0.json (mono-exchange) est repris automatiquement
comme snapshot_binance_t0.json s'il existe encore, puis converti au nouveau format
lors d'un --save-snapshot ultérieur.

Arguments CLI :
  --t0 YYYY-MM-DD    Date de référence (optionnelle ; auto-détectée depuis
                      le premier trade si le snapshot est absent)
  --save-snapshot    Persiste un snapshot partiellement reconstruit (ajout
                      de tokens, ou conversion). Un snapshot tout neuf est
                      toujours persisté automatiquement.
  --reset-cache      Supprime le cache PnL et force un re-scan complet.
"""
import glob
import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone

from exchange_base import ExchangeBase
from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

SNAPSHOT_FILE   = "snapshot_t0.json"      # legacy, mono-exchange
PNL_CACHE       = "pnl_cache.json"
LOG_FILE        = "audit.log"

# Métadonnées du snapshot : ne pas confondre avec les tokens
SNAPSHOT_META_KEYS = {"date_reference", "timestamp_reference", "exchange", "cash_reel_t0"}

DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "binance").lower()


def create_exchange(name: str) -> ExchangeBase:
    if name == "binance":
        from exchange_binance import ExchangeBinance
        return ExchangeBinance()
    elif name == "gateio":
        from exchange_gateio import ExchangeGateIO
        return ExchangeGateIO()
    elif name == "coinbase":
        from exchange_coinbase import ExchangeCoinbase
        return ExchangeCoinbase()
    raise ValueError(f"Exchange non supporté : {name}")


def exchange_key(exchange: ExchangeBase) -> str:
    return "".join(ch for ch in exchange.NAME.lower() if ch.isalnum())


def snapshot_filename(exchange: ExchangeBase) -> str:
    return f"snapshot_{exchange_key(exchange)}_t0.json"


PAIRES: dict = {}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ]
    )

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def fmt_signed(val: float, dec: int = 2) -> str:
    return f"{'+' if val >= 0 else ''}{val:.{dec}f}"

def pct(val: float, ref: float) -> str:
    if ref == 0:
        return "n/a"
    return f"{fmt_signed(val / ref * 100, 2)}%"

def _date_reference(snap: dict) -> str:
    if "date_reference" in snap:
        return snap["date_reference"]
    if "timestamp_reference" in snap:
        return snap["timestamp_reference"]
    return "inconnue"

def _trade_time_ms(t: dict) -> int | None:
    for key in ("time", "timestamp"):
        if key in t and t[key] is not None:
            return int(t[key])
    return None

# ─────────────────────────────────────────────
# STATE BOT
# ─────────────────────────────────────────────

def state_filename(exchange: ExchangeBase, symbol: str) -> str:
    prefix = "" if exchange_key(exchange) == "binance" else f"{exchange_key(exchange)}_"
    return f"state_{prefix}{symbol.lower()}.json"

def load_bot_state(exchange: ExchangeBase, symbol: str) -> dict:
    sf = state_filename(exchange, symbol)
    if not os.path.exists(sf):
        logger.warning(f"[{symbol}] Fichier state absent ({sf})")
        return {}
    try:
        with open(sf) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(f"[{symbol}] Fichier state corrompu ({sf}) : {e}")
        return {}

def get_allocated_usdc(exchange: ExchangeBase) -> float:
    """Somme des capitaux des bots (budgets) — à titre informatif uniquement."""
    total = 0.0
    for cfg in PAIRES.values():
        total += load_bot_state(exchange, cfg["symbol"]).get("capital_usdc", 0.0)
    return total

# ─────────────────────────────────────────────
# PNL INCRÉMENTAL
# ─────────────────────────────────────────────

def load_pnl_cache() -> dict:
    if os.path.exists(PNL_CACHE):
        try:
            with open(PNL_CACHE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Cache PnL corrompu ({PNL_CACHE}) — réinitialisé")
    return {}

def save_pnl_cache(cache: dict):
    with open(PNL_CACHE, "w") as f:
        json.dump(cache, f, indent=2)
    logger.info(f"Cache PnL sauvegardé -> {PNL_CACHE}")

def reset_pnl_cache():
    if os.path.exists(PNL_CACHE):
        os.remove(PNL_CACHE)
        logger.info(f"Cache PnL supprimé ({PNL_CACHE})")

def fetch_new_trades(exchange: ExchangeBase, symbol: str, cache: dict, t0_ms: int) -> dict:
    entry = cache.get(symbol, {
        "last_trade_id": None,
        "usdc_spent":    0.0,
        "usdc_gained":   0.0,
        "nb_trades":     0,
    })

    new_total  = 0
    first_call = True
    from_id    = None if entry["last_trade_id"] is None else entry["last_trade_id"] + 1
    start_time = t0_ms if entry["last_trade_id"] is None else None

    if from_id is None:
        logger.info(f"[{symbol}] Premier scan depuis T0...")

    while True:
        try:
            trades = exchange.get_my_trades(
                symbol     = symbol,
                from_id    = from_id,
                start_time = start_time,
                limit      = 1000
            )
        except Exception as e:
            logger.error(f"[{symbol}] Erreur API : {e}")
            raise

        if not trades:
            if first_call and entry["last_trade_id"] is not None:
                logger.warning(f"[{symbol}] Aucun trade depuis fromId={entry['last_trade_id']+1}")
            break

        first_call = False
        quote = exchange.DEFAULT_QUOTE
        for t in trades:
            qty        = float(t["quoteQty"])
            comm_quote = float(t["commission"]) if t["commissionAsset"] == quote else 0.0
            if t["isBuyer"]:
                entry["usdc_spent"]  += qty + comm_quote
            else:
                entry["usdc_gained"] += qty - comm_quote

        if trades:
            entry["last_trade_id"] = trades[-1]["id"]
        new_total += len(trades)

        if len(trades) < 1000:
            break
        from_id    = entry["last_trade_id"] + 1
        start_time = None

    entry["nb_trades"] += new_total
    if new_total > 0:
        logger.info(f"[{symbol}] +{new_total} nouveaux trades (total: {entry['nb_trades']})")
    else:
        logger.info(f"[{symbol}] Aucun nouveau trade")
    return entry

# ─────────────────────────────────────────────
# SNAPSHOT — BOOTSTRAP SIMPLIFIÉ
# ─────────────────────────────────────────────

def _detect_pairs_from_states(exchange: ExchangeBase) -> list[str]:
    quote = exchange.DEFAULT_QUOTE
    pattern = f"state_*{quote.lower()}.json"
    symbols = []
    for sf in sorted(glob.glob(pattern)):
        stem = os.path.basename(sf).replace("state_", "").replace(".json", "")
        parts = stem.split("_", 1)
        symbol = parts[1] if len(parts) == 2 else stem
        symbols.append(symbol.upper())
    if symbols:
        logger.info(f"Paires actives détectées : {symbols}")
    else:
        logger.info(f"Aucun fichier '{pattern}' trouvé")
    return symbols


def _bootstrap_stock_t0(
    exchange: ExchangeBase,
    t0_dt: datetime | None,
    symbols: list[str],
    authoritative_symbols: set[str] | None = None,
) -> tuple[dict, dict, datetime, dict[str, float], float]:
    """
    Reconstruit stock_t0, cash_reel_t0 et prix de référence pour chaque symbole.
    Retourne (snap_partial, pnl_seed, resolved_t0, prices_t0, cash_reel_t0)
    """
    authoritative_symbols = authoritative_symbols or set()
    quote = exchange.DEFAULT_QUOTE
    auto_detect = t0_dt is None
    t0_ms = None if auto_detect else int(t0_dt.timestamp() * 1000)
    earliest_ms = None

    snap_partial: dict = {}
    pnl_seed: dict = {}
    prices_t0: dict[str, float] = {}
    total_net_spent = 0.0

    for symbol in symbols:
        asset = symbol[: -len(quote)]
        is_authoritative = symbol in authoritative_symbols

        net_qty = 0.0
        flow_out = 0.0
        flow_in = 0.0
        last_trade_id = None
        nb_trades = 0

        from_id = None
        start_time = t0_ms

        if auto_detect:
            logger.info(f"[{symbol}] Bootstrap stock T0 (auto-détection T0)...")
        else:
            logger.info(f"[{symbol}] Bootstrap stock T0 depuis {t0_dt.date()}...")

        while True:
            try:
                trades = exchange.get_my_trades(
                    symbol=symbol, from_id=from_id,
                    start_time=start_time, limit=1000
                )
            except Exception as e:
                logger.error(f"[{symbol}] Erreur bootstrap : {e}")
                raise

            if not trades:
                break

            for t in trades:
                qty = float(t["qty"])
                qty_q = float(t["quoteQty"])
                comm_q = float(t["commission"]) if t["commissionAsset"] == quote else 0.0
                if t["isBuyer"]:
                    net_qty += qty
                    flow_out += qty_q + comm_q
                else:
                    net_qty -= qty
                    flow_in += qty_q - comm_q

                if auto_detect:
                    ts = _trade_time_ms(t)
                    if ts is not None and (earliest_ms is None or ts < earliest_ms):
                        earliest_ms = ts

            last_trade_id = trades[-1]["id"]
            nb_trades += len(trades)

            if len(trades) < 1000:
                break
            from_id = last_trade_id + 1
            start_time = None

        current_balance = exchange.get_balance(asset)
        stock_t0 = current_balance - net_qty
        snap_partial[asset] = {"stock": stock_t0}
        total_net_spent += flow_out - flow_in

        # Prix de référence au moment du bootstrap
        try:
            price = exchange.get_ticker_price(symbol)
            prices_t0[asset] = price if price is not None else 0.0
        except Exception:
            prices_t0[asset] = 0.0
        logger.info(f"Prix de référence pour {asset} : {prices_t0[asset]:.4f}")

        if is_authoritative:
            logger.info(f"[{symbol}] stock_t0={stock_t0:.6f} (actuel={current_balance:.6f}) | PnL source de vérité")
        else:
            pnl_seed[symbol] = {
                "last_trade_id": last_trade_id,
                "usdc_spent": flow_out,
                "usdc_gained": flow_in,
                "nb_trades": nb_trades,
            }
            logger.info(f"[{symbol}] stock_t0={stock_t0:.6f} | flux net={flow_in-flow_out:+.4f} | trades={nb_trades}")

    # Cash réel à T0 = cash actuel + total_net_spent (flux net depuis T0)
    current_cash = exchange.get_balance(quote)
    cash_reel_t0 = current_cash + total_net_spent
    logger.info(f"💰 Cash réel T0 = {cash_reel_t0:.2f} (actuel={current_cash:.2f}, flux_net={total_net_spent:+.2f})")

    if auto_detect:
        if earliest_ms is not None:
            resolved_t0 = datetime.fromtimestamp(earliest_ms / 1000, tz=timezone.utc)
            logger.info(f"📅 T0 auto-détecté : {resolved_t0.isoformat()}")
        else:
            resolved_t0 = datetime.now(timezone.utc)
            logger.info(f"📅 Aucun trade — T0 = maintenant ({resolved_t0.isoformat()})")
    else:
        resolved_t0 = t0_dt

    return snap_partial, pnl_seed, resolved_t0, prices_t0, cash_reel_t0


def _authoritative_symbols(exchange: ExchangeBase, symbols: list[str]) -> set[str]:
    out = set()
    for sym in symbols:
        st = load_bot_state(exchange, sym)
        if "total_pnl" in st and "total_trades" in st:
            out.add(sym)
    return out


def _seed_pnl_cache(pnl_seed: dict) -> None:
    if not pnl_seed:
        return
    cache = load_pnl_cache()
    seeded = [sym for sym in pnl_seed if sym not in cache]
    if seeded:
        for sym in seeded:
            cache[sym] = pnl_seed[sym]
        save_pnl_cache(cache)
        logger.info(f"Cache PnL amorcé pour : {seeded}")


def _write_snapshot(snap: dict, filename: str) -> None:
    snap_copy = snap.copy()
    # Les métadonnées autorisées sont dans SNAPSHOT_META_KEYS (inclut cash_reel_t0)
    for key, val in snap_copy.items():
        if key not in SNAPSHOT_META_KEYS:
            if not isinstance(val, dict):
                logger.warning(f"⚠️ Entrée {key} invalide")
                continue
            if "capital" not in val:
                logger.warning(f"⚠️ Token {key} sans 'capital' — mise à 0")
                val["capital"] = 0.0
            if "price" not in val:
                logger.warning(f"⚠️ Token {key} sans 'price' — mise à 0")
                val["price"] = 0.0
    with open(filename, "w") as f:
        json.dump(snap_copy, f, indent=2, ensure_ascii=False)
    logger.info(f"📸 Snapshot sauvegardé -> {filename}")


def _migrate_legacy_binance_snapshot(exchange: ExchangeBase, snap_file: str) -> bool:
    if exchange_key(exchange) != "binance":
        return False
    if os.path.exists(snap_file) or not os.path.exists(SNAPSHOT_FILE):
        return False
    try:
        with open(SNAPSHOT_FILE) as f:
            legacy = json.load(f)
    except json.JSONDecodeError:
        return False
    legacy["exchange"] = exchange.NAME
    # On tente de récupérer un cash_reel_t0 si présent (ancien CASH)
    cash_reel = 0.0
    if "CASH" in legacy and isinstance(legacy["CASH"], dict):
        for key in ("USDC", "USDT"):
            if key in legacy["CASH"]:
                cash_reel = float(legacy["CASH"].get(key, 0.0))
                break
        del legacy["CASH"]
    legacy["cash_reel_t0"] = cash_reel
    for key in list(legacy.keys()):
        if key not in SNAPSHOT_META_KEYS:
            if isinstance(legacy[key], dict):
                legacy[key]["capital"] = 0.0
                legacy[key]["price"] = 0.0
            else:
                legacy[key] = {"stock": legacy[key], "capital": 0.0, "price": 0.0}
    with open(snap_file, "w") as f:
        json.dump(legacy, f, indent=2, ensure_ascii=False)
    logger.info(f"📦 Migration legacy Binance -> {snap_file}")
    return True


def _calculate_capital_simple(
    exchange: ExchangeBase,
    snap: dict,
    new_tokens: list[str],
    quote: str,
) -> dict[str, float]:
    """
    Règle simple de bootstrap du capital pour les nouveaux tokens UNIQUEMENT.
    Capital_nouveau = cash_disponible / (nb_tokens_existants + 1)
    Les capitaux déjà présents dans le snapshot sont ignorés (ne sont jamais recalculés).
    """
    capitals = {}

    # Tokens déjà présents (avec capital) — on les compte simplement pour le nombre
    existing_tokens = [k for k in snap if k not in SNAPSHOT_META_KEYS and k not in new_tokens]
    nb_existing = len(existing_tokens)

    # Cash disponible : solde réel actuel
    cash_actuel = exchange.get_balance(quote)

    # Nombre total de bots après ajout des nouveaux
    nb_total = nb_existing + len(new_tokens)

    if cash_actuel > 0 and nb_total > 0:
        capital_par_bot = cash_actuel / nb_total
        for token in new_tokens:
            capitals[token] = round(capital_par_bot, 2)
            logger.info(f"💰 Capital attribué à {token} : {capitals[token]:.2f} (cash {cash_actuel:.2f} / {nb_total} bots)")
    else:
        for token in new_tokens:
            capitals[token] = 0.0
            logger.warning(f"⚠️ Aucun cash disponible — capital de {token} mis à 0")

    return capitals


def get_snapshot(
    exchange: ExchangeBase,
    t0_override: str | None = None,
    save_snapshot: bool = False,
) -> dict:
    global PAIRES
    quote = exchange.DEFAULT_QUOTE
    snap_file = snapshot_filename(exchange)

    # Détection des paires actives
    active_symbols = _detect_pairs_from_states(exchange)

    # Migration legacy
    _migrate_legacy_binance_snapshot(exchange, snap_file)

    # Chargement du snapshot
    snap: dict = {}
    snapshot_loaded = False
    snapshot_is_new = False

    if os.path.exists(snap_file):
        try:
            with open(snap_file) as f:
                snap = json.load(f)
            snapshot_loaded = True
            logger.info(f"Snapshot chargé : {snap_file}")
        except json.JSONDecodeError:
            logger.warning(f"Snapshot corrompu — reconstruction")
            snapshot_is_new = True
    else:
        snapshot_is_new = True
        logger.warning(f"⚠️ Snapshot absent ({snap_file}) — création automatique")
        if not active_symbols:
            raise ValueError(
                f"{snap_file} introuvable et aucun fichier state_*{quote.lower()}.json "
                f"détecté. Démarrez le bot au moins une fois."
            )

    # Résolution T0
    t0_dt: datetime | None = None
    if t0_override:
        try:
            t0_dt = datetime.strptime(t0_override, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"Format --t0 invalide : '{t0_override}'")
        snap.setdefault("date_reference", t0_dt.strftime("%Y-%m-%d"))
        snap.setdefault("timestamp_reference", t0_dt.isoformat())
        logger.info(f"T0 explicite : {t0_override}")
    elif snapshot_loaded:
        if "timestamp_reference" in snap:
            t0_dt = datetime.fromisoformat(snap["timestamp_reference"].replace("Z", "+00:00"))
        elif "date_reference" in snap:
            t0_dt = datetime.strptime(snap["date_reference"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            raise ValueError(f"{snap_file} doit contenir 'date_reference' ou 'timestamp_reference'")
    elif not snapshot_is_new:
        raise ValueError(f"{snap_file} corrompu et aucune date T0. Utilisez --t0 YYYY-MM-DD")

    # Identifier les tokens manquants ou incomplets
    known_tokens = {k for k in snap if k not in SNAPSHOT_META_KEYS}
    tokens_to_bootstrap = []
    for sym in active_symbols:
        asset = sym[: -len(quote)]
        if asset not in known_tokens:
            tokens_to_bootstrap.append(sym)
        else:
            token_data = snap.get(asset, {})
            if not isinstance(token_data, dict) or "capital" not in token_data or "price" not in token_data:
                tokens_to_bootstrap.append(sym)

    # Bootstrap des tokens manquants
    if tokens_to_bootstrap:
        logger.info(f"🔧 Bootstrap tokens manquants/incomplets : {[s[: -len(quote)] for s in tokens_to_bootstrap]}")

        partial, pnl_seed, resolved_t0, prices_t0, cash_reel_t0 = _bootstrap_stock_t0(
            exchange,
            t0_dt,
            tokens_to_bootstrap,
            authoritative_symbols=_authoritative_symbols(exchange, tokens_to_bootstrap),
        )

        if t0_dt is None:
            t0_dt = resolved_t0
            snap.setdefault("date_reference", t0_dt.strftime("%Y-%m-%d"))
            snap.setdefault("timestamp_reference", t0_dt.isoformat())

        # Récupération des noms d'actifs (sans le quote)
        new_assets = [s[: -len(quote)] for s in tokens_to_bootstrap]

        # Calcul du capital simple (uniquement pour les nouveaux tokens)
        capitals = _calculate_capital_simple(exchange, snap, new_assets, quote)

        # Fusion dans le snapshot
        for asset in new_assets:
            if asset in partial:
                snap[asset] = {
                    "stock": partial[asset].get("stock", 0.0),
                    "capital": capitals.get(asset, 0.0),
                    "price": prices_t0.get(asset, 0.0),
                }
            else:
                snap[asset] = {"stock": 0.0, "capital": capitals.get(asset, 0.0), "price": 0.0}

        snap["exchange"] = exchange.NAME
        snap["cash_reel_t0"] = cash_reel_t0  # Stocker le cash réel T0

        # Amorcer le cache PnL
        _seed_pnl_cache(pnl_seed)

        # Sauvegarde
        if snapshot_is_new or save_snapshot:
            _write_snapshot(snap, snap_file)
        else:
            logger.info(f"📋 Snapshot mis à jour en mémoire. Utilisez --save-snapshot pour persister.")

    else:
        snap["exchange"] = exchange.NAME
        # Si --save-snapshot et snapshot ancien (sans cash_reel_t0), on ajoute
        if save_snapshot and "cash_reel_t0" not in snap:
            logger.info("🔄 Ajout de cash_reel_t0 au snapshot")
            # On peut essayer de le reconstruire à partir du solde actuel et des flux nets
            # Mais ici on simplifie : on le met à 0, l'utilisateur pourra le recalculer via un bootstrap ultérieur.
            snap["cash_reel_t0"] = 0.0
        # Vérifier que tous les tokens ont price et capital
        for key in list(snap.keys()):
            if key not in SNAPSHOT_META_KEYS:
                if isinstance(snap[key], dict):
                    snap[key].setdefault("price", 0.0)
                    snap[key].setdefault("capital", 0.0)
        if save_snapshot:
            _write_snapshot(snap, snap_file)

    # Construction de PAIRES
    token_names = [k for k in snap if k not in SNAPSHOT_META_KEYS]
    active_assets = {sym[: -len(quote)] for sym in active_symbols}

    if active_assets:
        token_names = [k for k in token_names if k in active_assets]
        missing = sorted(active_assets - set(token_names))
        if missing:
            raise ValueError(
                f"Tokens actifs sans entrée dans le snapshot : {missing}. "
                f"Relancez avec --t0 et --save-snapshot."
            )
    else:
        logger.warning(f"Aucun state_*{quote.lower()}.json trouvé — utilisation de tous les tokens du snapshot")

    if not token_names:
        raise ValueError(f"Aucun token actif pour {exchange.NAME}")

    for name in token_names:
        if "stock" not in snap.get(name, {}):
            raise ValueError(f"Clé 'stock' manquante pour {name}")
        snap[name].setdefault("capital", 0.0)
        snap[name].setdefault("price", 0.0)

    PAIRES = {name: {"symbol": f"{name}{quote}", "asset": name} for name in token_names}
    logger.info(f"Tokens pour l'audit : {list(PAIRES.keys())}")

    return snap

# ─────────────────────────────────────────────
# ANALYSE PRINCIPALE
# ─────────────────────────────────────────────

def run_audit(exchange: ExchangeBase, snapshot: dict) -> dict:
    if "timestamp_reference" in snapshot:
        t0 = datetime.fromisoformat(snapshot["timestamp_reference"].replace("Z", "+00:00"))
    else:
        t0 = datetime.strptime(snapshot["date_reference"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    t0_ms = int(t0.timestamp() * 1000)

    cache = load_pnl_cache()
    cash_free, cash_locked, cash_actuel = get_cash_balance(exchange)

    results = {}
    valeur_crypto_actuelle = 0.0
    valeur_crypto_hold = 0.0
    pnl_reel_total = 0.0
    trades_total = 0
    quote = exchange.DEFAULT_QUOTE

    for name, cfg in PAIRES.items():
        try:
            solde_actuel = exchange.get_balance(cfg["asset"])
            prix = exchange.get_ticker_price(cfg["symbol"])
            if prix is None:
                raise ValueError(f"Prix introuvable pour {cfg['symbol']}")
        except Exception as e:
            logger.error(f"[{cfg['symbol']}] Erreur API : {e}")
            raise

        stock_t0 = snapshot[name]["stock"]
        val_actuelle = solde_actuel * prix
        val_hold = stock_t0 * prix
        delta_tokens = solde_actuel - stock_t0

        state = load_bot_state(exchange, cfg["symbol"])

        bot_is_source_of_truth = "total_pnl" in state and "total_trades" in state

        if bot_is_source_of_truth:
            pnl_reel = state["total_pnl"]
            nb_trades = state["total_trades"]
            pnl_source = "bot_state"
            usdc_spent = None
            usdc_gained = None
        else:
            c = fetch_new_trades(exchange, cfg["symbol"], cache, t0_ms)
            cache[cfg["symbol"]] = c
            pnl_reel = c["usdc_gained"] - c["usdc_spent"]
            nb_trades = c["nb_trades"]
            pnl_source = "trades_reconstruct"
            usdc_spent = c["usdc_spent"]
            usdc_gained = c["usdc_gained"]
            logger.warning(f"[{name}] total_pnl absent — PnL reconstruit (fallback)")

        delta_tokens_value = delta_tokens * prix
        alpha_pair = pnl_reel + delta_tokens_value

        capital_usdc = state.get("capital_usdc", 0.0)
        budget_usdc = state.get("budget_usdc", capital_usdc)
        wallet_peak = state.get("wallet_peak", capital_usdc)

        pnl_bot = state.get("total_pnl", 0.0)
        total_wallet = capital_usdc + pnl_bot

        drawdown_pct = max(0.0, 1.0 - total_wallet / wallet_peak) if wallet_peak > 0 else 0.0
        alpha_pct = (alpha_pair / capital_usdc) if capital_usdc > 0 else 0.0

        gv = state.get("Gv")
        density_k = state.get("density_k")
        buy_grid = state.get("buy_grid", [])
        sell_grid = state.get("sell_grid", [])
        nb_levels = len(buy_grid) + len(sell_grid)
        grille_prete = state.get("grid_ready", False)

        total_base_qty = state.get("total_base_qty", 0.0)
        if abs(total_base_qty - solde_actuel) > 1e-6:
            logger.warning(f"[{name}] Écart inventaire : state={total_base_qty:.6f}, réel={solde_actuel:.6f}")

        valeur_crypto_actuelle += val_actuelle
        valeur_crypto_hold += val_hold
        pnl_reel_total += pnl_reel
        trades_total += nb_trades

        results[name] = {
            "symbol": cfg["symbol"],
            "solde_actuel": solde_actuel,
            "stock_t0": stock_t0,
            "delta_tokens": delta_tokens,
            "prix": prix,
            "val_actuelle": val_actuelle,
            "val_hold": val_hold,
            "usdc_spent": usdc_spent,
            "usdc_gained": usdc_gained,
            "pnl_reel": pnl_reel,
            "nb_trades": nb_trades,
            "grille_prete": grille_prete,
            "sell_grid": sell_grid,
            "buy_grid": buy_grid,
            "delta_tokens_value": delta_tokens_value,
            "alpha_pair": alpha_pair,
            "capital_usdc": capital_usdc,
            "budget_usdc": budget_usdc,
            "pnl_bot": pnl_bot,
            "alpha_pct": alpha_pct,
            "wallet_peak": wallet_peak,
            "total_wallet": total_wallet,
            "drawdown_pct": drawdown_pct,
            "efficiency": (alpha_pair / nb_trades) if nb_trades > 0 else 0.0,
            "gv": gv,
            "density_k": density_k,
            "nb_levels": nb_levels,
            "total_base_qty": total_base_qty,
            "pnl_source": pnl_source,
        }

    save_pnl_cache(cache)

    total_crypto_value = sum(d["val_actuelle"] for d in results.values())
    for name, d in results.items():
        poids = d["val_actuelle"] / total_crypto_value if total_crypto_value > 0 else 1.0 / len(results)
        f0_bot = d["val_actuelle"] + cash_actuel * poids
        f0_recommande = round(f0_bot * 0.90, 2)
        results[name]["f0_estime"] = round(f0_bot, 2)
        results[name]["f0_recommande"] = f0_recommande

    # --- PATRIMOINE HOLD AVEC CASH RÉEL T0 ---
    cash_reel_t0 = snapshot.get("cash_reel_t0", 0.0)

    # Patrimoine Hold = crypto revalorisée + cash réel T0
    patrimoine_actuel = valeur_crypto_actuelle + cash_actuel
    patrimoine_hold = valeur_crypto_hold + cash_reel_t0
    alpha_brut = patrimoine_actuel - patrimoine_hold

    alpha_latent = valeur_crypto_actuelle - valeur_crypto_hold
    alpha_cash = cash_actuel - cash_reel_t0
    ratio_cristallisation = (alpha_cash / alpha_brut * 100) if alpha_brut != 0 else 0.0

    # --- BUDGETS DES BOTS (information indépendante) ---
    capital_initial_total = sum(
        snapshot.get(name, {}).get("capital", 0.0)
        for name in PAIRES.keys()
    )

    logger.info(
        f"Audit terminé — Patrimoine actuel : {patrimoine_actuel:.2f} {quote}  "
        f"| Alpha brut : {fmt_signed(alpha_brut)} {quote}  "
        f"| Trades : {trades_total}"
    )

    return {
        "exchange_name": exchange.NAME,
        "quote": quote,
        "date_reference": _date_reference(snapshot),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paires": results,
        "cash_free": cash_free,
        "cash_locked": cash_locked,
        "cash_actuel": cash_actuel,
        "cash_reel_t0": cash_reel_t0,                 # Cash réel à T0 (portefeuille de référence)
        "capital_initial_total": capital_initial_total,  # Somme des budgets (informatif)
        "cash_alloue": get_allocated_usdc(exchange),     # Budgets réellement utilisés par les bots
        "valeur_crypto_actuelle": valeur_crypto_actuelle,
        "valeur_crypto_hold": valeur_crypto_hold,
        "patrimoine_actuel": patrimoine_actuel,
        "patrimoine_hold": patrimoine_hold,
        "alpha_brut": alpha_brut,
        "alpha_latent": alpha_latent,
        "alpha_cash": alpha_cash,
        "ratio_cristallisation": ratio_cristallisation,
        "pnl_reel_total": pnl_reel_total,
        "delta_tokens_value": alpha_latent,
        "trades_total": trades_total,
    }


def get_cash_balance(exchange: ExchangeBase) -> tuple[float, float, float]:
    try:
        cash_actuel = exchange.get_balance(exchange.DEFAULT_QUOTE)
    except Exception as e:
        logger.error(f"Erreur API solde {exchange.DEFAULT_QUOTE} : {e}")
        raise
    return cash_actuel, 0.0, cash_actuel

# ─────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────

def print_report(r: dict):
    W, SEP, sep = 62, "=" * 62, "-" * 62
    print(f"\n{SEP}")
    print(f"  AUDIT PATRIMOINE GRID BOT — {r['timestamp']}")
    print(f"  Référence T0 : {r['date_reference']}")
    print(SEP)

    for name, d in r["paires"].items():
        status = "OK" if d["grille_prete"] else "KO"
        delta_ico = "+" if d["delta_tokens"] >= 0 else ""
        print(f"\n  [{status}] {name}/USDC  @  {d['prix']:.4f} $")
        print(sep)
        print(f"    Stock T0           : {d['stock_t0']:.4f} tokens")
        print(f"    Stock Actuel       : {d['solde_actuel']:.4f} tokens  ({delta_ico}{d['delta_tokens']:.4f})")
        print(f"    Val. Actuelle      : {d['val_actuelle']:.2f} $")
        print(f"    Val. Hold Pure     : {d['val_hold']:.2f} $")
        print(sep)
        if d["usdc_spent"] is not None:
            print(f"    USDC dépensé       : {d['usdc_spent']:.4f} $")
            print(f"    USDC encaissé      : {d['usdc_gained']:.4f} $")
        print(f"    PnL Réel           : {fmt_signed(d['pnl_reel'], 4)} $  <- source de vérité")
        if d.get("pnl_source") == "trades_reconstruct":
            print(f"      ⚠️  reconstruit depuis les trades")
        print(f"    Trades totaux      : {d['nb_trades']}")
        print(f"    Grille BUY / SELL  : {len(d['buy_grid'])} / {len(d['sell_grid'])}")

    print(f"\n{SEP}")
    print(f"  SYNTHESE PATRIMOINE")
    print(sep)
    print(f"    Cash réel T0        : {r['cash_reel_t0']:.2f} $  (portefeuille de référence)")
    print(f"    Cash USDC Actuel    : {r['cash_actuel']:.2f} $  (free {r['cash_free']:.2f})")
    print(f"    Budgets alloués     : {r['capital_initial_total']:.2f} $  (somme des capitaux par token, informatif)")
    print(f"    Cash alloué réel    : {r['cash_alloue']:.2f} $  (budgets effectivement utilisés par les bots)")
    print(f"    Crypto (Actuel)     : {r['valeur_crypto_actuelle']:.2f} $")
    print(f"    Crypto (Hold)       : {r['valeur_crypto_hold']:.2f} $")
    print(sep)
    print(f"    Patrimoine Actuel   : {r['patrimoine_actuel']:.2f} $")
    print(f"    Patrimoine Hold     : {r['patrimoine_hold']:.2f} $")
    print(sep)
    print(f"    ALPHA BRUT          : {fmt_signed(r['alpha_brut'])} $  ({pct(r['alpha_brut'], r['patrimoine_hold'])})")
    print(sep)
    print(f"    Alpha latent        : {fmt_signed(r['alpha_latent'])} $  (prix-dépendant)")
    print(f"      dont PnL cash     : {fmt_signed(r['pnl_reel_total'], 4)} $")
    print(f"      dont delta tok.   : {fmt_signed(r['delta_tokens_value'], 4)} $")
    c = r["ratio_cristallisation"]
    ico_c = "[!!]" if c < 20 else ("[~]" if c < 50 else "[OK]")
    print(f"    Cristallisation     : {ico_c} {c:.1f}%")
    print(sep)
    print(f"    PnL Réel Total      : {fmt_signed(r['pnl_reel_total'], 4)} $")
    print(f"    Trades Totaux       : {r['trades_total']}")
    print(SEP)


def export_metrics_json(r, filename="audit_metrics.json"):
    out = {
        "timestamp": r["timestamp"],
        "date_reference": r["date_reference"],
        "cash_reel_t0": r["cash_reel_t0"],
        "capital_initial_total": r["capital_initial_total"],
        "pairs": {}
    }
    for pair, d in r["paires"].items():
        out["pairs"][pair] = {
            "alpha_pair": d["alpha_pair"],
            "alpha_pct": d["alpha_pct"],
            "pnl_real": d["pnl_reel"],
            "delta_token_value": d["delta_tokens_value"],
            "capital_usdc": d["capital_usdc"],
            "budget_usdc": d["budget_usdc"],
            "pnl_bot": d["pnl_bot"],
            "wallet_peak": d["wallet_peak"],
            "total_wallet": d["total_wallet"],
            "drawdown_pct": d["drawdown_pct"],
            "trades": d["nb_trades"],
            "price": d["prix"],
            "gv": d["gv"],
            "density_k": d["density_k"],
            "nb_levels": d["nb_levels"],
            "f0_estime": d["f0_estime"],
            "f0_recommande": d["f0_recommande"],
            "total_base_qty": d["total_base_qty"],
            "pnl_source": d["pnl_source"],
        }

    with open(filename, "w") as f:
        json.dump(out, f, indent=2)

    history_file = "portfolio_history.jsonl"
    with open(history_file, "a") as f:
        for pair, d in out["pairs"].items():
            row = {**{"timestamp": out["timestamp"], "pair": pair}, **d}
            f.write(json.dumps(row) + "\n")

    logger.info(f"Metrics Portfolio -> {filename}")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Audit Grid Bot — V12s3 (snapshot avec cash réel T0 et capital par token)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python analyse.py --exchange gateio
  python analyse.py --exchange gateio --t0 2026-01-15
  python analyse.py --exchange gateio --t0 2026-06-01 --save-snapshot
  python analyse.py --reset-cache
        """
    )
    parser.add_argument("--reset-cache", action="store_true", help="Supprime le cache PnL")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE, help=f"Exchange (défaut: {DEFAULT_EXCHANGE})")
    parser.add_argument("--t0", default=None, metavar="YYYY-MM-DD", help="Date de référence T0")
    parser.add_argument("--save-snapshot", action="store_true", help="Persiste le snapshot mis à jour")
    return parser.parse_args()


if __name__ == "__main__":
    setup_logging()
    args = parse_args()

    if args.reset_cache:
        reset_pnl_cache()

    try:
        exchange = create_exchange(args.exchange)
        snapshot = get_snapshot(exchange, t0_override=args.t0, save_snapshot=args.save_snapshot)
        result = run_audit(exchange, snapshot)
        print_report(result)
        export_metrics_json(result)
    except Exception as e:
        logger.exception(f"Erreur : {e}")
        sys.exit(1)
