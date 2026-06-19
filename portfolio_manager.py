#!/usr/bin/env python3
"""
PORTFOLIO MANAGER V7.7 - Score sans drawdown, sans auto‑réparation
======================================================================
- Plus de drawdown dans le score (f_dd = 1)
- Plus d'auto‑réparation du wallet_peak
- Ne lit que le fichier state pour le capital (capital_usdc)
- Compatible syntaxe positionnelle des .service
======================================================================
"""

import os
import sys
import json
import re
import subprocess
import logging
import argparse
from datetime import datetime, timedelta, UTC

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────

BOTS = {
    "INJ": {
        "service": "bot_inj.service",
        "service_file": "/etc/systemd/system/bot_inj.service",
        "state_file": "state_injusdc.json",
        "symbol": "INJUSDC",
    },
    "FIL": {
        "service": "bot_fil.service",
        "service_file": "/etc/systemd/system/bot_fil.service",
        "state_file": "state_filusdc.json",
        "symbol": "FILUSDC",
    },
    "EGLD": {
        "service": "bot_egld.service",
        "service_file": "/etc/systemd/system/bot_egld.service",
        "state_file": "state_egldusdc.json",
        "symbol": "EGLDUSDC",
    },
}

TRANSFER_PCT            = 0.05
MIN_TRANSFER_USDC       = 10.0
MIN_CAPITAL_USDC        = 50.0
MIN_SCORE_DELTA         = 0.001
REALLOCATION_COOLDOWN_H = 24

WINDOWS = {
    "24h": timedelta(hours=24),
    "7j":  timedelta(days=7),
    "30j": timedelta(days=30),
}
WINDOW_WEIGHTS = {"24h": 0.50, "7j": 0.35, "30j": 0.15}

HISTORY_FILE            = "portfolio_history.jsonl"
AUDIT_METRICS_FILE      = "audit_metrics.json"
LAST_REALLOC_FILE       = "last_reallocation_v7.json"
LOG_FILE                = "portfolio_manager_v7.log"

# ──────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────
# LECTURE DES DONNÉES
# ──────────────────────────────────────────────────────────────

def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        raise FileNotFoundError(f"Fichier {HISTORY_FILE} introuvable")
    rows = []
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                row["_dt"] = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                rows.append(row)
            except Exception:
                pass
    rows.sort(key=lambda r: r["_dt"])
    logger.info(f"Historique chargé : {len(rows)} points")
    return rows

def load_current_metrics() -> dict:
    if not os.path.exists(AUDIT_METRICS_FILE):
        raise FileNotFoundError(f"Fichier {AUDIT_METRICS_FILE} introuvable")
    with open(AUDIT_METRICS_FILE) as f:
        return json.load(f)

def get_service_status(service: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"

def get_bot_health(pair: str) -> dict:
    cfg = BOTS.get(pair)
    if not cfg:
        return {}
    state_file = cfg["state_file"]
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file) as f:
            state = json.load(f)
        capital = state.get("capital_usdc", 0.0)
        return {
            "capital_usdc": capital,
            "total_pnl": state.get("total_pnl", 0.0),
            "total_trades": state.get("total_trades", 0),
            "grid_ready": state.get("grid_ready", False),
            "service_status": get_service_status(cfg["service"]),
        }
    except Exception as e:
        logger.error(f"Erreur lecture {state_file} : {e}")
        return {}

# ──────────────────────────────────────────────────────────────
# CALCUL DES PERFORMANCES PAR FENÊTRE
# ──────────────────────────────────────────────────────────────

