#!/usr/bin/env python3
"""
analyse.py – Moteur de métriques pour Portfolio Manager
================================================================================
Version : V1.3 (détection des bots via locks, sans invalidation temporelle,
          structure enrichie)

Rôle :
  - Détecter les bots réellement actifs à partir des fichiers lock_*.pid
  - Charger leur état et produire un contrat de données fiable
  - Fallback sur les fichiers state_*.json si aucun lock n'est trouvé

Invariants (vérifiés systématiquement par fail-fast) :
  1. inventory_qty == sum(lot["qty"])
  2. inventory_cost == sum(lot["qty"] * lot["buy_price"])
  3. wallet == capital_usdc + total_pnl + pnl_latent
  4. 0 <= drawdown_pct <= 1

Ce fichier ne produit plus de rapport humain.
================================================================================
"""

# ==========================================================
# AUDIT METRICS CONTRACT V1
#
# Ce fichier constitue la source officielle des métriques
# consommées par Portfolio Manager.
#
# Toute évolution devra :
#   - rester rétrocompatible
#   - ou incrémenter schema_version.
#
# Les calculs comptables sont considérés comme figés.
# ==========================================================


import os
import sys
import json
import glob
import time
import logging
import argparse
from datetime import datetime, timezone

# ─── IMPORT EXCHANGE ──────────────────────────────────────────
try:
    from exchange_base import ExchangeBase
except ImportError:
    ExchangeBase = None
    print("⚠️  exchange_base introuvable. Assurez-vous qu'il est dans le PYTHONPATH.")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── IMPORT INVENTORY MANAGER ────────────────────────────────
import inventory_manager as inv_mgr

# ─── CONSTANTES ──────────────────────────────────────────────
DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "gateio").lower()
SUPPORTED_QUOTES = ("USDC", "USDT")
INVARIANT_EPSILON = 1e-6
# Le timestamp est conservé pour information, mais n'est plus utilisé pour invalider un lock.

# ─── LOGGING ──────────────────────────────────────────────────
LOG_FILE = "analyse.log"
logger = logging.getLogger(__name__)

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

# ─── EXCHANGE FACTORY ────────────────────────────────────────
def create_exchange(name: str) -> "ExchangeBase":
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

def exchange_key(exchange: "ExchangeBase") -> str:
    return "".join(ch for ch in exchange.NAME.lower() if ch.isalnum())

# ─── DÉTECTION DES BOTS (ANCIENNE, FALLBACK) ──────────────────
def detect_bots(exchange_key: str, quote: str = None) -> dict:
    """
    Détecte les fichiers state_{exchange_key}_*.json.
    Retourne un dictionnaire {symbole: chemin_du_state}.
    Utilisé uniquement en fallback si aucun lock valide n'est trouvé.
    """
    pattern = f"state_{exchange_key}_*.json"
    bots = {}
    for sf in sorted(glob.glob(pattern)):
        stem = os.path.basename(sf).replace("state_", "").replace(".json", "")
        if stem.startswith(f"{exchange_key}_"):
            symbol_part = stem[len(exchange_key)+1:]
        else:
            symbol_part = stem
        symbol = symbol_part.upper()
        if quote:
            if not symbol.endswith(quote):
                continue
        else:
            found_quote = None
            for q in SUPPORTED_QUOTES:
                if symbol.endswith(q):
                    found_quote = q
                    break
            if not found_quote:
                continue
        bots[symbol] = sf
    return bots

# ─── NOUVELLE DÉTECTION PAR LOCKS ─────────────────────────────
def is_lock_valid(lock_path: str) -> bool:
    """
    Vérifie si un fichier lock correspond à un processus toujours actif.
    Le timestamp est conservé mais n'est pas utilisé pour invalider le lock.
    Retourne True si le lock est valide, False sinon.
    """
    try:
        with open(lock_path, "r") as f:
            content = f.read().strip()
        parts = content.split(":")
        if len(parts) != 2:
            return False
        pid = int(parts[0])
        # timestamp = int(parts[1])  # conservé mais non utilisé

        # Vérifier si le processus est toujours en vie
        try:
            os.kill(pid, 0)
        except OSError:
            return False  # process mort

        return True
    except Exception:
        return False

