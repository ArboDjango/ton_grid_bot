#!/usr/bin/env python3
"""
PORTFOLIO MANAGER V2 – RN-006 (Capital Policy Engine)
================================================================================
Cette version implémente la Research Note RN-006.

Le moteur de politique de capital (Capital Policy Engine) est mono-bot :
il ne compare jamais deux bots, ne transfère pas de capital entre eux,
et ne maintient pas de contrainte de capital total.

Chaque bot est traité indépendamment :
    current_capital  →  desired_capital (wallet)  →  converge_capital()  →  new_capital

Invariant fondamental :

Each bot is evaluated independently.

No capital decision depends on another bot.

The Capital Policy Engine never compares bots.

Seule la métrique validée "wallet" est utilisée pour déterminer le capital désiré.
Aucune métrique non validée (growth, volatilité, Kelly, UCB, GOI, fatigue, etc.)
n'influence la décision.
================================================================================
"""

import os
import sys
import glob
import json
import time
import subprocess
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Optional

# =============================================================================
#  CONSTANTES DE LA POLITIQUE DE CAPITAL (RN-006)
# =============================================================================

CAPITAL_POLICY_LAMBDA       = 0.25          # Facteur de convergence (0 = immobile, 1 = instantané)
CAPITAL_POLICY_DEADBAND_ABS = 0.01          # Seuil absolu (USDC) en dessous duquel on ne bouge pas
CAPITAL_POLICY_MAX_CHANGE_PCT = 0.05        # Variation maximale par période (en % du capital courant)
CAPITAL_POLICY_MIN_CAPITAL  = 50.0          # Capital minimum (USDC)
CAPITAL_POLICY_MAX_CAPITAL  = 500.0         # Capital maximum (USDC)

# =============================================================================
#  AUTRES CONSTANTES SYSTÈME
# =============================================================================

SYSTEMD_DIR = "/etc/systemd/system"
SUPPORTED_QUOTES = ("USDC", "USDT")
DEFAULT_EXCHANGE = os.getenv("EXCHANGE", "gateio")

# =============================================================================
#  LOGGING
# =============================================================================

logger = logging.getLogger(__name__)

def setup_logging(log_file: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

# =============================================================================
#  UTILITAIRES
# =============================================================================

def exchange_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())

def make_filenames(ekey: str) -> dict:
    suffix = f"_{ekey}"
    return {
        "history":         f"portfolio_history{suffix}.jsonl",
        "metrics":         f"audit_metrics{suffix}.json",
        "decision_journal": f"decision_journal{suffix}.jsonl",
        "log":             f"portfolio_manager{suffix}.log",
    }

def detect_bots(ekey: str) -> dict:
    """Détection des bots via les fichiers state (inchangée)."""
    pattern = f"state_{ekey}_*.json"
    bots = {}
    for sf in sorted(glob.glob(pattern)):
        stem = os.path.basename(sf).replace("state_", "").replace(".json", "")
        if stem.startswith(f"{ekey}_"):
            symbol_part = stem[len(ekey)+1:]
        else:
            symbol_part = stem
        symbol = symbol_part.upper()
        quote = None
        base = None
        for q in SUPPORTED_QUOTES:
            if symbol.endswith(q):
                quote = q
                base = symbol[:-len(q)]
                break
        if not quote:
            continue
        
        SERVICE_SUFFIX = {
            "binance": "",
            "gateio": "_gateio",
            "coinbase": "_coinbase",
        }
        
        suffix = SERVICE_SUFFIX.get(ekey, "")

        service_name = f"bot_{base.lower()}{suffix}.service"
        
        bots[symbol] = {
            "base":         base,
            "quote":        quote,
            "symbol":       symbol,
            "state_file":   sf,
            "service":      service_name,
            "service_file": os.path.join(SYSTEMD_DIR, service_name),
        }
    
    return bots

# =============================================================================
#  COMPOSANT 1 : AUDIT LOADER
# =============================================================================

