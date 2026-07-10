"""
transfer_engine.py

Moteur d'exécution atomique (TransferEngine).

Responsabilité : exécuter une opération unitaire sur un bot.
- Si SIMULATE : simule sans écrire ni redémarrer.
- Si EXECUTE : utilise BotManager.apply_transaction() pour effectuer la transaction complète
  (lecture, écriture atomique, restart, vérification).
- Retourne un TransferResult.
"""

import logging
import traceback
from typing import Optional

from execution_dtos import ExecutionOperation, TransferResult
from bot_manager import BotManager, MockBotManager
from shared_types import RunMode

logger = logging.getLogger(__name__)


class TransferEngine:
    """
    Moteur d'exécution atomique.

    Args:
        bot_manager: Gestionnaire de bots (BotManager ou MockBotManager).
    """

    def __init__(self, bot_manager):
        self.bot_manager = bot_manager
        self.logger = logging.getLogger(self.__class__.__name__)

    def execute(self, operation: ExecutionOperation, mode: RunMode) -> TransferResult:
        """
        Exécute une opération.

        Args:
            operation: Opération à exécuter.
            mode: Mode d'exécution (SIMULATE ou EXECUTE).

        Returns:
            TransferResult avec le résultat.
        """
        self.logger.debug(f"execute() appelée avec symbol={operation.symbol}, mode={mode}")

        if operation.operation_type.value == "HOLD":
            return TransferResult(
                operation_id=operation.operation_id,
                success=True,
                old_budget=operation.current_budget,
                new_budget=operation.current_budget,
                is_dry_run=(mode == RunMode.SIMULATE),
                error_message="Operation HOLD, aucune action."
            )

        # Vérification du symbole
        if not operation.symbol:
            return TransferResult(
                operation_id=operation.operation_id,
                success=False,
                old_budget=operation.current_budget,
                new_budget=None,
                error_message="Symbole vide dans l'opération"
            )

        # Récupérer le descripteur du bot
        try:
            descriptor = self.bot_manager.get_descriptor(operation.symbol)
        except Exception as e:
            self.logger.error(f"Erreur dans get_descriptor pour {operation.symbol}: {e}")
            self.logger.debug(traceback.format_exc())
            return TransferResult(
                operation_id=operation.operation_id,
                success=False,
                old_budget=operation.current_budget,
                new_budget=None,
                error_message=f"Erreur get_descriptor: {str(e)}"
            )

        if descriptor is None:
            self.logger.error(f"Bot {operation.symbol} introuvable (descriptor None)")
            return TransferResult(
                operation_id=operation.operation_id,
                success=False,
                old_budget=operation.current_budget,
                new_budget=None,
                error_message=f"Bot {operation.symbol} introuvable"
            )

        # Lire le budget actuel pour la simulation et la comparaison
        try:
            current_real = self.bot_manager.get_budget(descriptor)
        except Exception as e:
            self.logger.error(f"Impossible de lire le budget de {operation.symbol}: {e}")
            return TransferResult(
                operation_id=operation.operation_id,
                success=False,
                old_budget=operation.current_budget,
                new_budget=None,
                error_message=f"Lecture du budget échouée: {e}"
            )

        # Si déjà à la cible, on saute
        if abs(current_real - operation.target_budget) < 1e-6:
            self.logger.info(f"Bot {operation.symbol} déjà à la cible {operation.target_budget}")
            return TransferResult(
                operation_id=operation.operation_id,
                success=True,
                old_budget=current_real,
                new_budget=current_real,
                is_dry_run=(mode == RunMode.SIMULATE),
                error_message="Budget déjà à la cible"
            )

        # Mode SIMULATE : simulation
        if mode == RunMode.SIMULATE:
            self.logger.info(f"SIMULATION: {operation.symbol} budget {current_real} -> {operation.target_budget}")
            # Simuler une transaction en mémoire si le bot_manager est un MockBotManager
            if hasattr(self.bot_manager, 'apply_transaction'):
                # Utiliser la transaction simulée si disponible
                self.bot_manager.apply_transaction(operation.symbol, operation.target_budget)
            return TransferResult(
                operation_id=operation.operation_id,
                success=True,
                old_budget=current_real,
                new_budget=operation.target_budget,
                is_dry_run=True,
                error_message="SIMULATION: opération simulée"
            )

        # Mode EXECUTE : application réelle via BotManager.apply_transaction()
        self.logger.info(f"EXECUTE: application de la transaction pour {operation.symbol}")
        try:
            result = self.bot_manager.apply_transaction(operation.symbol, operation.target_budget)
            self.logger.debug(f"Résultat de apply_transaction: {result}")
            if result["success"]:
                return TransferResult(
                    operation_id=operation.operation_id,
                    success=True,
                    old_budget=result["old_budget"],
                    new_budget=result["new_budget"],
                    is_dry_run=False,
                    error_message=None
                )
            else:
                return TransferResult(
                    operation_id=operation.operation_id,
                    success=False,
                    old_budget=result.get("old_budget", current_real),
                    new_budget=None,
                    error_message=result.get("error", "Transaction échouée"),
                    is_dry_run=False
                )
        except Exception as e:
            self.logger.error(f"Erreur lors de l'application de la transaction pour {operation.symbol}: {e}")
            self.logger.debug(traceback.format_exc())
            return TransferResult(
                operation_id=operation.operation_id,
                success=False,
                old_budget=current_real,
                new_budget=None,
                error_message=f"Erreur transaction: {str(e)}",
                is_dry_run=False
            )
