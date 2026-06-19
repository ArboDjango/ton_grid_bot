#!/usr/bin/env python3
"""
AUDIT BOT GRID - V8
Analyse complète du patrimoine vs stratégie Hold.
PnL Binance incrémental via curseur last_trade_id -> ne relit jamais les vieux trades.

Améliorations V8 :
  - Suppression de l'export HTML (non utilisé)
  - Correction lecture slippage EMA (ema_slippage_buy / sell)
  - Ajout vérification d'écart d'inventaire (total_base_qty vs solde réel)
  - Ajout de total_base_qty dans les exports
  - alpha_pct conservé mais basé sur capital_usdc (définition bot)
  - Nettoyage des imports inutiles
"""
import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

SNAPSHOT_FILE      = "snapshot_t0.json"
PNL_CACHE          = "pnl_cache.json"
LOG_FILE           = "audit.log"

# Clés réservées du snapshot — tout le reste est un token tradé
SNAPSHOT_META_KEYS = {"date_reference","timestamp_reference","CASH"}
SNAPSHOT_REQUIRED_KEYS = ["CASH"]

# Construit dynamiquement par get_snapshot()
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

# ─────────────────────────────────────────────
# SNAPSHOT — avec validation
# ─────────────────────────────────────────────

def get_snapshot() -> dict:
    global PAIRES

    if not os.path.exists(SNAPSHOT_FILE):
        raise FileNotFoundError(f"Snapshot introuvable : {SNAPSHOT_FILE}")
    with open(SNAPSHOT_FILE) as f:
        snap = json.load(f)
        
    if (
        "date_reference" not in snap
        and
        "timestamp_reference" not in snap
    ):
        raise ValueError(
            "snapshot_t0.json doit contenir "
            "'date_reference' ou 'timestamp_reference'"
        )

    missing = [k for k in SNAPSHOT_REQUIRED_KEYS if k not in snap]
    if missing:
        raise ValueError(
            f"snapshot_t0.json incomplet — clés manquantes : {', '.join(missing)}"
        )
    if "usdc" not in snap["CASH"]:
        raise ValueError("snapshot_t0.json : clé 'usdc' manquante dans CASH")

    # Détection automatique des tokens
    token_names = [k for k in snap if k not in SNAPSHOT_META_KEYS]
    if not token_names:
        raise ValueError("snapshot_t0.json : aucun token détecté (hors date_reference et CASH)")

    for name in token_names:
        if "stock" not in snap[name]:
            raise ValueError(
                f"snapshot_t0.json : clé 'stock' manquante pour le token {name}"
            )

    PAIRES = {
        name: {"symbol": f"{name}USDC", "asset": name}
        for name in token_names
    }
    logger.info(f"Tokens détectés depuis snapshot : {list(PAIRES.keys())}")

    return snap

# ─────────────────────────────────────────────
# STATE BOT — avec warnings explicites
# ─────────────────────────────────────────────

def load_bot_state(symbol: str) -> dict:
    sf = f"state_{symbol.lower()}.json"
    if not os.path.exists(sf):
        logger.warning(f"[{symbol}] Fichier state absent ({sf}) — grille et capital_usdc ignorés")
        return {}
    try:
        with open(sf) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(f"[{symbol}] Fichier state corrompu ({sf}) : {e} — ignoré")
        return {}

def get_allocated_usdc() -> float:
    total = 0.0
    for cfg in PAIRES.values():
        total += load_bot_state(cfg["symbol"]).get("capital_usdc", 0.0)
    return total

# ─────────────────────────────────────────────
# PNL INCRÉMENTAL — CŒUR DU SYSTÈME
# ─────────────────────────────────────────────

