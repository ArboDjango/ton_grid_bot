#!/usr/bin/env python3
"""
PORTFOLIO MANAGER V9.5 (V107) — Correctifs age pnl_bot + header version
============================================================================
Changements par rapport à V9.3 (V105) :

  1. [BUGFIX] compute_window_performance — âge pnl_bot calculé globalement.

     PROBLÈME (V105) : la vérification `pnl_bot_age_hours < 24` utilisait
     `oldest = min(_dt for pnl_bot points in window)`, soit le point le plus
     vieux DANS la fenêtre courante. Pour la fenêtre 24h, ce point ne peut
     jamais avoir plus de 24h d'âge par construction (la fenêtre ne couvre
     que [now-24h, now]), donc `pnl_bot_age_hours < 24` était TOUJOURS vrai.
     Résultat : la fenêtre 24h était définitivement invalide quelle que soit
     la durée réelle de l'historique pnl_bot disponible.

     CORRECTION (V106) : `pnl_bot_age_hours` est maintenant calculé depuis
     le point pnl_bot le plus ancien de toute la paire dans `history`
     (non limité à la fenêtre courante). Une fois 24h de données pnl_bot
     accumulées globalement, toutes les fenêtres (24h, 7j, 30j) peuvent
     devenir valides dès que les autres conditions sont remplies.

  2. [BUGFIX] Header d'affichage : "V9.2" corrigé en "V9.4".

Changements par rapport à V9.2 (V104) :

  4. [SCORING] Remplacement de pnl_real par pnl_bot comme métrique de scoring.
     pnl_bot = state["total_pnl"] = somme cumulée des profits des round-trips.
     Propriétés de pnl_bot vs pnl_real :
       * Toujours >= 0 quand la grille fonctionne (pas de biais directionnel)
       * Ne dépend pas des achats d'inventaire ni du sens du marché
       * Mesure exactement l'edge de market-making (round-trips complets)
     -> compute_window_performance calcule delta_pnl_bot = pnl_bot_end - pnl_bot_start
        (fallback sur alpha_pair pour les lignes JSONL historiques sans pnl_bot)
     -> pnl_rate et vol_pnl sont désormais basés sur pnl_bot (et non pnl_real).
     -> delta_pnl (pnl_real-based) est conservé dans le return dict pour
        l'affichage et la vérification croisée.

Changements par rapport à V9.1 (V103) :

  1. [SCORING] Le score Sharpe-like est maintenant calculé sur pnl_bot
     (voir point 4 ci-dessus, évolution de l'approche pnl_reel de V104).

  2. [SIZING] Remplacement du TRANSFER_PCT fixe et de _dynamic_transfer_pct
     par un sizing Quarter-Kelly :
       f* = pnl_rate_winner / vol_pnl_winner²   (full Kelly)
       transfer_pct = clamp(f*/4, KELLY_MIN_PCT, KELLY_MAX_PCT)
     Kelly pondéré par la moyenne effective des fenêtres valides.
     Si les données sont insuffisantes pour Kelly (vol_pnl indéfini
     ou pnl_rate <= 0), fallback sur KELLY_MIN_PCT.

  3. [EXPLORATION] Remplacement du gate binaire MIN_TOTAL_WEIGHT_FOR_DECISION
     par un terme UCB continu ajouté au score avant ranking :
       ucb_bonus = UCB_C * sqrt(log(N_total) / max(1, n_pair))
     où n_pair = points dans la fenêtre 30j, N_total = total tous bots.
     -> Un nouveau bot obtient un bonus d'exploration décroissant
        naturellement à mesure que l'historique se remplit, au lieu
        d'être bloqué par un seuil binaire.
     -> Le transfer pour ce bot reste limité par Kelly (faible pnl_rate
        ou vol_pnl élevé = Kelly conservateur = petit transfert).
     -> raw_score (sans UCB) et ucb_bonus sont exposés séparément.

Invariants conservés de V103 :
  - Rollback service file si mise à jour partielle
  - Plafond de concentration MAX_PAIR_WEIGHT
  - Correction tz-aware pour last_reallocation_v7.json hérité
  - _risk_factor (pénalité drawdown)
  - vol_target (1/(1+vol_pnl)) dans le score
============================================================================
"""

import os
import sys
import json
import math
import subprocess
import logging
import argparse
from datetime import datetime, timedelta
from statistics import stdev

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
    "STX": {
        "service": "bot_stx.service",
        "service_file": "/etc/systemd/system/bot_stx.service",
        "state_file": "state_stxusdc.json",
        "symbol": "STXUSDC",
    },
}

MIN_TRANSFER_USDC       = 10.0
MIN_CAPITAL_USDC        = 50.0
MIN_SCORE_DELTA         = 0.001
REALLOCATION_COOLDOWN_H = 24
MAX_PAIR_WEIGHT         = 0.40   # plafond de concentration