def detect_active_bots(exchange_key: str, exchange_name: str, quote: str = None) -> dict:
    """
    Détecte les bots actifs à partir des fichiers lock_*.pid.
    Pour chaque lock valide, vérifie que le fichier state correspondant existe.
    Retourne un dictionnaire {symbole: {"state_file": ..., "lock_file": ..., "pid": ...}}.
    Si aucun lock valide n'est trouvé, retourne un dictionnaire vide.
    """
    lock_files = glob.glob("lock_*.pid")
    if not lock_files:
        logger.info("Aucun fichier lock trouvé.")
        return {}

    active_bots = {}
    for lock_path in lock_files:
        # Extraire le symbole du nom du fichier lock
        basename = os.path.basename(lock_path)
        if not basename.startswith("lock_") or not basename.endswith(".pid"):
            continue
        symbol_lower = basename[5:-4]  # "lock_" + symbol + ".pid"
        symbol = symbol_lower.upper()

        # Filtrer par quote si demandé
        if quote:
            if not symbol.endswith(quote):
                continue
        else:
            # Vérifier que le symbole est bien dans une quote supportée
            if not any(symbol.endswith(q) for q in SUPPORTED_QUOTES):
                continue

        # Vérifier que le lock est valide (processus vivant)
        if not is_lock_valid(lock_path):
            logger.debug(f"Lock {lock_path} invalide (processus mort), ignoré.")
            continue

        # Lire le contenu du lock pour récupérer le PID
        try:
            with open(lock_path, "r") as f:
                parts = f.read().strip().split(":")
            pid = int(parts[0]) if len(parts) >= 1 else None
        except Exception:
            pid = None

        # Construire le chemin du fichier state
        state_file = f"state_{exchange_key}_{symbol_lower}.json"
        if not os.path.exists(state_file):
            logger.error(f"Lock présent pour {symbol} mais state {state_file} introuvable → bot ignoré")
            continue

        active_bots[symbol] = {
            "state_file": state_file,
            "lock_file": lock_path,
            "pid": pid,
        }

    return active_bots

# ─── CHARGEMENT D'UN STATE ──────────────────────────────────
def load_state(state_file: str) -> dict:
    """Charge le state JSON et retourne un dictionnaire avec des valeurs par défaut."""
    defaults = {
        "capital_usdc": 0.0,
        "total_pnl": 0.0,
        "wallet_peak": 0.0,
        "total_trades": 0,
        "grid_ready": False,
        "Gv": 0.0,
        "density_k": 0.65,
        "sell_grid": [],
        "buy_grid": [],
        "inventory_lots": [],
        "inventory_cost": 0.0,
        "P0": None,
    }
    if not os.path.exists(state_file):
        logger.warning(f"Fichier state introuvable : {state_file}")
        return defaults
    try:
        with open(state_file, "r") as f:
            data = json.load(f)
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Erreur de décodage JSON dans {state_file} : {e}")
        return defaults

# ─── VÉRIFICATIONS DES INVARIANTS ─────────────────────────────
def verify_invariants(state: dict, metrics: dict):
    """
    Vérifie les invariants comptables.
    Lance une RuntimeError en cas de violation (fail-fast).
    """
    # Invariant 1 : inventory_qty == somme des lots
    lots = state.get("inventory_lots", [])
    computed_qty = sum(float(lot.get("qty", 0.0)) for lot in lots)
    if abs(metrics["inventory_qty"] - computed_qty) > INVARIANT_EPSILON:
        raise RuntimeError(
            f"Invariant 1 violé : inventory_qty={metrics['inventory_qty']:.8f}, "
            f"somme des lots={computed_qty:.8f}"
        )

    # Invariant 2 : inventory_cost == somme(qty * buy_price)
    computed_cost = sum(float(lot.get("qty", 0.0)) * float(lot.get("buy_price", 0.0)) for lot in lots)
    if abs(metrics["inventory_cost"] - computed_cost) > INVARIANT_EPSILON:
        raise RuntimeError(
            f"Invariant 2 violé : inventory_cost={metrics['inventory_cost']:.8f}, "
            f"coût recalculé={computed_cost:.8f}"
        )

    # Invariant 3 : wallet == capital_usdc + total_pnl + pnl_latent
    capital_usdc = metrics["capital_usdc"]
    total_pnl = metrics["total_pnl"]
    pnl_latent = metrics["pnl_latent"]
    wallet = metrics["wallet"]
    computed_wallet = capital_usdc + total_pnl + pnl_latent
    if abs(wallet - computed_wallet) > INVARIANT_EPSILON:
        raise RuntimeError(
            f"Invariant 3 violé : wallet={wallet:.8f}, "
            f"capital+pnl+latent={computed_wallet:.8f}"
        )

    # Invariant 4 : drawdown_pct dans [0, 1]
    dd = metrics["drawdown_pct"]
    if not (0.0 <= dd <= 1.0):
        raise RuntimeError(
            f"Invariant 4 violé : drawdown_pct={dd:.8f} hors de l'intervalle [0, 1]"
        )

    logger.debug("✅ Tous les invariants sont respectés.")