def compute_window_performance(history: list, pair: str, window: timedelta, now: datetime) -> dict:
    cutoff = now - window
    points = [r for r in history if r.get("pair") == pair and r["_dt"] >= cutoff]
    if len(points) < 2:
        return {"valid": False, "reason": "Pas assez de points", "points": len(points)}
    points.sort(key=lambda r: r["_dt"])
    alpha_start = points[0].get("alpha_pair", 0.0)
    alpha_end   = points[-1].get("alpha_pair", 0.0)
    delta_alpha = alpha_end - alpha_start
    pnl_start = points[0].get("pnl_real", 0.0)
    pnl_end   = points[-1].get("pnl_real", 0.0)
    delta_pnl = pnl_end - pnl_start
    trades_start = points[0].get("trades", 0)
    trades_end   = points[-1].get("trades", 0)
    delta_trades = trades_end - trades_start
    days = (points[-1]["_dt"] - points[0]["_dt"]).total_seconds() / 86400
    if days < 0.01:
        days = window.total_seconds() / 86400
    activity = delta_trades / days if days > 0 else 0.0
    capital_moyen = sum(p.get("capital_usdc", 1.0) for p in points) / len(points) or 1.0
    alpha_rate = (delta_alpha / capital_moyen) / days if days > 0 else 0.0
    coverage = min(1.0, (now - points[0]["_dt"]).total_seconds() / window.total_seconds())
    return {
        "valid": True,
        "points": len(points),
        "days": days,
        "delta_alpha": delta_alpha,
        "delta_pnl": delta_pnl,
        "activity": activity,
        "alpha_rate": alpha_rate,
        "capital_moyen": capital_moyen,
        "coverage": coverage,
    }

def compute_scores(perf_by_window: dict, health: dict) -> dict:
    scores = {}
    for pair, h in health.items():
        if not h:
            scores[pair] = {"score": 0.0, "valid": False}
            continue
        total_weight = 0.0
        weighted_score = 0.0
        window_details = {}
        for wname, wdata in perf_by_window.items():
            pdata = wdata.get(pair, {})
            window_details[wname] = pdata
            if not pdata.get("valid"):
                continue
            alpha_rate = pdata["alpha_rate"]
            w = WINDOW_WEIGHTS[wname]
            activity = pdata.get("activity", 0.0)
            f_act = 0.5 + 0.5 * min(1.0, activity / 10.0)
            window_score = alpha_rate * f_act   # f_dd = 1
            weighted_score += window_score * w
            total_weight += w
        final_score = (weighted_score / total_weight) if total_weight > 0 else 0.0
        scores[pair] = {
            "score": final_score,
            "valid": total_weight > 0,
            "windows": window_details,
        }
    return scores

# ──────────────────────────────────────────────────────────────
# GESTION DES SERVICES (sans aucune réparation de wallet_peak)
# ──────────────────────────────────────────────────────────────

def read_current_budget(service_file: str) -> float | None:
    if not os.path.exists(service_file):
        return None
    with open(service_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("ExecStart="):
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        return float(parts[3])
                    except ValueError:
                        pass
    return None

def update_service_budget(service_file: str, new_budget: float, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info(f"[DRY-RUN] Mise à jour budget {service_file} → {new_budget:.2f}")
        return True
    if not os.path.exists(service_file):
        logger.error(f"Fichier service {service_file} introuvable")
        return False
    with open(service_file) as f:
        lines = f.readlines()
    modified = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("ExecStart="):
            parts = line.split()
            if len(parts) >= 4:
                parts[3] = f"{new_budget:.2f}"
                new_line = " ".join(parts)
                if new_line != line.rstrip():
                    modified = True
                new_lines.append(new_line + "\n")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    if not modified:
        return False
    tmp = f"/tmp/{os.path.basename(service_file)}.tmp"

    with open(tmp, "w") as f:
        f.writelines(new_lines)

    subprocess.run(
        ["sudo", "cp", tmp, service_file],
        check=True
    )

    os.remove(tmp)
    logger.info(f"Service mis à jour : budget → {new_budget:.2f}")
    return True

def restart_service(service: str, dry_run: bool = False):
    if dry_run:
        logger.info(f"[DRY-RUN] Redémarrage de {service}")
        return
    try:
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True, timeout=10)
        subprocess.run(["sudo", "systemctl", "restart", service], check=True, timeout=20)
        logger.info(f"Service {service} redémarré")
    except Exception as e:
        logger.error(f"Erreur redémarrage {service} : {e}")

def reset_capital_in_state(state_file: str, new_budget: float, dry_run: bool = False):
    """Met à jour capital_usdc dans le fichier state (wallet_peak inchangé)."""
    if dry_run:
        logger.info(f"[DRY-RUN] Mise à jour capital_usdc dans {state_file} → {new_budget:.2f}")
        return
    if not os.path.exists(state_file):
        logger.warning(f"State file {state_file} introuvable")
        return
    try:
        with open(state_file) as f:
            state = json.load(f)
        state["capital_usdc"] = new_budget
        # On ne touche pas à wallet_peak (le bot le recalculera au besoin)
        tmp = state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, state_file)
        logger.info(f"capital_usdc mis à jour à {new_budget:.2f} dans {state_file}")
    except Exception as e:
        logger.error(f"Erreur mise à jour capital_usdc pour {state_file} : {e}")

