#!/usr/bin/env python3
"""
update_bot_params.py - Mise à jour périodique des paramètres de grille (ATR, K)
Exécuter avec python3 (sans sudo) - sudo sera utilisé uniquement pour systemctl.
"""

import os
import sys
import subprocess
import logging
import argparse

# Ajouter le répertoire courant au PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Importer la fonction de calibration depuis script_atr.py
try:
    from script_atr import calibrate
    print("✅ Import depuis script_atr.py réussi")
except ImportError as e:
    print(f"❌ ERREUR: Impossible d'importer 'calibrate' depuis script_atr.py")
    print(f"   Détail: {e}")
    print("   Assurez-vous que script_atr.py existe et contient calibrate(exchange, symbol).")
    print("   Et que les dépendances (pandas, numpy, ta, python-binance) sont installées.")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────

BOTS = {
    "INJ": {
        "service": "bot_inj.service",
        "service_file": "/etc/systemd/system/bot_inj.service",
        "symbol": "INJUSDC",
    },
    "FIL": {
        "service": "bot_fil.service",
        "service_file": "/etc/systemd/system/bot_fil.service",
        "symbol": "FILUSDC",
    },
    "EGLD": {
        "service": "bot_egld.service",
        "service_file": "/etc/systemd/system/bot_egld.service",
        "symbol": "EGLDUSDC",
    },
}

THRESHOLD_ATR_CHANGE = 0.30      # 30%
THRESHOLD_K_CHANGE   = 0.10      # 0.10 absolu

LOG_FILE = "update_bot_params.log"

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
# LECTURE DES PARAMÈTRES ACTUELS
# ──────────────────────────────────────────────────────────────

def read_current_params(service_file: str) -> dict | None:
    if not os.path.exists(service_file):
        logger.error(f"Fichier service introuvable : {service_file}")
        return None
    with open(service_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith("ExecStart="):
                parts = line.split()
                try:
                    idx_bot = next(i for i, p in enumerate(parts) if 'bot.py' in p)
                except StopIteration:
                    logger.error(f"'bot.py' non trouvé dans {service_file}")
                    return None
                if len(parts) >= idx_bot + 7:
                    try:
                        return {
                            "budget": float(parts[idx_bot+2]),
                            "atr_low": float(parts[idx_bot+3]),
                            "atr_high": float(parts[idx_bot+4]),
                            "k_min": float(parts[idx_bot+5]),
                            "k_max": float(parts[idx_bot+6]),
                        }
                    except ValueError:
                        return None
    return None

# ──────────────────────────────────────────────────────────────
# MISE À JOUR (nécessite sudo pour l'écriture)
# ──────────────────────────────────────────────────────────────

def update_service_params(service_file: str, new_params: dict, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info(f"[DRY-RUN] Mise à jour de {service_file} avec : "
                    f"atr_low={new_params['atr_low']:.4f}, "
                    f"atr_high={new_params['atr_high']:.4f}, "
                    f"k_min={new_params['k_min']:.2f}, "
                    f"k_max={new_params['k_max']:.2f}")
        return True

    # Lire le fichier actuel (lecture sans sudo possible si fichier lisible)
    try:
        with open(service_file, 'r') as f:
            lines = f.readlines()
    except PermissionError:
        logger.error(f"Permission refusée pour lire {service_file}. Exécutez le script avec les droits suffisants.")
        return False

    modified = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("ExecStart="):
            parts = line.split()
            try:
                idx_bot = next(i for i, p in enumerate(parts) if 'bot.py' in p)
            except StopIteration:
                new_lines.append(line)
                continue
            if len(parts) >= idx_bot + 7:
                parts[idx_bot+3] = f"{new_params['atr_low']:.4f}"
                parts[idx_bot+4] = f"{new_params['atr_high']:.4f}"
                parts[idx_bot+5] = f"{new_params['k_min']:.2f}"
                parts[idx_bot+6] = f"{new_params['k_max']:.2f}"
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

    # Écrire le fichier modifié (nécessite sudo, on utilise tee)
    tmp_file = "/tmp/update_service.tmp"
    with open(tmp_file, 'w') as f:
        f.writelines(new_lines)
    try:
        subprocess.run(["sudo", "cp", tmp_file, service_file], check=True, timeout=5)
        os.remove(tmp_file)
        logger.info(f"Fichier service mis à jour : {service_file}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Erreur copie avec sudo : {e}")
        return False

def restart_service(service: str, dry_run: bool = False):
    if dry_run:
        logger.info(f"[DRY-RUN] Redémarrage de {service}")
        return
    try:
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True, timeout=10)
        subprocess.run(["sudo", "systemctl", "restart", service], check=True, timeout=20)
        logger.info(f"Service {service} redémarré")
    except subprocess.CalledProcessError as e:
        logger.error(f"Erreur redémarrage {service} : {e}")

# ──────────────────────────────────────────────────────────────
# DÉCISION
# ──────────────────────────────────────────────────────────────

def should_update(current: dict, new: dict) -> bool:
    if current["atr_low"] > 0:
        atr_low_ratio = abs(new["atr_low"] - current["atr_low"]) / current["atr_low"]
    else:
        atr_low_ratio = 1.0 if new["atr_low"] > 0 else 0.0
    if current["atr_high"] > 0:
        atr_high_ratio = abs(new["atr_high"] - current["atr_high"]) / current["atr_high"]
    else:
        atr_high_ratio = 1.0 if new["atr_high"] > 0 else 0.0
    k_min_diff = abs(new["k_min"] - current["k_min"])
    return (atr_low_ratio > THRESHOLD_ATR_CHANGE or
            atr_high_ratio > THRESHOLD_ATR_CHANGE or
            k_min_diff > THRESHOLD_K_CHANGE)

# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--pair", type=str)
    args = parser.parse_args()

    setup_logging()
    from exchange_gateio import ExchangeGateIO
    exchange = ExchangeGateIO()
    logger.info("=== Début calibration paramètres ===")

    pairs = [args.pair.upper()] if args.pair else BOTS.keys()
    any_update = False
    for pair in pairs:
        if pair not in BOTS:
            logger.error(f"Paire inconnue : {pair}")
            continue
        cfg = BOTS[pair]
        logger.info(f"\n--- Traitement de {pair} ({cfg['symbol']}) ---")
        try:
            new = calibrate(exchange, cfg['symbol'])
            if "k_max" not in new:
                new["k_max"] = 1.0
        except Exception as e:
            logger.error(f"Erreur calibration {cfg['symbol']} : {e}")
            continue

        current = read_current_params(cfg["service_file"])
        if not current:
            continue

        logger.info(f"Actuels : ATR_LOW={current['atr_low']:.4f} ATR_HIGH={current['atr_high']:.4f} K_MIN={current['k_min']:.2f}")
        logger.info(f"Nouveaux: ATR_LOW={new['atr_low']:.4f} ATR_HIGH={new['atr_high']:.4f} K_MIN={new['k_min']:.2f}")

        if args.force or should_update(current, new):
            logger.info("Changement significatif → mise à jour")
            if update_service_params(cfg["service_file"], new, args.dry_run):
                restart_service(cfg["service"], args.dry_run)
                any_update = True
        else:
            logger.info("Aucun écart significatif")

    if any_update and not args.dry_run:
        logger.info("\n✅ Mise à jour effectuée.")
    logger.info("=== Fin ===")

if __name__ == "__main__":
    main()