class AuditLoader:
    def __init__(self, metrics_file: str):
        self.metrics_file = metrics_file
        self.data = {}

    def load(self) -> dict:
        if not os.path.exists(self.metrics_file):
            raise FileNotFoundError(f"Fichier {self.metrics_file} introuvable")
        with open(self.metrics_file) as f:
            self.data = json.load(f)
        return self.data

    def get_pairs(self) -> List[str]:
        return list(self.data.get("pairs", {}).keys())

    def get_pair_data(self, pair: str) -> dict:
        return self.data.get("pairs", {}).get(pair, {})

# =============================================================================
#  COMPOSANT 2 : HISTORY MANAGER (conservé pour le futur)
# =============================================================================

class HistoryManager:
    def __init__(self, history_file: str):
        self.history_file = history_file
        self.rows = []

    def load(self) -> List[dict]:
        if not os.path.exists(self.history_file):
            raise FileNotFoundError(f"Fichier {self.history_file} introuvable")
        self.rows = []
        with open(self.history_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    row["_dt"] = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    self.rows.append(row)
                except Exception:
                    pass
        self.rows.sort(key=lambda r: r["_dt"])
        logger.info(f"Historique chargé : {len(self.rows)} points")
        return self.rows

    def get_series(self, pair: str) -> List[dict]:
        return [r for r in self.rows if r.get("pair") == pair]

# =============================================================================
#  PIPELINE (inchangé)
# =============================================================================

class Pipeline:
    def __init__(self):
        self.layers = []

    def add(self, layer_cls):
        self.layers.append(layer_cls)

    def run(self, context):
        timings = {}
        for layer_cls in self.layers:
            name = layer_cls.NAME
            logger.info(f"[Pipeline] → {name}")
            try:
                start = time.time()
                context = layer_cls.process(context)
                elapsed = time.time() - start
                timings[name] = elapsed
                logger.debug(f"[Pipeline] {name} : {elapsed*1000:.2f} ms")
            except Exception as e:
                logger.error(f"[Pipeline] Erreur dans la couche {name} : {e}")
                raise
        context["timings"] = timings
        return context

# =============================================================================
#  COUCHES CONSERVÉES POUR LE FUTUR (stubs neutres)
# =============================================================================

class Layer0_MarketRegime:
    NAME = "MarketRegime"
    OUTPUTS = ["goi", "goi_confidence"]
    @staticmethod
    def process(context: dict) -> dict:
        for pair in context["pairs"]:
            context["pairs"][pair]["goi"] = 1.0
            context["pairs"][pair]["goi_confidence"] = 1.0
        return context

class Layer1_Health:
    NAME = "Health"
    OUTPUTS = ["health", "capacity", "fatigue"]
    @staticmethod
    def process(context: dict) -> dict:
        for pair in context["pairs"]:
            context["pairs"][pair]["health"] = 1.0
            context["pairs"][pair]["capacity"] = 1.0
            context["pairs"][pair]["fatigue"] = 1.0
        return context

class Layer2_Performance:
    NAME = "Performance"
    OUTPUTS = ["capital", "total_wallet", "alpha_pct", "drawdown", "points"]
    @staticmethod
    def process(context: dict) -> dict:
        # Cette couche est conservée mais n'est pas utilisée par la décision RN-006.
        # Elle pourra être activée dans une future Research Note.
        for pair in context["pairs"]:
            audit = context["audit_data"]["pairs"].get(pair, {})
            context["pairs"][pair]["capital"] = audit.get("capital_usdc", 0.0)
            context["pairs"][pair]["total_wallet"] = audit.get("wallet", 0.0)
            context["pairs"][pair]["alpha_pct"] = audit.get("alpha_pct", 0.0)
            context["pairs"][pair]["drawdown"] = audit.get("drawdown_pct", 0.0)
            context["pairs"][pair]["points"] = 0
        return context

class Layer3_Confidence:
    NAME = "Confidence"
    OUTPUTS = ["confidence"]
    @staticmethod
    def process(context: dict) -> dict:
        # Couche neutre conservée pour le futur.
        for pair in context["pairs"]:
            context["pairs"][pair]["confidence"] = 1.0
        return context

# =============================================================================
#  COUCHE 4 – CAPITAL POLICY ENGINE (RN-006)
# =============================================================================

# --- Fonctions pures ---

def compute_desired_capital(bot_audit_data: dict) -> float:
    """
    Research Note RN-006 :
    Le capital désiré est défini comme la valeur économique courante du bot.
    Actuellement : desired_capital = wallet.

    Cette fonction sera modifiée dans les futures Research Notes pour intégrer
    d'autres métriques validées (GOI, capacité, etc.) sans impacter
    l'algorithme de convergence.
    """
    return float(bot_audit_data.get("wallet", 0.0))

def converge_capital(
    current_capital: float,
    desired_capital: float,
    lambda_factor: float,
    deadband_abs: float,
    max_change_pct: float,
    min_capital: float,
    max_capital: float,
) -> float:
    """
    Implémente la règle de convergence progressive de RN-006.

    Cette fonction est PURE :
        - Pas de logging
        - Pas d'accès disque
        - Pas d'effet de bord

    Args:
        current_capital: Capital alloué actuel (USDC).
        desired_capital: Capital cible (USDC).
        lambda_factor: Vitesse de convergence (0 = immobile, 1 = instantané).
        deadband_abs: Seuil absolu en dessous duquel on ne bouge pas.
        max_change_pct: Variation maximale autorisée par période (en % du capital courant).
        min_capital: Plancher absolu.
        max_capital: Plafond absolu.

    Returns:
        Nouveau capital (USDC).
    """
    delta = desired_capital - current_capital

    # 1. Zone morte
    if abs(delta) <= deadband_abs:
        return current_capital

    # 2. Convergence proportionnelle
    new_cap = current_capital + lambda_factor * delta

    # 3. Borne de variation maximale (en valeur absolue)
    max_change_abs = current_capital * max_change_pct
    new_cap = max(current_capital - max_change_abs, min(new_cap, current_capital + max_change_abs))

    # 4. Bornes absolues
    new_cap = max(min_capital, min(new_cap, max_capital))

    return new_cap

# --- Orchestrateur ---

def compute_new_capital(context: dict) -> Dict[str, float]:
    """
    Calcule le nouveau capital pour chaque bot, indépendamment des autres.
    Chaque bot est traité isolément.

    Retourne un dictionnaire {nom_bot: nouveau_capital}.
    """
    audit_data = context["audit_data"]["pairs"]
    current_capitals = {
        pair: context["pairs"][pair].get("capital", 0.0)
        for pair in context["pairs"]
    }

    new_capitals = {}

    for pair, audit in audit_data.items():
        current = current_capitals.get(pair, 0.0)
        desired = compute_desired_capital(audit)

        new_cap = converge_capital(
            current_capital=current,
            desired_capital=desired,
            lambda_factor=CAPITAL_POLICY_LAMBDA,
            deadband_abs=CAPITAL_POLICY_DEADBAND_ABS,
            max_change_pct=CAPITAL_POLICY_MAX_CHANGE_PCT,
            min_capital=CAPITAL_POLICY_MIN_CAPITAL,
            max_capital=CAPITAL_POLICY_MAX_CAPITAL,
        )
        new_capitals[pair] = new_cap

    return new_capitals

# --- Couche Pipeline ---

class Layer4_CapitalPolicy:
    NAME = "CapitalPolicy"
    OUTPUTS = ["new_capitals", "decision"]

    @staticmethod
    def process(context: dict) -> dict:
        # 1. Calcul des nouveaux capitaux (mono-bot)
        new_capitals = compute_new_capital(context)
        context["new_capitals"] = new_capitals

        # 2. Construction de la décision (individuelle)
        decisions = {}
        for pair, new_cap in new_capitals.items():
            old_cap = context["pairs"][pair].get("capital", 0.0)
            delta = new_cap - old_cap
            if abs(delta) > 0.01:  # seuil min pour éviter les micro-ajustements
                decisions[pair] = {
                    "action": "UPDATE_CAPITAL",
                    "delta": delta,
                    "new_capital": new_cap,
                    "old_capital": old_cap,
                }

        if not decisions:
            context["decision"] = {"action": "HOLD", "reason": "Aucun changement significatif"}
        else:
            context["decision"] = {
                "action": "CAPITAL_POLICY_UPDATE",
                "details": decisions,
                "new_capitals": new_capitals,
            }

        # 3. Mise à jour du contexte pour les couches suivantes
        # for pair in context["pairs"]:
            # context["pairs"][pair]["capital"] = new_capitals[pair]
            # Le contexte représente l'état observé.
            # Il n'est jamais modifié par le moteur de décision.

        return context

# =============================================================================
#  COUCHE 5 – EXÉCUTION (applique les mises à jour individuelles)
# =============================================================================

class Layer5_Execution:
    @staticmethod
    def process(context: dict) -> bool:
        reco = context.get("decision", {})
        if reco.get("action") not in ("UPDATE_CAPITAL", "CAPITAL_POLICY_UPDATE"):
            return False

        dry_run = context.get("dry_run", False)
        bots_config = context["bots_config"]
        new_capitals = reco.get("new_capitals", {})

        success = True
        modified_services = []

        for pair, new_cap in new_capitals.items():
            if pair not in bots_config:
                continue

            service_file = bots_config[pair]["service_file"]

            result = Layer5_Execution._update_service_budget(
                service_file,
                new_cap,
                dry_run
            )

            if result is True:
                modified_services.append(bots_config[pair]["service"])

            elif result is False:
                logger.error(f"Échec mise à jour service {pair}")
                success = False

            # result is None :
            # le budget est déjà correct, on ne fait rien

        if dry_run:
            logger.info("[DRY-RUN] Mise à jour des capitaux simulée")
            return success

        logger.info(f"success = {success}")
        
        if success and modified_services:
            try:
                logger.info(">>> APPEL daemon-reload <<<")
                subprocess.run(
                    ["sudo", "systemctl", "daemon-reload"],
                    check=True,
                    timeout=20
                )
                logger.info("systemd rechargé (daemon-reload)")
            except Exception as e:
                logger.error(f"Erreur daemon-reload : {e}")
                return False

            for svc in modified_services:
                try:
                    logger.info(f">>> RESTART {svc} <<<")
                    subprocess.run(
                        ["sudo", "systemctl", "restart", svc],
                        check=True,
                        timeout=20
                    )
                    logger.info(f"Service redémarré : {svc}")
                except Exception as e:
                    logger.error(f"Erreur redémarrage {svc} : {e}")
                    success = False

        return success



    @staticmethod
    def _update_service_budget(
        service_file: str,
        new_budget: float,
        dry_run: bool,
    ):
        """
        Met à jour le budget d'un service systemd.

        Retour :
            True  -> fichier modifié
            None  -> budget déjà identique
            False -> erreur
        """

        if dry_run:
            logger.info(f"[DRY-RUN] Budget {service_file} → {new_budget:.2f}")
            return True

        if not os.path.exists(service_file):
            logger.error(f"Fichier service introuvable : {service_file}")
            return False

        try:
            with open(service_file, "r") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"Erreur lecture {service_file} : {e}")
            return False

        new_lines = []

        found = False

        i = 0

        while i < len(lines):

            line = lines[i]

            # -------------------------------------------------------------
            # Ligne normale
            # -------------------------------------------------------------

            if not line.lstrip().startswith("ExecStart="):
                new_lines.append(line)
                i += 1
                continue

            found = True

            # -------------------------------------------------------------
            # Reconstruction d'un ExecStart (mono ou multi-ligne)
            # -------------------------------------------------------------

            exec_lines = [line]

            while exec_lines[-1].rstrip().endswith("\\"):

                i += 1

                if i >= len(lines):
                    logger.error(f"ExecStart incomplet dans {service_file}")
                    return False

                exec_lines.append(lines[i])

            exec_cmd = "".join(exec_lines)

            exec_cmd = exec_cmd.replace("\\\n", " ")

            parts = exec_cmd.strip().split()

            if len(parts) < 2:
                logger.error(f"Format ExecStart invalide : {service_file}")
                return False

            try:
                current_budget = float(parts[-1])

            except ValueError:
                logger.error(f"Budget invalide dans {service_file}")
                return False

            # -------------------------------------------------------------
            # Déjà à jour
            # -------------------------------------------------------------

            if abs(current_budget - new_budget) < 0.005:

                logger.info(
                    f"Budget inchangé : {current_budget:.2f} ({service_file})"
                )

                return None

            # -------------------------------------------------------------
            # Remplacement du budget
            # -------------------------------------------------------------

            parts[-1] = f"{new_budget:.2f}"

            new_lines.append(" ".join(parts) + "\n")

            i += 1

        if not found:
            logger.error(f"Aucune ligne ExecStart dans {service_file}")
            return False

        tmp = f"/tmp/{os.path.basename(service_file)}.tmp"

        try:

            with open(tmp, "w") as f:
                f.writelines(new_lines)

            subprocess.run(
                ["sudo", "cp", tmp, service_file],
                check=True,
            )

            os.remove(tmp)

        except Exception as e:
            logger.error(f"Erreur écriture {service_file} : {e}")
            return False

        logger.info(f"Budget mis à jour → {new_budget:.2f} ({service_file})")

        return True