# ──────────────────────────────────────────────────────────────
# COOLDOWN ET RÉALLOCATION
# ──────────────────────────────────────────────────────────────

def get_last_reallocation() -> datetime | None:
    if not os.path.exists(LAST_REALLOC_FILE):
        return None
    try:
        with open(LAST_REALLOC_FILE) as f:
            data = json.load(f)
        return datetime.fromisoformat(data["timestamp"])
    except Exception:
        return None

def save_last_reallocation(now: datetime, reco: dict):
    try:
        with open(LAST_REALLOC_FILE, "w") as f:
            json.dump({"timestamp": now.isoformat(), "reco": reco}, f, indent=2)
        logger.info(f"Cooldown enregistré : {now.isoformat()}")
    except Exception as e:
        logger.error(f"Erreur écriture {LAST_REALLOC_FILE} : {e}")

def compute_reallocation(scores: dict, current_metrics: dict, now: datetime) -> dict:
    pairs = list(scores.keys())
    if len(pairs) < 2:
        return {"action": "SKIP", "reason": "Moins de 2 paires"}

    last = get_last_reallocation()
    if last:
        elapsed_h = (now - last).total_seconds() / 3600
        if elapsed_h < REALLOCATION_COOLDOWN_H:
            return {"action": "HOLD", "reason": f"Cooldown ({elapsed_h:.1f}h < {REALLOCATION_COOLDOWN_H}h)"}

    ranked = sorted(pairs, key=lambda p: scores[p]["score"], reverse=True)
    winner = ranked[0]
    loser = ranked[-1]
    score_winner = scores[winner]["score"]
    score_loser  = scores[loser]["score"]
    delta = score_winner - score_loser

    if delta < MIN_SCORE_DELTA:
        return {"action": "HOLD", "reason": f"Écart trop faible ({delta:.6f})", "winner": winner, "loser": loser, "delta": delta}
    if score_winner <= 0:
        return {"action": "HOLD", "reason": f"Meilleur score non positif ({score_winner:.4f})", "winner": winner, "loser": loser, "score_winner": score_winner}

    cfg_winner = BOTS.get(winner)
    cfg_loser  = BOTS.get(loser)
    if not cfg_winner or not cfg_loser:
        return {"action": "SKIP", "reason": "Configuration BOTS manquante"}

    budget_winner = read_current_budget(cfg_winner["service_file"])
    budget_loser  = read_current_budget(cfg_loser["service_file"])
    if budget_winner is None:
        budget_winner = current_metrics["pairs"].get(winner, {}).get("capital_usdc", 100.0)
    if budget_loser is None:
        budget_loser = current_metrics["pairs"].get(loser, {}).get("capital_usdc", 100.0)

    transfer_raw = round(budget_loser * TRANSFER_PCT, 2)
    transfer = transfer_raw
    reason_extra = ""
    if budget_loser - transfer < MIN_CAPITAL_USDC:
        transfer = max(0.0, budget_loser - MIN_CAPITAL_USDC)
        reason_extra = f" (limité par capital minimum)"
    if transfer < MIN_TRANSFER_USDC:
        return {
            "action": "HOLD",
            "reason": f"Transfert trop petit ({transfer:.2f}$ < {MIN_TRANSFER_USDC}$){reason_extra}",
            "winner": winner,
            "loser": loser,
            "transfer_computed": transfer_raw,
            "budget_loser": budget_loser,
            "budget_winner": budget_winner,
        }

    new_budget_winner = round(budget_winner + transfer, 2)
    new_budget_loser  = round(budget_loser - transfer, 2)

    return {
        "action": "REALLOCATE",
        "winner": winner,
        "loser": loser,
        "score_winner": score_winner,
        "score_loser": score_loser,
        "delta": delta,
        "budget_winner_before": budget_winner,
        "budget_loser_before": budget_loser,
        "transfer_usdc": transfer,
        "new_budget_winner": new_budget_winner,
        "new_budget_loser": new_budget_loser,
        "reason": f"Surperformance de {winner} (delta={delta:.6f})"
    }