# ── Quarter-Kelly sizing ───────────────────────────────────────
# transfer_pct = clamp(pnl_rate_winner / (4 * vol_pnl_winner²), MIN, MAX)
KELLY_MIN_PCT = 0.02   # transfert minimum si Kelly donne trop peu
KELLY_MAX_PCT = 0.15   # transfert maximum quelle que soit la confiance

# ── UCB exploration bonus ──────────────────────────────────────
# ucb_bonus = UCB_C * sqrt(log(N_total) / n_pair)
# Règle empirique : UCB_C ~ 0.5 * magnitude_typique_raw_score
# Ajustable selon l'agressivité d'exploration souhaitée pour les nouveaux bots
UCB_C = 0.30

SORTINO_VOL_FLOOR = 0.02

WINDOWS = {
    "24h": timedelta(hours=24),
    "7j":  timedelta(days=7),
    "30j": timedelta(days=30),
}
WINDOW_WEIGHTS = {"24h": 0.50, "7j": 0.35, "30j": 0.15}

MIN_POINTS_PER_WINDOW         = 2
MIN_WINDOW_COVERAGE           = 0.05

RISK_PENALTY_ENABLED  = True
RISK_PENALTY_STRENGTH = 1.0

HISTORY_FILE            = "portfolio_history.jsonl"
AUDIT_METRICS_FILE      = "audit_metrics.json"
LAST_REALLOC_FILE       = "last_reallocation_v7.json"
LOG_FILE                = "portfolio_manager_v107.log"

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
                row["_dt"] = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
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
# OUTILS DE CALCUL
# ──────────────────────────────────────────────────────────────