# ─── TRADING METRICS ─────────────────────────────────────────────

def compute_trading_metrics(state: dict) -> dict:
    """
    Métriques décrivant l'activité de trading du bot.

    Ces métriques sont purement observationnelles.
    """

    return {
        "trading": {
            "total_trades": state.get("total_trades", 0),
        }
    }


# ─── CALCUL DES MÉTRIQUES ──────────────────────────────────
def compute_metrics(state: dict, price: float, symbol: str, exchange_name: str) -> dict:
    """
    Calcule les métriques fondamentales à partir du state et du prix.
    Retourne un dictionnaire conforme au contrat V1.1.
    Les invariants sont vérifiés avant le retour.
    """
    # --- 1. Données d'inventaire (via inventory_manager) ---
    inventory_qty = inv_mgr.inventory_qty(state)
    inventory_cost = inv_mgr.inventory_cost(state)
    inventory_value = inventory_qty * price
    pnl_latent = inventory_value - inventory_cost

    # --- 2. Métriques du wallet ---
    capital_usdc = state.get("capital_usdc", 0.0)
    total_pnl = state.get("total_pnl", 0.0)
    wallet = capital_usdc + total_pnl + pnl_latent

    # --- 3. Peak (correction : max avec le wallet courant) ---
    wallet_peak = max(state.get("wallet_peak", 0.0), wallet)

    # --- 4. Drawdown (correction : borne explicite) ---
    if wallet_peak > 0:
        drawdown_pct = max(0.0, min(1.0, 1.0 - wallet / wallet_peak))
    else:
        drawdown_pct = 0.0

    # --- 5. Alpha ---
    alpha = total_pnl + pnl_latent
    alpha_pct = alpha / capital_usdc if capital_usdc > 0 else 0.0

    # --- 6. Trading metrics ---
    trading = compute_trading_metrics(state)
    
    # --- 7. Diagnostics (déplacés dans un sous-objet) ---
    sell_grid = state.get("sell_grid", [])
    buy_grid = state.get("buy_grid", [])
    diagnostics = {
        "price": price,
        "grid_ready": state.get("grid_ready", False),
        "gv": state.get("Gv", 0.0),
        "density_k": state.get("density_k", 0.65),
        "nb_levels": len(sell_grid) + len(buy_grid),
    }

    # --- 8. Construction du résultat brut ---
    result = {
        # Identité de la paire (rend l'objet autonome)
        "symbol": symbol,
        "exchange": exchange_name,

        # Métriques économiques (premier niveau)
        "capital_usdc": capital_usdc,
        "wallet": wallet,
        "wallet_peak": wallet_peak,
        "total_pnl": total_pnl,
        "inventory_qty": inventory_qty,
        "inventory_cost": inventory_cost,
        "inventory_value": inventory_value,
        "pnl_latent": pnl_latent,
        "alpha": alpha,
        "alpha_pct": alpha_pct,
        "drawdown_pct": drawdown_pct,

        # Nom métier cohérent
        **trading,

        # Diagnostics (isolés, jamais utilisés par le PM)
        "diagnostics": diagnostics,
    }

    # --- 9. Vérification systématique des invariants (fail-fast) ---
    verify_invariants(state, result)

    return result
    