def execute_reallocation(reco: dict, dry_run: bool, now: datetime) -> bool:
    if reco["action"] != "REALLOCATE":
        return False
    winner = reco["winner"]
    loser = reco["loser"]
    cfg_w = BOTS[winner]
    cfg_l = BOTS[loser]

    ok_w = update_service_budget(cfg_w["service_file"], reco["new_budget_winner"], dry_run)
    ok_l = update_service_budget(cfg_l["service_file"], reco["new_budget_loser"], dry_run)
    if not (ok_w or ok_l):
        return False

    # Mise à jour du capital_usdc dans les states (sans toucher au wallet_peak)
    reset_capital_in_state(cfg_w["state_file"], reco["new_budget_winner"], dry_run)
    reset_capital_in_state(cfg_l["state_file"], reco["new_budget_loser"], dry_run)

    if dry_run:
        logger.info("[DRY-RUN] Réallocation effectuée (simulation)")
        return True

    try:
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True, timeout=10)
        subprocess.run(["sudo", "systemctl", "restart", cfg_w["service"]], check=True, timeout=20)
        subprocess.run(["sudo", "systemctl", "restart", cfg_l["service"]], check=True, timeout=20)
        logger.info(f"Services redémarrés : {cfg_w['service']}, {cfg_l['service']}")
    except Exception as e:
        logger.error(f"Erreur redémarrage : {e}")
        return False

    save_last_reallocation(now, reco)
    logger.info(f"Réallocation : {reco['transfer_usdc']:.2f}$ de {loser} vers {winner}")
    return True

# ──────────────────────────────────────────────────────────────
# AFFICHAGE
# ──────────────────────────────────────────────────────────────

def fmt_rate(r):
    if r is None:
        return "  n/a      "
    sign = "+" if r >= 0 else ""
    return f"{sign}{r * 100:.4f}%cap/j"