def _time_weighted_mean(points: list, key: str, treat_zero_as_missing: bool = False):
    vals = []
    for p in points:
        v = p.get(key)
        if v is None:
            continue
        if treat_zero_as_missing and v <= 0:
            continue
        vals.append((p["_dt"], v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0][1]
    weighted_sum = 0.0
    total_weight = 0.0
    for i in range(len(vals) - 1):
        dt = (vals[i + 1][0] - vals[i][0]).total_seconds()
        if dt <= 0:
            continue
        weighted_sum += vals[i][1] * dt
        total_weight += dt
    if total_weight <= 0:
        return sum(v for _, v in vals) / len(vals)
    return weighted_sum / total_weight

def _detect_counter_reset(points: list, key: str = "trades") -> bool:
    prev = None
    for p in points:
        v = p.get(key, 0)
        if prev is not None and v < prev:
            return True
        prev = v
    return False

def _compute_drawdown(points: list) -> float | None:
    capitals = [p.get("capital_usdc") for p in points if p.get("capital_usdc") is not None]
    if not capitals:
        return None
    max_cap = max(capitals)
    if max_cap == 0:
        return None
    dd_series = [max(0.0, 1.0 - c / max_cap) for c in capitals]
    return sum(dd_series) / len(dd_series)

def _risk_factor(drawdown_pct):
    if not RISK_PENALTY_ENABLED or drawdown_pct is None:
        return 1.0
    return max(0.1, 1.0 - RISK_PENALTY_STRENGTH * max(0.0, drawdown_pct))

# ──────────────────────────────────────────────────────────────
# CALCUL DES PERFORMANCES PAR FENÊTRE
# ──────────────────────────────────────────────────────────────

def _pnl_bot_or_fallback(point: dict) -> float:
    """
    Renvoie pnl_bot (= state["total_pnl"]) si la ligne JSONL le contient,
    sinon fallback sur alpha_pair pour les lignes historiques antérieures
    à V10b qui n'exportaient pas encore pnl_bot.

    Propriétés de pnl_bot :
      - Toujours >= 0 quand la grille fonctionne (somme des round-trips)
      - Indépendant du sens du marché et des achats d'inventaire
    alpha_pair (fallback) est acceptable car il incorpore lui aussi les
    profits de trading, mais avec un bruit directionnel en plus.
    """
    v = point.get("pnl_bot")
    if v is not None:
        return float(v)
    return float(point.get("alpha_pair", 0.0))


def compute_window_performance(history: list, pair: str, window: timedelta, now: datetime) -> dict:
    cutoff = now - window
    points = [r for r in history if r.get("pair") == pair and r["_dt"] >= cutoff]

    if len(points) < MIN_POINTS_PER_WINDOW:
        return {"valid": False, "reason": "Pas assez de points", "points": len(points), "coverage": 0.0}

    points.sort(key=lambda r: r["_dt"])

    # -------------------------------------------------
    # Transition historique vers pnl_bot
    # -------------------------------------------------

    # Si pnl_bot existe dans l'historique,
    # on ne conserve que les points contenant pnl_bot.
    if any("pnl_bot" in p for p in points):
        points = [p for p in points if "pnl_bot" in p]

        # V106 — BUGFIX : l'âge du pnl_bot doit être mesuré sur l'historique
        # GLOBAL de la paire, pas sur les points de la fenêtre courante.
        #
        # Ancien code (V105) :
        #   oldest = min(p["_dt"] for p in points)   # limité à la fenêtre !
        # Problème : pour la fenêtre 24h, `points` ne couvre que [now-24h, now],
        # donc `oldest` est toujours <= 24h, et la condition `< 24` était
        # TOUJOURS vraie → fenêtre 24h définitivement invalide.
        #
        # Correction : chercher le point pnl_bot le plus vieux dans TOUT
        # l'historique de la paire, indépendamment de la fenêtre courante.
        all_pnl_bot_for_pair = [
            r for r in history if r.get("pair") == pair and "pnl_bot" in r
        ]
        if all_pnl_bot_for_pair:
            oldest_global = min(p["_dt"] for p in all_pnl_bot_for_pair)
            pnl_bot_age_hours = (now - oldest_global).total_seconds() / 3600
        else:
            pnl_bot_age_hours = 0.0   # sécurité (ne peut pas arriver ici)

        if pnl_bot_age_hours < 24:
            return {
                "valid": False,
                "reason": f"Historique pnl_bot trop récent ({pnl_bot_age_hours:.1f}h)",
                "points": len(points),
                "coverage": 0.0,
            }
        
    if len(points) < MIN_POINTS_PER_WINDOW:
        return {
            "valid": False,
            "reason": "Pas assez de points pnl_bot",
            "points": len(points),
            "coverage": 0.0,
        }
        
      

    if _detect_counter_reset(points, "trades"):
        return {
            "valid": False,
            "reason": "Rupture détectée (reset du cache PnL)",
            "points": len(points),
            "coverage": 0.0,
        }


    coverage = min(1.0, (now - points[0]["_dt"]).total_seconds() / window.total_seconds())
    if coverage < MIN_WINDOW_COVERAGE:
        return {
            "valid": False,
            "reason": f"Couverture trop faible ({coverage:.1%})",
            "points": len(points),
            "coverage": coverage,
        }

    alpha_start = points[0].get("alpha_pair", 0.0)
    alpha_end   = points[-1].get("alpha_pair", 0.0)
    delta_alpha = alpha_end - alpha_start

    # ── delta_pnl_bot : incrément de la somme des round-trips (SCORING)
    # Utilise pnl_bot si présent (lignes JSONL V10b+),
    # sinon fallback sur alpha_pair (lignes historiques sans pnl_bot).
    pnl_bot_start = _pnl_bot_or_fallback(points[0])
    pnl_bot_end   = _pnl_bot_or_fallback(points[-1])
    delta_pnl_bot = pnl_bot_end - pnl_bot_start

    # ── delta_pnl : incrément de pnl_real (AFFICHAGE / vérification uniquement)
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

    capital_moyen = _time_weighted_mean(points, "capital_usdc", treat_zero_as_missing=True)
    if capital_moyen is None:
        return {
            "valid": False,
            "reason": "capital_usdc absent de l'historique",
            "points": len(points),
            "coverage": coverage,
        }

    alpha_rate = (delta_alpha / capital_moyen) / days if days > 0 and capital_moyen > 0 else 0.0

    # ── pnl_rate : rendement du market-making basé sur pnl_bot (scoring et Kelly)
    # delta_pnl_bot >= 0 : monotone croissant, indépendant du sens du marché.
    pnl_rate = (delta_pnl_bot / capital_moyen) / days if days > 0 and capital_moyen > 0 else 0.0

    # ── Volatilité de alpha_pair (conservée pour affichage / comparaison)
    returns = []
    negative_returns = []
    
    for i in range(len(points) - 1):
        a0 = points[i].get("alpha_pair")
        a1 = points[i+1].get("alpha_pair")
        c0 = points[i].get("capital_usdc")
        if a0 is None or a1 is None or c0 is None or c0 == 0:
            continue
        dt = (points[i+1]["_dt"] - points[i]["_dt"]).total_seconds() / 86400
        if dt <= 0:
            continue
        r = (a1 - a0) / c0 / dt
        returns.append(r)
        if r < 0:
            negative_returns.append(r)
        
    vol_alpha = stdev(returns) if len(returns) >= 2 else 0.0
    
    downside_vol = (
        max(stdev(negative_returns), 0.01)
        if len(negative_returns) >= 2
        else vol_alpha
    )

    # ── Volatilité de pnl_bot (utilisée pour le scoring et le Kelly)
    # Même fallback que _pnl_bot_or_fallback pour chaque point.

    pnl_returns = []

    for i in range(len(points) - 1):
        p0 = _pnl_bot_or_fallback(points[i])
        p1 = _pnl_bot_or_fallback(points[i + 1])
        c0 = points[i].get("capital_usdc")

        if c0 is None or c0 == 0:
            continue

        dt = (points[i + 1]["_dt"] - points[i]["_dt"]).total_seconds() / 86400

        if dt <= 0:
            continue

        pnl_returns.append((p1 - p0) / c0 / dt)

    vol_pnl = stdev(pnl_returns) if len(pnl_returns) >= 2 else 0.0

    # Drawdown (priorité champ exporté, sinon calculé)
    drawdown_pct = _time_weighted_mean(points, "drawdown_pct")
    if drawdown_pct is None:
        drawdown_pct = _compute_drawdown(points)

    return {
        "valid": True,
        "points": len(points),
        "days": days,
        "delta_alpha": delta_alpha,
        "delta_pnl_bot": delta_pnl_bot,   # pnl_bot-based — pour scoring (V104b)
        "delta_pnl": delta_pnl,            # pnl_real-based — pour affichage uniquement
        "activity": activity,
        "alpha_rate": alpha_rate,          # pour affichage uniquement
        "pnl_rate": pnl_rate,              # pour scoring et Kelly (basé sur pnl_bot)
        "capital_moyen": capital_moyen,
        "coverage": coverage,
        "drawdown_pct": drawdown_pct,
        "vol_alpha": vol_alpha,            # pour affichage uniquement
        "downside_vol": downside_vol,
        "vol_pnl": vol_pnl,               # pour scoring et Kelly (basé sur pnl_bot)
    }

def compute_scores(perf_by_window: dict, health: dict) -> dict:
    """
    Score composite par paire :
      raw_score = Σ (sortino_alpha * risk_factor * effective_weight) / total_weight
      score     = raw_score + ucb_bonus

    sortino_alpha = alpha_rate / max(downside_vol, 0.05)
    ucb_bonus  = UCB_C * sqrt(log(N_total) / n_pair)  — exploration bonus

    Les champs kelly_pnl_rate et kelly_vol_pnl (moyennes pondérées par
    effective_weight) sont exposés pour le sizing Quarter-Kelly en aval.
    """
    # Compter les points 30j par paire pour le terme UCB
    n_counts = {
        pair: perf_by_window.get("30j", {}).get(pair, {}).get("points", 0)
        for pair in health
    }
    N_total = max(1, sum(n_counts.values()))

    scores = {}
    for pair, h in health.items():
        if not h:
            scores[pair] = {"score": 0.0, "raw_score": 0.0, "ucb_bonus": 0.0,
                            "valid": False, "windows": {},
                            "kelly_pnl_rate": 0.0, "kelly_vol_pnl": 0.0}
            continue

        total_weight   = 0.0
        weighted_score = 0.0
        # Accumulateurs pour la moyenne Kelly pondérée par effective_weight
        kelly_pnl_acc  = 0.0
        kelly_vol_acc  = 0.0
        window_details = {}

        for wname, wdata in perf_by_window.items():
            pdata = dict(wdata.get(pair, {}))
            nominal_weight = WINDOW_WEIGHTS[wname]
            pdata["nominal_weight"] = nominal_weight

            if not pdata.get("valid"):
                pdata["effective_weight"] = 0.0
                window_details[wname] = pdata
                continue

            coverage         = pdata.get("coverage", 0.0)
            effective_weight = nominal_weight * coverage
            pdata["effective_weight"] = effective_weight
            window_details[wname] = pdata

            if effective_weight <= 0:
                continue

            # Variables du score V107
            alpha_rate   = pdata.get("alpha_rate", 0.0)
            downside_vol = pdata.get("downside_vol", 0.0)

            # Variables Kelly (inchangées)
            pnl_rate = pdata.get("pnl_rate", 0.0)
            vol_pnl  = pdata.get("vol_pnl", 0.0)

            risk_factor = _risk_factor(pdata.get("drawdown_pct"))

            # Plancher de robustesse : évite les scores délirants
            # lorsqu'une paire a très peu de pertes observées.
            sortino_alpha = alpha_rate / max(downside_vol, SORTINO_VOL_FLOOR)

            window_score = sortino_alpha * risk_factor

            weighted_score += window_score * effective_weight
            total_weight   += effective_weight
            kelly_pnl_acc  += pnl_rate * effective_weight
            kelly_vol_acc  += vol_pnl  * effective_weight

        raw_score = (weighted_score / total_weight) if total_weight > 0 else 0.0

        # Moyennes Kelly pondérées
        kelly_pnl_rate = kelly_pnl_acc / total_weight if total_weight > 0 else 0.0
        kelly_vol_pnl  = kelly_vol_acc  / total_weight if total_weight > 0 else 0.0

        # Terme UCB : bonus d'exploration inversement proportionnel aux données
        n_pair    = n_counts.get(pair, 0)
        ucb_bonus = UCB_C * math.sqrt(math.log(N_total) / max(1, n_pair))

        final_score = raw_score + ucb_bonus

        scores[pair] = {
            "score":          final_score,
            "raw_score":      raw_score,
            "ucb_bonus":      ucb_bonus,
            "valid":          total_weight > 0,
            "windows":        window_details,
            "kelly_pnl_rate": kelly_pnl_rate,
            "kelly_vol_pnl":  kelly_vol_pnl,
        }
    return scores

def _total_effective_weight(score_entry: dict) -> float:
    return sum(w.get("effective_weight", 0.0) for w in score_entry.get("windows", {}).values())

# ──────────────────────────────────────────────────────────────
# GESTION DES SERVICES
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
    try:
        with open(service_file) as f:
            lines = f.readlines()
    except Exception as e:
        logger.error(f"Erreur lecture {service_file} : {e}")
        return False

    found_execstart = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("ExecStart="):
            parts = line.split()
            if len(parts) >= 4:
                found_execstart = True
                parts[3] = f"{new_budget:.2f}"
                new_lines.append(" ".join(parts) + "\n")
            else:
                logger.error(f"Ligne ExecStart malformée dans {service_file}")
                return False
        else:
            new_lines.append(line)

    if not found_execstart:
        logger.error(f"Aucune ligne ExecStart trouvée dans {service_file}")
        return False

    tmp = f"/tmp/{os.path.basename(service_file)}.tmp"
    try:
        with open(tmp, "w") as f:
            f.writelines(new_lines)
        subprocess.run(["sudo", "cp", tmp, service_file], check=True)
        os.remove(tmp)
    except Exception as e:
        logger.error(f"Erreur écriture {service_file} : {e}")
        return False

    logger.info(f"Service mis à jour : budget → {new_budget:.2f} ({service_file})")
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

def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def load_state_json(state_file: str) -> dict | None:
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Erreur lecture state {state_file} : {e}")
        return None

def compute_quote_virtual_usdc(state: dict) -> float:
    """
    Capital économique total du bot = capital_usdc (inclut déjà la valeur de la stratégie)
    + total_pnl (PnL réalisé cumulé).
    """
    return _num(state.get("capital_usdc")) + _num(state.get("total_pnl"))

def reset_capital_in_state(state_file, new_budget, dry_run=False):
    logger.info(
        f"Réallocation : budget cible = {new_budget:.2f}$ "
        f"(capital_usdc et wallet_peak inchangés, "
        f"seul le service systemd est mis à jour)"
    )

# ──────────────────────────────────────────────────────────────
# COOLDOWN ET RÉALLOCATION
# ──────────────────────────────────────────────────────────────

def get_last_reallocation() -> datetime | None:
    if not os.path.exists(LAST_REALLOC_FILE):
        return None
    try:
        with open(LAST_REALLOC_FILE) as f:
            data = json.load(f)
        dt = datetime.fromisoformat(data["timestamp"])
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception as e:
        logger.warning(f"Impossible de lire {LAST_REALLOC_FILE} : {e} — cooldown ignoré")
        return None

def save_last_reallocation(now: datetime, reco: dict):
    try:
        with open(LAST_REALLOC_FILE, "w") as f:
            json.dump({"timestamp": now.isoformat(), "reco": reco}, f, indent=2)
        logger.info(f"Cooldown enregistré : {now.isoformat()}")
    except Exception as e:
        logger.error(f"Erreur écriture {LAST_REALLOC_FILE} : {e}")

def _quarter_kelly_pct(pnl_rate: float, vol_pnl: float) -> tuple[float, str]:
    """
    Calcule le pourcentage de transfert via le critère de Kelly au quart.

    Formule : f* = pnl_rate / vol_pnl²  (full Kelly, rendement continu)
              transfer_pct = clamp(f*/4, KELLY_MIN_PCT, KELLY_MAX_PCT)

    Retourne (transfer_pct, description) pour le logging.
    """
    if pnl_rate <= 0:
        return KELLY_MIN_PCT, f"fallback (pnl_rate={pnl_rate:.5f} ≤ 0)"
    if vol_pnl <= 0:
        # vol nulle = données insuffisantes — être conservateur
        return KELLY_MIN_PCT, f"fallback (vol_pnl indéfini)"

    full_kelly = pnl_rate / (vol_pnl ** 2)
    quarter_kelly = full_kelly / 4.0
    clamped = max(KELLY_MIN_PCT, min(KELLY_MAX_PCT, quarter_kelly))
    desc = (f"f*={full_kelly:.4f} → f*/4={quarter_kelly:.4f} "
            f"→ clamp[{KELLY_MIN_PCT:.0%},{KELLY_MAX_PCT:.0%}] = {clamped:.2%}")
    return clamped, desc

def compute_reallocation(scores: dict, current_metrics: dict, now: datetime) -> dict:
    """
    Décide et dimensionne la réallocation.

    Classement  : basé sur score = raw_score + ucb_bonus (exploration incluse)
    Sizing      : Quarter-Kelly sur pnl_rate_winner / vol_pnl_winner²
    Garde-fous  : MIN_CAPITAL_USDC, quote_virtuelle loser, MAX_PAIR_WEIGHT,
                  MIN_TRANSFER_USDC, cooldown 24h, score_winner > 0

    Note : MIN_TOTAL_WEIGHT_FOR_DECISION (gate binaire V103) est supprimé.
    La protection est désormais assurée par deux mécanismes continus :
      - le terme UCB dégrade le bonus naturellement quand n_pair augmente
      - le Quarter-Kelly retourne KELLY_MIN_PCT si les données sont
        insuffisantes (vol_pnl nul ou pnl_rate ≤ 0), limitant le transfert
        au minimum sûr plutôt que de bloquer la décision.
    """
    valid_pairs = [p for p in scores if scores[p].get("valid")]
    excluded    = [p for p in scores if not scores[p].get("valid")]
    if excluded:
        logger.warning(f"Paires exclues du classement (données insuffisantes) : {excluded}")

    if len(valid_pairs) < 2:
        return {
            "action": "SKIP",
            "reason": f"Moins de 2 paires valides ({len(valid_pairs)})",
            "excluded": excluded,
        }

    last = get_last_reallocation()
    if last:
        elapsed_h = (now - last).total_seconds() / 3600
        if elapsed_h < REALLOCATION_COOLDOWN_H:
            return {"action": "HOLD", "reason": f"Cooldown ({elapsed_h:.1f}h < {REALLOCATION_COOLDOWN_H}h)"}

    # Ranking sur score UCB-augmenté (exploration incluse)
    ranked       = sorted(valid_pairs, key=lambda p: scores[p]["score"], reverse=True)
    winner       = ranked[0]
    loser        = ranked[-1]
    score_winner = scores[winner]["score"]
    score_loser  = scores[loser]["score"]
    delta        = score_winner - score_loser

    if delta < MIN_SCORE_DELTA:
        return {"action": "HOLD", "reason": f"Écart trop faible ({delta:.6f})",
                "winner": winner, "loser": loser, "delta": delta,
                "score_winner": score_winner, "score_loser": score_loser}

    if score_winner <= 0:
        return {"action": "HOLD", "reason": f"Meilleur score non positif ({score_winner:.4f})",
                "winner": winner, "loser": loser,
                "score_winner": score_winner, "score_loser": score_loser}

    cfg_winner = BOTS.get(winner)
    cfg_loser  = BOTS.get(loser)
    if not cfg_winner or not cfg_loser:
        return {"action": "SKIP", "reason": "Configuration BOTS manquante"}

    # Budgets actuels (service file > fallback metrics)
    
    budgets = {}

    for pair in BOTS:
        m = current_metrics["pairs"].get(pair, {})

        budgets[pair] = float(
            m.get(
                "total_wallet",
                m.get("capital_usdc", 0.0)
            )
        )
    
    
    total_capital = sum(budgets.values())
    logger.info(f"DEBUG budgets = {budgets}")
    logger.info(f"DEBUG total_capital = {total_capital:.2f}")
    budget_winner = budgets[winner]
    budget_loser  = budgets[loser]

    # ── Quarter-Kelly sizing ──────────────────────────────────────
    pnl_rate_w = scores[winner].get("kelly_pnl_rate", 0.0)
    vol_pnl_w  = scores[winner].get("kelly_vol_pnl",  0.0)
    transfer_pct, kelly_desc = _quarter_kelly_pct(pnl_rate_w, vol_pnl_w)
    logger.info(f"Quarter-Kelly {winner}: pnl_rate={pnl_rate_w:.5f}/j  "
                f"vol_pnl={vol_pnl_w:.5f}/j  {kelly_desc}")

    transfer_raw  = round(budget_loser * transfer_pct, 2)
    transfer      = transfer_raw
    reason_extra  = ""

    if budget_loser - transfer < MIN_CAPITAL_USDC:
        transfer = max(0.0, budget_loser - MIN_CAPITAL_USDC)
        reason_extra += " (limité par capital minimum)"

    loser_state = load_state_json(cfg_loser["state_file"])
    if loser_state is not None:
        quote_virtual = max(0.0, compute_quote_virtual_usdc(loser_state))
        if transfer > quote_virtual:
            transfer = round(quote_virtual, 2)
            reason_extra += f" (limité par quote virtuelle {quote_virtual:.2f}$)"

    max_allowed_winner = MAX_PAIR_WEIGHT * total_capital
    if budget_winner + transfer > max_allowed_winner:
        max_transfer_for_winner = max(0.0, max_allowed_winner - budget_winner)
        if transfer > max_transfer_for_winner:
            logger.warning(
                f"Plafond de concentration atteint pour {winner}. "
                f"Transfert réduit à {max_transfer_for_winner:.2f}$"
            )
            transfer = round(max_transfer_for_winner, 2)
            reason_extra += f" (plafond concentration {MAX_PAIR_WEIGHT:.0%})"

    if transfer < MIN_TRANSFER_USDC:
        return {
            "action": "HOLD",
            "reason": f"Transfert trop petit ({transfer:.2f}$ < {MIN_TRANSFER_USDC}$){reason_extra}",
            "winner": winner, "loser": loser,
            "score_winner": score_winner, "score_loser": score_loser,
            "transfer_computed": transfer_raw,
            "budget_loser": budget_loser,
            "budget_winner": budget_winner,
            "transfer_pct_used": transfer_pct,
            "kelly_desc": kelly_desc,
        }

    new_budget_winner = round(budget_winner + transfer, 2)
    new_budget_loser  = round(budget_loser  - transfer, 2)

    return {
        "action":               "REALLOCATE",
        "winner":               winner,
        "loser":                loser,
        "score_winner":         score_winner,
        "score_loser":          score_loser,
        "raw_score_winner":     scores[winner].get("raw_score", score_winner),
        "ucb_bonus_winner":     scores[winner].get("ucb_bonus", 0.0),
        "delta":                delta,
        "budget_winner_before": budget_winner,
        "budget_loser_before":  budget_loser,
        "transfer_usdc":        transfer,
        "new_budget_winner":    new_budget_winner,
        "new_budget_loser":     new_budget_loser,
        "reason":               f"Surperformance de {winner} (delta={delta:.4f}){reason_extra}",
        "transfer_pct_used":    transfer_pct,
        "kelly_desc":           kelly_desc,
        "total_capital":        total_capital,
    }

def execute_reallocation(reco: dict, dry_run: bool, now: datetime) -> bool:
    if reco["action"] != "REALLOCATE":
        return False
    winner = reco["winner"]
    loser = reco["loser"]
    cfg_w = BOTS[winner]
    cfg_l = BOTS[loser]

    ok_w = update_service_budget(cfg_w["service_file"], reco["new_budget_winner"], dry_run)
    if not ok_w:
        logger.error(f"Échec mise à jour service {winner} – annulé")
        return False

    ok_l = update_service_budget(cfg_l["service_file"], reco["new_budget_loser"], dry_run)
    if not ok_l:
        logger.error(f"Échec mise à jour service {loser} – rollback de {winner}")
        rollback_ok = update_service_budget(cfg_w["service_file"], reco["budget_winner_before"], dry_run)
        if not rollback_ok:
            logger.critical(
                f"ROLLBACK ÉCHOUÉ pour {winner} ! Intervention manuelle requise."
            )
        return False

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
    """Pour les rendements (alpha_rate)"""
    if r is None:
        return "  n/a      "
    sign = "+" if r >= 0 else ""
    return f"{sign}{r * 100:.4f}%/j"

def fmt_score(s):
    """Pour le score composite (sans dimension)"""
    if s is None:
        return "  n/a    "
    return f"{s:+.4f}"

def print_detailed_report(scores: dict, perf_by_window: dict, health: dict, reco: dict, now: datetime):
    W = 72
    SEP = "═" * W
    sep = "─" * W

    print(f"\n{SEP}")
    print(f"  PORTFOLIO MANAGER V9.4  ·  {now.strftime('%Y-%m-%d %H:%M:%S')} (heure locale)")
    print(SEP)

    print(f"\n  📐 POIDS FENÊTRES [nominal, avant pondération par coverage]")
    for wname, w in WINDOW_WEIGHTS.items():
        print(f"     {wname:<4} : {w:.1%}")
    print(f"  📐 UCB_C={UCB_C}  Kelly=[{KELLY_MIN_PCT:.0%},{KELLY_MAX_PCT:.0%}]")
    print(sep)

    for pair, s in scores.items():
        h = health.get(pair, {})
        if not h:
            print(f"\n  ❌ {pair}  [pas de state file – exclu du classement]")
            continue

        tag         = "✅" if s["valid"] else "❌"
        capital     = h.get("capital_usdc", 0.0)
        total_pnl   = h.get("total_pnl", 0.0)
        total_trades = h.get("total_trades", 0)
        raw_score   = s.get("raw_score", s["score"])
        ucb_bonus   = s.get("ucb_bonus", 0.0)

        print(f"\n  {tag} {pair}  🟢 {h.get('service_status','?')}")
        print(f"     Score={fmt_score(s['score'])}  "
              f"(raw={fmt_score(raw_score)}  ucb_bonus={ucb_bonus:+.4f})")
        print(f"     Capital={capital:.2f}$  PnL={total_pnl:+.4f}$  Trades={total_trades}")
        print(f"     Kelly: pnl_rate={s.get('kelly_pnl_rate',0.0)*100:+.4f}%/j  "
              f"vol_pnl={s.get('kelly_vol_pnl',0.0)*100:.4f}%/j")
        print(sep)

        for wname in ["24h", "7j", "30j"]:
            wdata = s["windows"].get(wname, {})
            if wdata.get("valid"):
                eff_w         = wdata.get("effective_weight", 0.0)
                nom_w         = wdata.get("nominal_weight", 0.0)
                pnl_rate      = wdata.get("pnl_rate", 0.0)
                vol_pnl       = wdata.get("vol_pnl", 0.0)
                alpha_rate    = wdata.get("alpha_rate", 0.0)
                # delta_pnl_bot : scoring V104b (pnl_bot-based)
                # delta_pnl     : pnl_real-based (vérification)
                downside_vol = wdata.get("downside_vol", 0.0)
                vol_alpha    = wdata.get("vol_alpha", 0.0)
                
                delta_pnl_bot = wdata.get("delta_pnl_bot", wdata.get("delta_pnl", 0.0))
                delta_pnl     = wdata.get("delta_pnl", 0.0)
                delta_alpha   = wdata.get("delta_alpha", 0.0)
                points        = wdata.get("points", 0)
                activity      = wdata.get("activity", 0.0)
                coverage      = wdata.get("coverage", 0.0)
                dd            = wdata.get("drawdown_pct")
                dd_str        = f"  dd={dd:.1%}" if dd is not None else ""
                print(f"    {wname:<4}  "
                      f"pnl_rate={pnl_rate*100:+.4f}%/j  vol_pnl={vol_pnl*100:.4f}%/j  "
                      f"Δpnl_bot={delta_pnl_bot:+.3f}$  Δpnl_real={delta_pnl:+.3f}$")
                print(f"          "
                      f"α_rate={alpha_rate*100:+.4f}%/j  "
                      f"volα={vol_alpha*100:.4f}%/j  "
                      f"down={downside_vol*100:.4f}%/j  "
                      f"Δα={delta_alpha:+.3f}  "
                      f"pts={points}  act={activity:.0f}t/j  "
                      f"cov={coverage:.0%}  poids={eff_w:.3f}/{nom_w:.2f}{dd_str}")
            else:
                reason = wdata.get("reason", "Données insuffisantes")
                print(f"    {wname:<4}  {'--':>14}   [{reason}]")

        if not s["valid"]:
            print(f"    ⚠️  Aucune fenêtre exploitable (score basé sur UCB uniquement)")

    print(f"\n{SEP}")
    print("  DÉCISION DE RÉALLOCATION")
    print(sep)
    action = reco["action"]
    if action == "REALLOCATE":
        print(f"  ACTION    : RÉALLOUER")
        print(f"  Bot fort  : {reco['winner']}  (score {fmt_score(reco.get('score_winner'))}  "
              f"raw {fmt_score(reco.get('raw_score_winner'))}  "
              f"ucb +{reco.get('ucb_bonus_winner', 0.0):.4f})")
        print(f"  Bot faible: {reco['loser']}  (score {fmt_score(reco.get('score_loser'))})")
        print(f"  Écart     : {reco['delta']:+.4f}")
        print(sep)
        transfer_pct = reco.get("transfer_pct_used", 0.0)
        print(f"  Quarter-Kelly : {reco.get('kelly_desc', 'n/a')}")
        print(f"  Transférer    : {reco['transfer_usdc']:.2f}$  ({transfer_pct*100:.1f}% du loser)")
        print(f"  {reco['loser']:<10}: {reco['budget_loser_before']:.2f}$  →  {reco['new_budget_loser']:.2f}$")
        print(f"  {reco['winner']:<10}: {reco['budget_winner_before']:.2f}$  →  {reco['new_budget_winner']:.2f}$")
        if "total_capital" in reco:
            print(f"  Capital total : {reco['total_capital']:.2f}$  "
                  f"(max {MAX_PAIR_WEIGHT:.0%}/paire = {MAX_PAIR_WEIGHT*reco['total_capital']:.2f}$)")
    elif action == "HOLD":
        print(f"  ACTION  : CONSERVER")
        print(f"  Raison  : {reco['reason']}")
        if "transfer_computed" in reco:
            print(f"  (Kelly aurait transféré : {reco['transfer_computed']:.2f}$ "
                  f"— {reco.get('kelly_desc', '')})")
        if "winner" in reco:
            print(f"  Meilleur: {reco['winner']}  (score {fmt_score(reco.get('score_winner'))})")
            print(f"  Faible  : {reco['loser']}   (score {fmt_score(reco.get('score_loser'))})")
    else:
        print(f"  ACTION  : IGNORÉ")
        print(f"  Raison  : {reco['reason']}")
        if reco.get("excluded"):
            print(f"  Paires exclues : {reco['excluded']}")
    print(SEP)

# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Portfolio Manager V9.4 — Score pnl_bot, Quarter-Kelly, UCB continu"
    )
    parser.add_argument("--dry-run",    action="store_true", help="Simuler sans modification")
    parser.add_argument("--min-delta",  type=float, default=MIN_SCORE_DELTA, help="Écart minimum de score")
    parser.add_argument("--ucb-c",      type=float, default=UCB_C,           help=f"Constante UCB (défaut {UCB_C})")
    parser.add_argument("--kelly-min",  type=float, default=KELLY_MIN_PCT,   help=f"Transfer pct min Kelly (défaut {KELLY_MIN_PCT})")
    parser.add_argument("--kelly-max",  type=float, default=KELLY_MAX_PCT,   help=f"Transfer pct max Kelly (défaut {KELLY_MAX_PCT})")
    return parser.parse_args()

if __name__ == "__main__":
    setup_logging()
    args = parse_args()
    MIN_SCORE_DELTA = args.min_delta
    UCB_C           = args.ucb_c
    KELLY_MIN_PCT   = args.kelly_min
    KELLY_MAX_PCT   = args.kelly_max

    if args.dry_run:
        logger.info("=== MODE DRY-RUN (aucune modification réelle) ===")

    try:
        now = datetime.now()
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