# ─── TEMPORAL METRICS ENGINE ─────────────────────────────────

def compute_temporal_metrics(metrics: dict) -> dict:
    """
    Calcule les métriques nécessitant un historique.

    Cette fonction constitue le point d'entrée unique pour toutes
    les métriques temporelles.

    Pour RN-006, aucune métrique temporelle n'est encore calculée.
    Le dictionnaire est donc retourné inchangé.

    Les futures RN ajouteront notamment :
        - trades_per_day
        - profit_per_trade
        - grid_throughput
        - wallet_growth
        - inventory_turnover
        - ...
    """
    return metrics

# ─── PRODUCTION DU CONTRAT JSON ─────────────────────────────
def build_audit_metrics(exchange_name: str, pairs_metrics: dict) -> dict:
    """Construit le dictionnaire final conforme au contrat V1.1."""
    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exchange": exchange_name,
        "pairs": pairs_metrics,
    }

def write_audit_json(data: dict, exchange_key: str):
    """Écrit le fichier audit_metrics_{exchange}.json."""
    filename = f"audit_metrics_{exchange_key}.json"
    tmp = filename + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, filename)
        logger.info(f"✅ Métriques écrites dans {filename}")
    except Exception as e:
        logger.error(f"❌ Erreur d'écriture de {filename} : {e}")

# ─── MAIN ─────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyse des métriques des bots pour Portfolio Manager (V1.3)"
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE,
                        help=f"Exchange (défaut: {DEFAULT_EXCHANGE})")
    parser.add_argument("--quote", default=None, choices=SUPPORTED_QUOTES,
                        help="Quote asset (défaut: détection auto)")
    return parser.parse_args()

def main():
    setup_logging()
    args = parse_args()

    logger.info(f"=== Analyse des métriques (exchange={args.exchange}) ===")

    try:
        exchange = create_exchange(args.exchange)
    except Exception as e:
        logger.error(f"❌ Échec de création de l'exchange : {e}")
        sys.exit(1)

    ekey = exchange_key(exchange)

    # ---- Détection des bots actifs ----
    bots_info = detect_active_bots(ekey, args.exchange, args.quote)

    if bots_info:
        symbols = list(bots_info.keys())
        logger.info(f"Bots actifs détectés via lock : {', '.join(symbols)}")
        # Pour compatibilité, on transforme en dictionnaire {symbole: state_file}
        bots = {sym: info["state_file"] for sym, info in bots_info.items()}
    else:
        logger.warning("Aucun lock valide trouvé → fallback sur les fichiers state.")
        bots = detect_bots(ekey, args.quote)
        if bots:
            logger.info(f"Bots détectés via state (fallback) : {', '.join(bots.keys())}")
        else:
            logger.error("Aucun bot détecté. Vérifiez les fichiers lock ou state.")
            sys.exit(1)

    pairs_metrics = {}

    for symbol, info in bots_info.items():
        logger.info(
            f"Traitement de {symbol} "
            f"(PID={info['pid']}, lock={info['lock_file']})..."
        )

        state = load_state(info["state_file"])

        try:
            price = exchange.get_ticker_price(symbol)
            if price is None:
                logger.warning(f"Prix introuvable pour {symbol}, ignoré.")
                continue
        except Exception as e:
            logger.error(f"Erreur lors de l'obtention du prix pour {symbol} : {e}")
            continue

        try:
            metrics = compute_metrics(state, price, symbol, args.exchange)
            metrics = compute_temporal_metrics(metrics)
            pairs_metrics[symbol] = metrics
        except RuntimeError as e:
            logger.error(f"Invariant violé pour {symbol} : {e}")
            # On ignore ce bot mais on continue avec les autres
            continue

    if not pairs_metrics:
        logger.error("Aucune métrique calculée pour un bot valide.")
        sys.exit(1)

    audit_data = build_audit_metrics(args.exchange, pairs_metrics)
    write_audit_json(audit_data, ekey)

    logger.info("✅ Analyse terminée.")

if __name__ == "__main__":
    main()