def print_detailed_report(scores: dict, perf_by_window: dict, health: dict, reco: dict, now: datetime):
    W = 70
    SEP = "═" * W
    sep = "─" * W

    print(f"\n{SEP}")
    print(f"  PORTFOLIO MANAGER V7.7  ·  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(SEP)

    print(f"\n  📐 POIDS FENÊTRES [static]")
    for wname, w in WINDOW_WEIGHTS.items():
        print(f"     {wname:<4} : {w:.1%}")
    print(sep)

    for pair, s in scores.items():
        h = health.get(pair, {})
        if not h:
            continue
        tag = "✅" if s["valid"] else "❌"
        capital = h.get("capital_usdc", 0.0)
        total_pnl = h.get("total_pnl", 0.0)
        total_trades = h.get("total_trades", 0)
        act_24h = perf_by_window.get("24h", {}).get(pair, {}).get("activity", 0.0)
        f_act = 0.5 + 0.5 * min(1.0, act_24h / 10.0)
        print(f"\n  {tag} {pair}  🟢 {h.get('service_status','?')}      Score : {fmt_rate(s['score'])}")
        print(f"     Capital={capital:.2f}$  PnL={total_pnl:+.4f}$  Trades={total_trades}  f_act={f_act:.2f}")
        print(sep)
        for wname in ["24h", "7j", "30j"]:
            wdata = s["windows"].get(wname, {})
            if wdata.get("valid"):
                coverage = wdata.get("coverage", 0.0)
                if coverage < 0.9:
                    print(f"    {wname:<4}  {'--':>14}   [Couverture insuffisante ({coverage:.0%} < 90%)]")
                else:
                    alpha_rate = wdata.get("alpha_rate", 0.0)
                    delta_alpha = wdata.get("delta_alpha", 0.0)
                    points = wdata.get("points", 0)
                    delta_pnl = wdata.get("delta_pnl", 0.0)
                    activity = wdata.get("activity", 0.0)
                    print(f"    {wname:<4}  α_rate={alpha_rate:+.5f}/j  Δα={delta_alpha:+.3f}  pts={points}  pnl={delta_pnl:+.2f}$  act={activity:.0f}t/j")
            else:
                reason = wdata.get("reason", "Données insuffisantes")
                print(f"    {wname:<4}  {'--':>14}   [{reason}]")
        if not s["valid"]:
            print(f"    ⚠️  Aucune fenêtre valide")

    print(f"\n{SEP}")
    print("  DÉCISION DE RÉALLOCATION")
    print(sep)
    action = reco["action"]
    if action == "REALLOCATE":
        print(f"  ACTION    : RÉALLOUER")
        print(f"  Bot fort  : {reco['winner']}  ({fmt_rate(reco.get('score_winner'))})")
        print(f"  Bot faible: {reco['loser']}  ({fmt_rate(reco.get('score_loser'))})")
        if reco.get("delta"):
            print(f"  Écart     : {reco['delta'] * 100:.4f}%/j")
        print(sep)
        print(f"  Transférer : {reco['transfer_usdc']:.2f}$ ({TRANSFER_PCT*100:.1f}%)")
        print(f"  {reco['loser']:<10}: {reco['budget_loser_before']:.2f}$  →  {reco['new_budget_loser']:.2f}$")
        print(f"  {reco['winner']:<10}: {reco['budget_winner_before']:.2f}$  →  {reco['new_budget_winner']:.2f}$")
    elif action == "HOLD":
        print(f"  ACTION  : CONSERVER")
        print(f"  Raison  : {reco['reason']}")
        if "transfer_computed" in reco:
            print(f"  (transfert hypothétique : {reco['transfer_computed']:.2f}$)")
        if "winner" in reco:
            print(f"  Meilleur: {reco['winner']}  ({fmt_rate(reco.get('score_winner'))})")
            print(f"  Faible  : {reco['loser']}   ({fmt_rate(reco.get('score_loser'))})")
    else:
        print(f"  ACTION  : IGNORÉ")
        print(f"  Raison  : {reco['reason']}")
    print(SEP)

# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Portfolio Manager V7.7 - Sans drawdown ni auto‑réparation")
    parser.add_argument("--dry-run", action="store_true", help="Simuler sans modification")
    parser.add_argument("--transfer-pct", type=float, default=TRANSFER_PCT, help="Pourcentage à transférer")
    parser.add_argument("--min-delta", type=float, default=MIN_SCORE_DELTA, help="Écart minimum de score")
    return parser.parse_args()

if __name__ == "__main__":
    setup_logging()
    args = parse_args()
    TRANSFER_PCT = args.transfer_pct
    MIN_SCORE_DELTA = args.min_delta

    if args.dry_run:
        logger.info("=== MODE DRY-RUN (aucune modification réelle) ===")

    try:
        now = datetime.now(UTC)
        history = load_history()
        metrics = load_current_metrics()
        pairs = list(metrics["pairs"].keys())
        if not pairs:
            raise ValueError("Aucune paire trouvée dans audit_metrics.json")

        health = {}
        for pair in pairs:
            health[pair] = get_bot_health(pair)

        perf_by_window = {}
        for wname, wdelta in WINDOWS.items():
            perf_by_window[wname] = {}
            for pair in pairs:
                perf_by_window[wname][pair] = compute_window_performance(history, pair, wdelta, now)

        scores = compute_scores(perf_by_window, health)
        reco = compute_reallocation(scores, metrics, now)

        print_detailed_report(scores, perf_by_window, health, reco, now)

        if reco["action"] == "REALLOCATE":
            execute_reallocation(reco, args.dry_run, now)

    except Exception as e:
        logger.exception(f"Erreur fatale : {e}")
        sys.exit(1)
