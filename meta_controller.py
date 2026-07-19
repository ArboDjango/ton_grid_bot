# meta_controller.py

"""
MetaController V1 — Chef d'orchestre du système.

Ce module est le point d'entrée principal du Meta-Controller.
Il orchestre les différents moteurs spécialisés (ObservationLoader, FeatureEngine,
GOIEngine, DecisionEngine, VirtualTreasuryManager, ExecutionPlanner, ExecutionQueue)
sans jamais contenir de logique métier.

Le MetaController :
- Charge les observations via ObservationLoader (retourne dict[str, dict]).
- Pour chaque stratégie, calcule Features, GOI et Decision.
- Récupère le solde USDT libre du compte Spot.
- Exécute le VirtualTreasuryManager.
- Exécute l'ExecutionPlanner.
- Si le mode est DRY_RUN ou EXECUTE, soumet le plan à l'ExecutionQueue.
- Produit un résultat structuré (MetaControllerResult).

Il est purement analytique et exécutif, mais ne contient aucune logique métier.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from observation_loader import ObservationLoader
from feature_engine import FeatureEngine, FeatureSet
from goi_engine import GOIEngine, GOIResult
from decision_engine import DecisionEngine, DecisionResult, DecisionInput
from virtual_treasury_manager import VirtualTreasuryManager, VirtualTreasuryResult, StrategyState
from exchange_base import ExchangeBase

from execution_planner import ExecutionPlanner, ExecutionPlan
from execution_dtos import TransferReport
from execution_queue import ExecutionQueue
from bot_manager import BotManager, MockBotManager
from shared_types import RunMode


@dataclass(frozen=True)
class StrategyEvaluation:
    symbol: str
    observation: Optional[Dict[str, Any]]
    features: Optional[FeatureSet]
    goi_result: Optional[GOIResult]
    decision_result: Optional[DecisionResult]
    error: Optional[str] = None


@dataclass(frozen=True)
class ExecutionSummary:
    start_time: float
    end_time: float
    duration_ms: float
    number_of_strategies: int
    successful_strategies: int
    failed_strategies: int
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class MetaControllerResult:
    exchange: str
    mode: RunMode
    evaluations: List[StrategyEvaluation]
    treasury_result: Optional[VirtualTreasuryResult]
    free_usdt: float
    summary: ExecutionSummary
    execution_plan: Optional[ExecutionPlan] = None
    transfer_report: Optional[TransferReport] = None


class MetaController:
    def __init__(
        self,
        exchange_name: str,
        exchange: ExchangeBase,
        loader: ObservationLoader,
        feature_engine: FeatureEngine,
        goi_engine: GOIEngine,
        decision_engine: DecisionEngine,
        treasury_manager: VirtualTreasuryManager,
        mode: RunMode = RunMode.OBSERVE,
        bot_manager_config: Optional[Dict[str, Any]] = None,
    ):
        self.exchange_name = exchange_name
        self.exchange = exchange
        self.loader = loader
        self.feature_engine = feature_engine
        self.goi_engine = goi_engine
        self.decision_engine = decision_engine
        self.treasury_manager = treasury_manager
        self.mode = mode
        self.bot_manager_config = bot_manager_config or {}
        self.logger = logging.getLogger(self.__class__.__name__)

    def run(self) -> Optional[MetaControllerResult]:
        try:
            result = self._run_internal()
        except Exception as e:
            self.logger.error(f"Erreur non gérée dans MetaController: {e}", exc_info=True)
            return self._build_result(
                evaluations=[],
                treasury_result=None,
                execution_plan=None,
                transfer_report=None,
                free_usdt=0.0,
                start_time=time.time(),
                errors=[f"Erreur critique: {str(e)}"],
                warnings=[]
            )

        # Historisation (en cas de succès)
        if result is not None:
            try:
                from history_logger import MetaControllerHistoryLogger, build_reports
                history_logger = MetaControllerHistoryLogger()
                reports = build_reports(result)
                history_logger.log_meta_controller(reports["meta"])
                history_logger.log_capital(reports["capital"])
                history_logger.log_summary(reports["summary"])

                if result.execution_plan:
                    decisions = {}
                    for rec in result.execution_plan.recommendations:
                        decisions[rec.symbol] = rec.target_budget
                    history_logger.log_control_with_state(
                        reports["meta"]["timestamp"],
                        decisions
                    )
            except Exception as e:
                self.logger.warning(f"⚠️ Historisation échouée: {e}")

        return result

    def _run_internal(self) -> MetaControllerResult:
        start_time = time.time()
        errors = []
        warnings = []
        evaluations = []
        strategies = []

        self.logger.info(f"Démarrage du MetaController (mode={self.mode.value})")

        # Étape 1 : Chargement des observations
        self.logger.info("Chargement des observations...")
        try:
            observations = self.loader.load_exchange(self.exchange_name)
            if not observations:
                msg = "Aucune observation chargée."
                self.logger.warning(msg)
                warnings.append(msg)
                return self._build_result(
                    evaluations, None, None, None, 0.0, start_time, errors, warnings
                )
        except Exception as e:
            msg = f"Erreur lors du chargement des observations : {str(e)}"
            self.logger.error(msg, exc_info=True)
            errors.append(msg)
            return self._build_result(
                [], None, None, None, 0.0, start_time, errors, warnings
            )

        self.logger.info(f"{len(observations)} observations chargées.")

        # Étape 2 : Évaluation
        self.logger.info("Évaluation des stratégies...")
        for symbol, observation in observations.items():
            try:
                features = self.feature_engine.compute_all(observation)
                goi_result = self.goi_engine.compute(features)
                if not goi_result.valid:
                    msg = f"GOI invalide pour {symbol}: {goi_result.reason}"
                    self.logger.warning(msg)
                    warnings.append(msg)
                    evaluations.append(StrategyEvaluation(
                        symbol, observation, features, None, None, msg
                    ))
                    continue

                decision_input = DecisionInput(
                    current_capital=observation.get("capital_usdt", 0.0),
                    wallet=observation.get("wallet", {}),
                    goi=goi_result.value if goi_result.valid else 0.0,
                    headroom=features.headroom,
                    confidence=goi_result.confidence,
                )
                decision_result = self.decision_engine.compute(decision_input)
                if not decision_result.valid:
                    msg = f"Décision invalide pour {symbol}: {decision_result.reason}"
                    self.logger.warning(msg)
                    warnings.append(msg)

                evaluations.append(StrategyEvaluation(
                    symbol, observation, features, goi_result, decision_result,
                    None if decision_result.valid else msg
                ))

                # RN-026 : allocated_capital/capital_usdc est une consigne de
                # pilotage, pas une richesse.  La valeur de la stratégie est
                # son inventaire réel ; le cash partagé est ajouté une seule
                # fois plus bas via free_usdt.
                current_budget = observation.get("strategy_economic_value")
                if current_budget is None:
                    raise ValueError(
                        f"Valeur économique absente pour {symbol} "
                        "(strategy_economic_value)"
                    )
                current_budget = float(current_budget)
                if current_budget < 0:
                    raise ValueError(
                        f"Valeur économique négative pour {symbol}: {current_budget}"
                    )
                goi_value = goi_result.value if goi_result.valid else 0.0
                strategies.append(StrategyState(
                    symbol=symbol,
                    current_budget=current_budget,
                    goi=goi_value,
                    decision=decision_result,
                ))
            except Exception as e:
                msg = f"Erreur lors du traitement de {symbol}: {str(e)}"
                self.logger.error(msg, exc_info=True)
                errors.append(msg)
                evaluations.append(StrategyEvaluation(
                    symbol, observation, None, None, None, msg
                ))

        if not strategies:
            msg = "Aucune stratégie valide pour le VirtualTreasuryManager."
            self.logger.warning(msg)
            warnings.append(msg)
            return self._build_result(
                evaluations, None, None, None, 0.0, start_time, errors, warnings
            )

        self.logger.info(f"{len(strategies)} stratégies évaluées avec succès.")

        # Étape 3 : Solde USDT
        self.logger.info("Récupération du solde USDT libre...")
        try:
            free_usdt = self.exchange.get_balance("USDT")
            self.logger.info(f"Solde USDT libre : {free_usdt:.2f} USDT")
        except Exception as e:
            msg = f"Impossible de récupérer le solde USDT : {str(e)}"
            self.logger.error(msg, exc_info=True)
            errors.append(msg)
            free_usdt = 0.0

        # Étape 4 : VTM
        self.logger.info("Calcul des allocations virtuelles...")
        treasury_result = None
        try:
            treasury_result = self.treasury_manager.compute(strategies, free_usdt)
            self.logger.info("Allocations calculées avec succès.")
        except Exception as e:
            msg = f"Erreur lors du calcul des allocations : {str(e)}"
            self.logger.error(msg, exc_info=True)
            errors.append(msg)

        # Étape 5 : ExecutionPlanner
        execution_plan = None
        if treasury_result is not None and treasury_result.allocations:
            try:
                execution_plan = ExecutionPlanner.compute(
                    treasury_result.allocations,
                    free_usdt
                )
                self.logger.info("Plan d'exécution généré avec succès.")
            except Exception as e:
                msg = f"Erreur lors de la génération du plan d'exécution : {str(e)}"
                self.logger.error(msg, exc_info=True)
                errors.append(msg)

        # Étape 6 : Exécution (si mode == SIMULATE ou EXECUTE)
        transfer_report = None
        if self.mode.is_execution_mode and execution_plan is not None:
            self.logger.info(f"Lancement de l'exécution en mode {self.mode.value}...")
            try:
                if self.mode == RunMode.SIMULATE:
                    bot_manager = MockBotManager()
                    self.logger.info("Utilisation de MockBotManager pour la simulation.")
                else:
                    # En production, utiliser BotManager avec les bons répertoires
                    bot_manager = BotManager(
                        exchange=self.exchange_name,
                        state_dir=self.bot_manager_config.get("state_dir", "."),
                        lock_dir=self.bot_manager_config.get("lock_dir"),  # None par défaut
                        service_template=self.bot_manager_config.get(
                            "service_template", "bot_{base_asset}_{exchange}.service"
                        ),
                        state_file_template=self.bot_manager_config.get(
                            "state_file_template", "state_{exchange}_{symbol_lower}.json"
                        ),
                        lock_file_template=self.bot_manager_config.get(
                            "lock_file_template", "lock_{symbol_lower}.pid"
                        ),
                    )
                    self.logger.info(f"BotManager initialisé pour {self.exchange_name}")

                queue = ExecutionQueue(bot_manager=bot_manager)
                batch = queue.submit(execution_plan, self.mode)
                transfer_report = queue.process(batch.batch_id)
                self.logger.info("Exécution terminée.")
            except Exception as e:
                msg = f"Erreur lors de l'exécution : {str(e)}"
                self.logger.error(msg, exc_info=True)
                errors.append(msg)

        return self._build_result(
            evaluations,
            treasury_result,
            execution_plan,
            transfer_report,
            free_usdt,
            start_time,
            errors,
            warnings
        )

    def _build_result(
        self,
        evaluations: List[StrategyEvaluation],
        treasury_result: Optional[VirtualTreasuryResult],
        execution_plan: Optional[ExecutionPlan],
        transfer_report: Optional[TransferReport],
        free_usdt: float,
        start_time: float,
        errors: List[str],
        warnings: List[str],
    ) -> MetaControllerResult:
        end_time = time.time()
        duration_ms = (end_time - start_time) * 1000.0
        successful = sum(1 for e in evaluations if e.error is None)
        failed = len(evaluations) - successful

        summary = ExecutionSummary(
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            number_of_strategies=len(evaluations),
            successful_strategies=successful,
            failed_strategies=failed,
            warnings=warnings,
            errors=errors,
        )

        return MetaControllerResult(
            exchange=self.exchange_name,
            mode=self.mode,
            evaluations=evaluations,
            treasury_result=treasury_result,
            execution_plan=execution_plan,
            transfer_report=transfer_report,
            free_usdt=free_usdt,
            summary=summary,
        )
