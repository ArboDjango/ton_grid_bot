#!/usr/bin/env python3
"""
run_meta_controller.py

Point d'entrée CLI du MetaController.

Ce script instancie les moteurs nécessaires, configure le MetaController
en fonction des arguments de ligne de commande, exécute le pipeline complet
et affiche le rapport. Il retourne un code de sortie approprié.

Utilisation :
    python run_meta_controller.py --exchange binance --mode OBSERVE

Arguments :
    --exchange NOM      Nom de l'exchange (ex: binance, gateio, etc.)
    --mode MODE         Mode d'exécution : OBSERVE (défaut), SIMULATE, EXECUTE
    --state-dir DIR     Répertoire des fichiers d'état (défaut: .)
    --lock-dir DIR      Répertoire des fichiers de lock (défaut: .)
    --debug             Activer les logs DEBUG
"""

import argparse
import sys
import logging
from typing import Optional

from observation_loader import ObservationLoader
from feature_engine import FeatureEngine
from goi_engine import GOIEngine
from decision_engine import DecisionEngine
from virtual_treasury_manager import VirtualTreasuryManager
from meta_controller import MetaController
from meta_report import MetaReportPrinter
from shared_types import RunMode


def create_exchange(exchange_name: str):
    """
    Crée une instance d'exchange à partir des implémentations du projet.
    """
    exchange_name = exchange_name.lower()

    if exchange_name == "gateio":
        from exchange_gateio import ExchangeGateIO
        return ExchangeGateIO()

    elif exchange_name == "binance":
        from exchange_binance import ExchangeBinance
        return ExchangeBinance()

    elif exchange_name == "coinbase":
        from exchange_coinbase import ExchangeCoinbase
        return ExchangeCoinbase()

    raise ValueError(f"Exchange non supporté : {exchange_name}")


def main():
    """Point d'entrée principal."""
    parser = argparse.ArgumentParser(
        description="Lanceur du MetaController (orchestrateur de stratégies)."
    )
    parser.add_argument(
        "--exchange",
        required=True,
        help="Nom de l'exchange (ex: binance, gateio, kraken)."
    )
    parser.add_argument(
        "--mode",
        choices=["OBSERVE", "SIMULATE", "EXECUTE"],
        default="OBSERVE",
        help="Mode d'exécution (défaut: OBSERVE)."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Activer le niveau de log DEBUG."
    )
    parser.add_argument(
        "--state-dir",
        default=".",
        help="Répertoire contenant les fichiers d'état (state_{exchange}_{symbol}.json)."
    )
    parser.add_argument(
        "--lock-dir",
        default=".",
        help="Répertoire contenant les fichiers de lock (lock_{symbol}.pid)."
    )
    args = parser.parse_args()

    # Configuration du logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    logger = logging.getLogger("run_meta_controller")

    try:
        # 1. Création de l'exchange
        logger.info(f"Création de l'exchange '{args.exchange}'...")
        exchange = create_exchange(args.exchange)

        # 2. Création des moteurs
        loader = ObservationLoader()
        feature_engine = FeatureEngine()
        goi_engine = GOIEngine()
        decision_engine = DecisionEngine()
        treasury_manager = VirtualTreasuryManager()

        # 3. Conversion du mode
        mode = RunMode[args.mode.upper()]

        # 4. Création du MetaController avec les répertoires
        controller = MetaController(
            exchange_name=args.exchange,
            exchange=exchange,
            loader=loader,
            feature_engine=feature_engine,
            goi_engine=goi_engine,
            decision_engine=decision_engine,
            treasury_manager=treasury_manager,
            mode=mode,
            bot_manager_config={
                "state_dir": args.state_dir,
                "lock_dir": args.lock_dir,
            },
        )

        # 5. Exécution
        logger.info("Lancement du MetaController...")
        result = controller.run()

        if result is None:
            logger.error("Le MetaController a retourné None. Vérifiez les erreurs ci-dessus.")
            return 1

        # 6. Affichage du rapport
        MetaReportPrinter.print_console(result)

        # 7. Code de retour
        if result.summary.errors:
            logger.error("Des erreurs globales ont été rencontrées.")
            return 1
        if result.summary.failed_strategies > 0:
            logger.warning("Certaines stratégies ont échoué.")
        return 0

    except Exception as e:
        logger.error(f"Erreur fatale : {e}", exc_info=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