# =============================================================================
#  DECISION JOURNAL
# =============================================================================

class DecisionJournal:
    def __init__(self, journal_file: str):
        self.journal_file = journal_file

    def log_decision(self, context: dict, reco: dict) -> None:
        entry = {
            "timestamp": context["now"].isoformat(),
            "budgets_before": {
                pair: context["pairs"][pair].get("capital", 0.0)
                for pair in context["pairs"]
            },
            "budgets_after": {},
            "decision": reco.get("action", "UNKNOWN"),
            "reason": reco.get("reason", ""),
            "desired_capitals": {},
            "layer_outputs": {
                "goi": {pair: context["pairs"][pair].get("goi", 1.0) for pair in context["pairs"]},
                "health": {pair: context["pairs"][pair].get("health", 1.0) for pair in context["pairs"]},
            },
            "timings": context.get("timings", {})
        }

        # Ajouter les desired_capitals (calculés via compute_desired_capital)
        audit_data = context["audit_data"]["pairs"]
        for pair in context["pairs"]:
            desired = compute_desired_capital(audit_data.get(pair, {}))
            entry["desired_capitals"][pair] = desired

        if reco.get("action") in ("UPDATE_CAPITAL", "CAPITAL_POLICY_UPDATE"):
            new_caps = reco.get("new_capitals", {})
            entry["budgets_after"] = new_caps
        else:
            entry["budgets_after"] = entry["budgets_before"].copy()

        try:
            with open(self.journal_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Erreur écriture journal : {e}")

# =============================================================================
#  RAPPORT
# =============================================================================

def print_report(context: dict, reco: dict, exchange_name: str) -> None:
    W, SEP = 72, "═" * 72
    now = context["now"]
    print(f"\n{SEP}")
    print(f"  CAPITAL POLICY ENGINE RN-006  ·  {exchange_name}  ·  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)
    print(f"  λ={CAPITAL_POLICY_LAMBDA}  deadband={CAPITAL_POLICY_DEADBAND_ABS}  max_change={CAPITAL_POLICY_MAX_CHANGE_PCT:.0%}  min={CAPITAL_POLICY_MIN_CAPITAL}  max={CAPITAL_POLICY_MAX_CAPITAL}")
    print("─" * W)

    # Récupérer les données par bot
    audit_data = context["audit_data"]["pairs"]
    for pair in context["pairs"]:
        old_cap = context["pairs"][pair].get("capital", 0.0)
        new_cap = reco.get("new_capitals", {}).get(pair, old_cap)
        desired = compute_desired_capital(audit_data.get(pair, {}))
        wallet = audit_data.get(pair, {}).get("wallet", 0.0)
        delta = new_cap - old_cap

        print(f"\n  {pair}")
        print(f"     Current Capital  : {old_cap:.2f}")
        print(f"     Economic Capital (wallet) : {wallet:.2f}")
        print(f"     Desired Capital  : {desired:.2f}")
        print(f"     New Capital      : {new_cap:.2f}")
        print(f"     Delta            : {delta:+.2f}")

    print(f"\n{SEP}")
    print("  DÉCISION")
    print("─" * W)
    a = reco.get("action", "UNKNOWN")
    if a in ("UPDATE_CAPITAL", "CAPITAL_POLICY_UPDATE"):
        print(f"  ACTION  : MISE À JOUR DES CAPITAUX")
        for pair, details in reco.get("details", {}).items():
            print(f"    {pair}: {details['old_capital']:.2f} → {details['new_capital']:.2f} (delta: {details['delta']:+.2f})")
    elif a == "HOLD":
        print(f"  ACTION  : CONSERVER")
        print(f"  Raison  : {reco.get('reason','')}")
    else:
        print(f"  ACTION  : IGNORÉ")
        print(f"  Raison  : {reco.get('reason','')}")
    print(SEP)

# =============================================================================
#  MAIN
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="Portfolio Manager – RN-006 (Capital Policy Engine)")
    p.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()

def main():
    args = parse_args()
    ekey = exchange_key(args.exchange)
    files = make_filenames(ekey)
    setup_logging(files["log"])

    logger.info(f"=== Capital Policy Engine RN-006 — {args.exchange} ===")
    if args.dry_run:
        logger.info("MODE DRY-RUN")

    try:
        now = datetime.now()

        bots = detect_bots(ekey)
        if not bots:
            logger.error("Aucun bot détecté.")
            sys.exit(1)

        audit_loader = AuditLoader(files["metrics"])
        audit_data = audit_loader.load()
        history_manager = HistoryManager(files["history"])
        history_manager.load()

        context = {
            "now": now,
            "dry_run": args.dry_run,
            "bots_config": bots,
            "audit_data": audit_data,
            "history_manager": history_manager,
            "pairs": {pair: {} for pair in audit_data.get("pairs", {}).keys() if pair in bots},
            "decision": {},
        }

        logger.info(f"Paires audit : {list(audit_data.get('pairs', {}).keys())}")
        logger.info(f"Paires contexte : {list(context['pairs'].keys())}")

        if not context["pairs"]:
            raise ValueError("Aucune paire commune entre l'audit et les bots détectés.")

        # Construction du pipeline
        pipeline = Pipeline()
        pipeline.add(Layer0_MarketRegime)
        pipeline.add(Layer1_Health)
        pipeline.add(Layer2_Performance)
        pipeline.add(Layer3_Confidence)
        pipeline.add(Layer4_CapitalPolicy)   # <--- remplace l'ancien Optimizer

        context = pipeline.run(context)

        reco = context["decision"]

        # Journalisation
        journal = DecisionJournal(files["decision_journal"])
        journal.log_decision(context, reco)

        # Rapport
        print_report(context, reco, args.exchange)

        # Exécution
        if reco.get("action") in ("UPDATE_CAPITAL", "CAPITAL_POLICY_UPDATE"):
            success = Layer5_Execution.process(context)
            if success:
                logger.info("Mise à jour des capitaux exécutée avec succès.")
            else:
                logger.error("Échec de la mise à jour des capitaux.")
        else:
            logger.info("Aucune mise à jour exécutée (HOLD).")

    except Exception as e:
        logger.exception(f"Erreur fatale : {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
