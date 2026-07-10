"""
execution_dtos.py

Couche de données pour l'exécution (ExecutionQueue, TransferEngine).

Contient :
- Les DTO (ExecutionOperation, ExecutionBatch, TransferResult, TransferReport)
- Les enums (OperationStatus, BatchStatus, FailurePolicy)
- Les interfaces (IQueueStore, ISchedulingStrategy, IBotManager, ILock)
"""

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any

from virtual_treasury_manager import AllocationAction
from shared_types import RunMode  # Import depuis shared_types


# -------------------------------------------------------------------------
# Enums
# -------------------------------------------------------------------------

class OperationStatus(Enum):
    """Statut d'une opération atomique."""
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class BatchStatus(Enum):
    """Statut global d'un lot d'exécution."""
    PREPARING = "PREPARING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    ABORTED = "ABORTED"


class FailurePolicy(Enum):
    """Politique de gestion des échecs au sein d'un batch."""
    FAIL_FAST = "FAIL_FAST"              # Dès un échec, on arrête tout.
    CONTINUE_ON_FAILURE = "CONTINUE_ON_FAILURE"  # On continue les autres opérations.


# -------------------------------------------------------------------------
# DTO
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionOperation:
    """
    Opération atomique sur un bot.

    Attributes:
        symbol: Nom du bot (stratégie).
        operation_type: INCREASE, DECREASE ou HOLD (mais HOLD ne devrait pas arriver ici).
        current_budget: Budget actuel du bot (avant exécution).
        target_budget: Budget cible (après exécution).
        amount: Montant absolu du delta (|target - current|).
        priority: Priorité (plus petit = plus prioritaire) – pour futures extensions.
        scheduled_at: Timestamp pour exécution différée (None = immédiat).
        status: État actuel de l'opération.
        retry_count: Nombre de tentatives déjà effectuées.
        metadata: Dictionnaire extensible pour informations supplémentaires.
        operation_id: Identifiant unique (UUID).
    """
    # Champs obligatoires (sans valeur par défaut)
    symbol: str
    operation_type: AllocationAction
    current_budget: float
    target_budget: float
    amount: float

    # Champs avec valeurs par défaut
    priority: int = 0
    scheduled_at: Optional[float] = None
    status: OperationStatus = OperationStatus.PENDING
    retry_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class ExecutionBatch:
    """
    Lot d'opérations correspondant à un cycle d'exécution.

    Attributes:
        execution_mode: Mode d'exécution (OBSERVE, DRY_RUN, EXECUTE).
        plan_version: Version de l'ExecutionPlanner.
        operations: Liste des opérations (ordre défini par la stratégie).
        status: Statut du batch.
        started_at: Timestamp de début de traitement (None si pas encore).
        completed_at: Timestamp de fin de traitement (None si pas fini).
        failure_policy: Politique de gestion des échecs.
        metadata: Extensible.
        batch_id: Identifiant unique (UUID).
        timestamp: Timestamp de création.
    """
    # Champs obligatoires
    execution_mode: RunMode
    plan_version: str

    # Champs avec valeurs par défaut
    operations: List[ExecutionOperation] = field(default_factory=list)
    status: BatchStatus = BatchStatus.PREPARING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    metadata: Dict[str, Any] = field(default_factory=dict)
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TransferResult:
    """
    Résultat d'une tentative d'exécution atomique.

    Attributes:
        operation_id: ID de l'opération traitée.
        success: True si tout s'est bien passé.
        old_budget: Budget avant modification.
        new_budget: Budget après modification (ou None si échec).
        error_message: Message d'erreur détaillé (si échec).
        systemd_return_code: Code retour de systemctl (si applicable).
        executed_at: Timestamp d'exécution.
        is_dry_run: True si c'était une simulation DRY_RUN.
    """
    operation_id: str
    success: bool
    old_budget: float
    new_budget: Optional[float]
    error_message: Optional[str] = None
    systemd_return_code: Optional[int] = None
    executed_at: float = field(default_factory=time.time)
    is_dry_run: bool = False


@dataclass(frozen=True)
class TransferReport:
    """
    Rapport consolidé d'un batch.

    Attributes:
        batch_id: ID du batch.
        total_operations: Nombre total d'opérations (toutes statuts confondus).
        successful: Nombre d'opérations réussies.
        failed: Nombre d'échecs.
        skipped: Nombre d'opérations ignorées (ex: HOLD).
        cancelled: Nombre d'opérations annulées (ABORTED).
        results: Liste des TransferResult pour les opérations traitées.
        mode: Mode d'exécution.
        started_at: Début du traitement.
        finished_at: Fin du traitement.
    """
    batch_id: str
    total_operations: int
    successful: int
    failed: int
    skipped: int
    cancelled: int
    results: List[TransferResult]
    mode: RunMode
    started_at: float
    finished_at: float


# -------------------------------------------------------------------------
# Interfaces (abstractions)
# -------------------------------------------------------------------------

class IQueueStore(ABC):
    """Abstraction pour la persistance des batches."""
    @abstractmethod
    def save_batch(self, batch: ExecutionBatch) -> None:
        pass

    @abstractmethod
    def load_batch(self, batch_id: str) -> Optional[ExecutionBatch]:
        pass

    @abstractmethod
    def list_batches(self) -> List[str]:
        pass


class ISchedulingStrategy(ABC):
    """Abstraction pour l'ordonnancement des opérations."""
    @abstractmethod
    def order_operations(self, plan: 'ExecutionPlan') -> List[ExecutionOperation]:
        """Transforme un ExecutionPlan en liste ordonnée d'opérations."""
        pass


class IBotManager(ABC):
    """Abstraction pour interagir avec les bots et le système."""
    @abstractmethod
    def get_budget(self, symbol: str) -> float:
        """Lit le budget actuel du bot."""
        pass

    @abstractmethod
    def set_budget(self, symbol: str, amount: float) -> None:
        """Écrit le nouveau budget (modifie la configuration)."""
        pass

    @abstractmethod
    def restart_bot(self, symbol: str) -> int:
        """Redémarre le bot. Retourne le code retour de la commande."""
        pass

    @abstractmethod
    def is_running(self, symbol: str) -> bool:
        """Vérifie si le bot est actif."""
        pass


class ILock(ABC):
    """Abstraction pour un verrou distribué."""
    @abstractmethod
    def acquire(self, timeout: Optional[float] = None) -> bool:
        pass

    @abstractmethod
    def release(self) -> bool:
        pass

    @property
    @abstractmethod
    def locked(self) -> bool:
        pass