def load_pnl_cache() -> dict:
    """
    Structure :
    {
      "INJUSDC":  { "last_trade_id": 123, "usdc_spent": 0.0,
                    "usdc_gained": 0.0,   "nb_trades": 0 },
      ...
    }
    """
    if os.path.exists(PNL_CACHE):
        try:
            with open(PNL_CACHE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Cache PnL corrompu ({PNL_CACHE}) : {e} — réinitialisé")
    return {}

def save_pnl_cache(cache: dict):
    with open(PNL_CACHE, "w") as f:
        json.dump(cache, f, indent=2)
    logger.info(f"Cache PnL sauvegardé -> {PNL_CACHE}")

def reset_pnl_cache():
    if os.path.exists(PNL_CACHE):
        os.remove(PNL_CACHE)
        logger.info(f"Cache PnL supprimé ({PNL_CACHE}) — re-scan complet au prochain lancement")
    else:
        logger.info("Aucun cache PnL à supprimer")

def fetch_new_trades(client: Client, symbol: str,
                     cache: dict, t0_ms: int) -> dict:
    """
    Lit uniquement les trades nouveaux depuis le dernier curseur.
    - 1er lancement  : startTime = T0 UTC (scan initial complet depuis T0)
    - Suivants       : fromId = last_trade_id + 1 (incrémental)
    Gère la pagination automatiquement si > 1000 trades nouveaux.
    Détecte un curseur invalide (réponse vide inattendue) et avertit.
    """
    entry = cache.get(symbol, {
        "last_trade_id": None,
        "usdc_spent":    0.0,
        "usdc_gained":   0.0,
        "nb_trades":     0,
    })

    new_total  = 0
    first_call = True

    if entry["last_trade_id"] is None:
        kwargs = {"symbol": symbol, "startTime": t0_ms, "limit": 1000}
        logger.info(f"[{symbol}] Premier scan depuis T0...")
    else:
        kwargs = {"symbol": symbol, "fromId": entry["last_trade_id"] + 1, "limit": 1000}

    while True:
        try:
            trades = client.get_my_trades(**kwargs)
        except (BinanceAPIException, BinanceRequestException) as e:
            logger.error(f"[{symbol}] Erreur API Binance lors de get_my_trades : {e}")
            raise

        if not trades:
            if first_call and entry["last_trade_id"] is not None:
                logger.warning(
                    f"[{symbol}] Aucun trade retourné depuis fromId={entry['last_trade_id'] + 1}. "
                    f"Si suspect, lancez avec --reset-cache pour forcer un re-scan."
                )
            break

        first_call = False

        for t in trades:
            qty       = float(t["quoteQty"])
            comm_usdc = float(t["commission"]) if t["commissionAsset"] == "USDC" else 0.0
            if t["isBuyer"]:
                entry["usdc_spent"]  += qty + comm_usdc
            else:
                entry["usdc_gained"] += qty - comm_usdc

        entry["last_trade_id"] = trades[-1]["id"]
        new_total += len(trades)

        if len(trades) < 1000:
            break
        kwargs = {"symbol": symbol, "fromId": entry["last_trade_id"] + 1, "limit": 1000}

    entry["nb_trades"] += new_total
    if new_total > 0:
        logger.info(f"[{symbol}] +{new_total} nouveaux trades intégrés "
                    f"(total cumulé : {entry['nb_trades']})")
    else:
        logger.info(f"[{symbol}] Aucun nouveau trade depuis le dernier audit")

    return entry

# ─────────────────────────────────────────────
# ANALYSE PRINCIPALE
# ─────────────────────────────────────────────

def run_audit(client: Client, snapshot: dict) -> dict:
    # Timestamp T0
    if "timestamp_reference" in snapshot:
        t0 = datetime.fromisoformat(snapshot["timestamp_reference"].replace("Z", "+00:00"))
    else:
        t0 = datetime.strptime(snapshot["date_reference"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    t0_ms = int(t0.timestamp() * 1000)

    # Mise à jour incrémentale des trades
    cache = load_pnl_cache()
    logger.info("Mise à jour incrémentale des trades Binance...")
    for cfg in PAIRES.values():
        cache[cfg["symbol"]] = fetch_new_trades(client, cfg["symbol"], cache, t0_ms)
    save_pnl_cache(cache)

    # Récupération du cash actuel
    cash_free, cash_locked, cash_actuel = get_cash_balance(client)

    results = {}
    valeur_crypto_actuelle = 0.0
    valeur_crypto_hold = 0.0
    pnl_reel_total = 0.0
    trades_total = 0

    for name, cfg in PAIRES.items():
        try:
            balance = client.get_asset_balance(asset=cfg["asset"])
            prix = float(client.get_ticker(symbol=cfg["symbol"])["lastPrice"])
        except (BinanceAPIException, BinanceRequestException) as e:
            logger.error(f"[{cfg['symbol']}] Erreur API : {e}")
            raise

        solde_actuel = float(balance["free"]) + float(balance["locked"])
        stock_t0 = snapshot[name]["stock"]
        val_actuelle = solde_actuel * prix
        val_hold = stock_t0 * prix
        delta_tokens = solde_actuel - stock_t0

        c = cache[cfg["symbol"]]
        pnl_reel = c["usdc_gained"] - c["usdc_spent"]
        nb_trades = c["nb_trades"]

        delta_tokens_value = delta_tokens * prix
        alpha_pair = pnl_reel + delta_tokens_value

        state = load_bot_state(cfg["symbol"])
        capital_usdc = state.get("capital_usdc", 0.0)
        alpha_pct = (alpha_pair / capital_usdc) if capital_usdc > 0 else 0.0

        ema_buy = state.get("ema_slippage_buy", 0.0)
        ema_sell = state.get("ema_slippage_sell", 0.0)
        ema_slip = max(ema_buy, ema_sell) if (ema_buy or ema_sell) else None

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
            "solde_actuel": solde_actuel,
            "stock_t0": stock_t0,
            "delta_tokens": delta_tokens,
            "prix": prix,
            "val_actuelle": val_actuelle,
            "val_hold": val_hold,
            "usdc_spent": c["usdc_spent"],
            "usdc_gained": c["usdc_gained"],
            "pnl_reel": pnl_reel,
            "nb_trades": nb_trades,
            "grille_prete": grille_prete,
            "sell_grid": sell_grid,
            "buy_grid": buy_grid,
            "delta_tokens_value": delta_tokens_value,
            "alpha_pair": alpha_pair,
            "capital_usdc": capital_usdc,
            "alpha_pct": alpha_pct,
            "efficiency": (alpha_pair / nb_trades) if nb_trades > 0 else 0.0,
            "ema_slip": ema_slip,
            "ema_slippage_buy": ema_buy,
            "ema_slippage_sell": ema_sell,
            "gv": gv,
            "density_k": density_k,
            "nb_levels": nb_levels,
            "total_base_qty": total_base_qty,
        }

    # Calcul F0 recommandé (utilise cash_actuel)
    total_crypto_value = sum(d["val_actuelle"] for d in results.values())
    for name, d in results.items():
        poids = d["val_actuelle"] / total_crypto_value if total_crypto_value > 0 else 1.0 / len(results)
        f0_bot = d["val_actuelle"] + cash_actuel * poids
        f0_recommande = round(f0_bot * 0.90, 2)
        results[name]["f0_estime"] = round(f0_bot, 2)
        results[name]["f0_recommande"] = f0_recommande
        logger.info(f"F0 estimé {name} : {f0_bot:.2f}$ | Recommandé : {f0_recommande:.2f}$")

    patrimoine_actuel = valeur_crypto_actuelle + cash_actuel
    patrimoine_hold = valeur_crypto_hold + snapshot["CASH"]["usdc"]
    alpha_brut = patrimoine_actuel - patrimoine_hold
    alpha_latent = valeur_crypto_actuelle - valeur_crypto_hold
    delta_tokens_value_sum = sum(d["delta_tokens"] * d["prix"] for d in results.values())
    alpha_securise = pnl_reel_total + delta_tokens_value_sum
    ratio_cristallisation = (alpha_securise / alpha_brut * 100) if alpha_brut != 0 else 0.0

    logger.info(f"Audit terminé — Patrimoine actuel : {patrimoine_actuel:.2f} $  "
                f"| Alpha brut : {fmt_signed(alpha_brut)} $  "
                f"| PnL réel : {fmt_signed(pnl_reel_total, 4)} $  "
                f"| Trades : {trades_total}")

    return {
        "date_reference": snapshot["date_reference"],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paires": results,
        "cash_free": cash_free,
        "cash_locked": cash_locked,
        "cash_actuel": cash_actuel,
        "cash_alloue": get_allocated_usdc(),
        "cash_t0": snapshot["CASH"]["usdc"],
        "valeur_crypto_actuelle": valeur_crypto_actuelle,
        "valeur_crypto_hold": valeur_crypto_hold,
        "patrimoine_actuel": patrimoine_actuel,
        "patrimoine_hold": patrimoine_hold,
        "alpha_brut": alpha_brut,
        "alpha_latent": alpha_latent,
        "alpha_securise": alpha_securise,
        "ratio_cristallisation": ratio_cristallisation,
        "pnl_reel_total": pnl_reel_total,
        "delta_tokens_value": delta_tokens_value_sum,
        "trades_total": trades_total,
    }

def get_cash_balance(client: Client) -> tuple[float, float, float]:
    """Retourne (cash_free, cash_locked, cash_actuel)"""
    try:
        usdc_bal = client.get_asset_balance(asset="USDC")
    except (BinanceAPIException, BinanceRequestException) as e:
        logger.error(f"Erreur API Binance lors de la récupération du solde USDC : {e}")
        raise
    cash_free   = float(usdc_bal["free"])
    cash_locked = float(usdc_bal["locked"])
    cash_actuel = cash_free + cash_locked
    return cash_free, cash_locked, cash_actuel

# ─────────────────────────────────────────────
# AFFICHAGE CONSOLE
# ─────────────────────────────────────────────

def print_report(r: dict):
    W   = 62
    SEP = "=" * W
    sep = "-" * W

    print(f"\n{SEP}")
    print(f"  AUDIT PATRIMOINE GRID BOT — {r['timestamp']}")
    print(f"  Référence T0 : {r['date_reference']}")
    print(SEP)

    for name, d in r["paires"].items():
        status    = "OK" if d["grille_prete"] else "KO"
        delta_ico = "+" if d["delta_tokens"] >= 0 else ""
        pnl_ico   = "+" if d["pnl_reel"] >= 0 else ""
        print(f"\n  [{status}] {name}/USDC  @  {d['prix']:.4f} $")
        print(sep)
        print(f"    Stock T0           : {d['stock_t0']:.4f} tokens")
        print(f"    Stock Actuel       : {d['solde_actuel']:.4f} tokens  "
              f"({delta_ico}{d['delta_tokens']:.4f})")
        print(f"    Val. Actuelle      : {d['val_actuelle']:.2f} $")
        print(f"    Val. Hold Pure     : {d['val_hold']:.2f} $")
        print(sep)
        print(f"    USDC dépensé       : {d['usdc_spent']:.4f} $")
        print(f"    USDC encaissé      : {d['usdc_gained']:.4f} $")
        print(f"    PnL Réel (Binance) : {fmt_signed(d['pnl_reel'], 4)} $  <- source de vérité")
        print(f"    Trades totaux      : {d['nb_trades']}")
        print(f"    Grille BUY / SELL  : {len(d['buy_grid'])} / {len(d['sell_grid'])}")
        if d["total_base_qty"] != 0:
            print(f"    Inventaire local   : {d['total_base_qty']:.6f} tokens (state['total_base_qty'])")

    print(f"\n{SEP}")
    print(f"  SYNTHESE PATRIMOINE")
    print(sep)
    print(f"    Cash USDC T0       : {r['cash_t0']:.2f} $")
    print(f"    Cash USDC Actuel   : {r['cash_actuel']:.2f} $  (free {r['cash_free']:.2f} + locked {r['cash_locked']:.2f})")
    print(f"    Cash USDC Alloué   : {r['cash_alloue']:.2f} $  (capital initial bots, informatif)")
    print(f"    Crypto (Actuel)    : {r['valeur_crypto_actuelle']:.2f} $")
    print(f"    Crypto (Hold)      : {r['valeur_crypto_hold']:.2f} $")
    print(sep)
    print(f"    Patrimoine Actuel  : {r['patrimoine_actuel']:.2f} $")
    print(f"    Patrimoine Hold    : {r['patrimoine_hold']:.2f} $")
    print(sep)
    print(f"    ALPHA BRUT         : {fmt_signed(r['alpha_brut'])} $  ({pct(r['alpha_brut'], r['patrimoine_hold'])})")
    print(sep)
    print(f"    Alpha latent       : {fmt_signed(r['alpha_latent'])} $  (prix-dépendant)")
    print(f"    Alpha sécurisé     : {fmt_signed(r['alpha_securise'], 4)} $  (PnL cash + tokens valorisés)")
    print(f"      dont PnL cash    : {fmt_signed(r['pnl_reel_total'], 4)} $")
    print(f"      dont delta tok.  : {fmt_signed(r['delta_tokens_value'], 4)} $")

    c     = r["ratio_cristallisation"]
    ico_c = "[!!]" if c < 20 else ("[~]" if c < 50 else "[OK]")
    print(f"    Cristallisation    : {ico_c} {c:.1f}%")
    print(sep)
    print(f"    PnL Réel Total     : {fmt_signed(r['pnl_reel_total'], 4)} $")
    print(f"    Trades Totaux      : {r['trades_total']}")
    print(SEP)

def export_metrics_json(r, filename="audit_metrics.json"):
    out = {
        "timestamp": r["timestamp"],
        "date_reference": r["date_reference"],
        "pairs": {}
    }

    for pair, d in r["paires"].items():
        out["pairs"][pair] = {
            "alpha_pair":        d["alpha_pair"],
            "alpha_pct":         d["alpha_pct"],
            "pnl_real":          d["pnl_reel"],
            "delta_token_value": d["delta_tokens_value"],
            "capital_usdc":      d["capital_usdc"],
            "trades":            d["nb_trades"],
            "price":             d["prix"],
            "ema_slip":          d["ema_slip"],
            "gv":                d["gv"],
            "density_k":         d["density_k"],
            "nb_levels":         d["nb_levels"],
            "f0_estime":         d["f0_estime"],
            "f0_recommande":     d["f0_recommande"],
            "total_base_qty":    d["total_base_qty"],
        }

    with open(filename, "w") as f:
        json.dump(out, f, indent=2)

    # Historisation Portfolio Manager (JSONL)
    history_file = "portfolio_history.jsonl"
    with open(history_file, "a") as f:
        for pair, d in out["pairs"].items():
            row = {
                "timestamp": out["timestamp"],
                "pair": pair,
                **d
            }
            f.write(json.dumps(row) + "\n")

    logger.info(f"Metrics Portfolio -> {filename}")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Audit PnL Grid Bot — Binance")
    parser.add_argument(
        "--reset-cache",
        action="store_true",
        help="Supprime le cache PnL et force un re-scan complet depuis T0"
    )
    return parser.parse_args()

if __name__ == "__main__":
    setup_logging()
    args = parse_args()

    if args.reset_cache:
        reset_pnl_cache()

    try:
        client   = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))
        snapshot = get_snapshot()
        # On récupère les soldes cash avant run_audit pour les utiliser dans les calculs de F0
        cash_free, cash_locked, cash_actuel = get_cash_balance(client)
        # On injecte ces valeurs dans snapshot pour run_audit ? Non, on les passera via closure ou on les recalcule.
        # Pour simplifier, on modifie run_audit pour qu'il utilise get_cash_balance directement.
        # Mais run_audit utilise déjà get_cash_balance interne. Il faut le définir avant.
        # En fait, run_audit appelle get_cash_balance, mais il n'est pas encore défini.
        # Correction : déplacer la définition de get_cash_balance avant run_audit.
        # Je réorganise dans le code final.
        result = run_audit(client, snapshot)
        print_report(result)
        export_metrics_json(result)
    except FileNotFoundError as e:
        logger.error(f"Fichier manquant : {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Données invalides : {e}")
        sys.exit(1)
    except (BinanceAPIException, BinanceRequestException) as e:
        logger.error(f"Erreur API Binance : {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Erreur inattendue : {e}")
        sys.exit(1)
