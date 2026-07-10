"""
execution_queue.py

Moteur d'orchestration de l'exécution.
[...]
"""

import logging
import time
from typing import List, Optional, Dict, Any
import threading

from execution_dtos import (
    ExecutionBatch, ExecutionOperation,
    OperationStatus, BatchStatus, FailurePolicy,
    IQueueStore, ISchedulingStrategy, IBotManager, ILock,
    TransferResult, TransferReport, RunMode
)
from execution_planner import ExecutionPlan
from virtual_treasury_manager import AllocationAction
from transfer_engine import TransferEngine

logger = logging.getLogger(__name__)


class SimpleSchedulingStrategy(ISchedulingStrategy):
    def order_operations(self, plan: ExecutionPlan) -> List[ExecutionOperation]:
        operations = []
        for rec in plan.recommendations:
            if rec.action == AllocationAction.HOLD:
                continue
            amount = rec.cash_amount + rec.reallocation_amount
            if amount <= 1e-9:
                continue
            op = ExecutionOperation(
                symbol=rec.symbol,
                operation_type=rec.action,
                current_budget=rec.current_budget,
                target_budget=rec.target_budget,
                amount=amount,
                priority=0,
                status=OperationStatus.PENDING,
                metadata={
                    "funding_source": rec.funding_source.value,
                    "cash_amount": rec.cash_amount,
                    "reallocation_amount": rec.reallocation_amount,
                }
            )
            operations.append(op)
        def sort_key(op):
            if op.operation_type == AllocationAction.DECREASE:
                return 0
            elif op.operation_type == AllocationAction.INCREASE:
                return 1
            else:
                return 2
        operations.sort(key=sort_key)
        return operations


class InMemoryQueueStore(IQueueStore):
    def __init__(self):
        self._batches: Dict[str, ExecutionBatch] = {}

    def save_batch(self, batch: ExecutionBatch) -> None:
        self._batches[batch.batch_id] = batch

    def load_batch(self, batch_id: str) -> Optional[ExecutionBatch]:
        return self._batches.get(batch_id)

    def list_batches(self) -> List[str]:
        return list(self._batches.keys())


class ThreadingLock(ILock):
    def __init__(self):
        self._lock = threading.Lock()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        if timeout is None:
            return self._lock.acquire(blocking=True)
        else:
            return self._lock.acquire(blocking=True, timeout=timeout)

    def release(self) -> bool:
        self._lock.release()
        return True

    @property
    def locked(self) -> bool:
        return self._lock.locked()


class ExecutionQueue:
    def __init__(
        self,
        bot_manager: IBotManager,
        transfer_engine: Optional[TransferEngine] = None,
        store: Optional[IQueueStore] = None,
        scheduling_strategy: Optional[ISchedulingStrategy] = None,
        lock: Optional[ILock] = None,
        default_failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST,
    ):
        self.bot_manager = bot_manager
        self.transfer_engine = transfer_engine or TransferEngine(bot_manager)
        self.store = store or InMemoryQueueStore()
        self.scheduling_strategy = scheduling_strategy or SimpleSchedulingStrategy()
        self.lock = lock or ThreadingLock()
        self.default_failure_policy = default_failure_policy
        self.logger = logging.getLogger(self.__class__.__name__)

    def submit(self, plan: ExecutionPlan, mode: RunMode) -> ExecutionBatch:
        if self.lock.locked:
            raise RuntimeError("Un autre batch est déjà en cours d'exécution.")
        operations = self.scheduling_strategy.order_operations(plan)
        batch = ExecutionBatch(
            execution_mode=mode,
            plan_version=plan.plan_version,
            operations=operations,
            failure_policy=self.default_failure_policy,
            metadata={"free_cash": plan.free_cash, "positive_need": plan.positive_need}
        )
        self.store.save_batch(batch)
        self.logger.info(f"Batch {batch.batch_id} créé avec {len(operations)} opérations.")
        if mode == RunMode.OBSERVE:
            self.logger.info("Mode OBSERVE : batch enregistré mais non traité.")
            return batch
        if not self.lock.acquire():
            raise RuntimeError("Impossible d'acquérir le verrou.")
        try:
            self._process_batch(batch.batch_id)
        finally:
            self.lock.release()
        return self.store.load_batch(batch.batch_id)

    def process(self, batch_id: str) -> TransferReport:
        if self.lock.locked:
            raise RuntimeError("Un autre batch est déjà en cours.")
        if not self.lock.acquire():
            raise RuntimeError("Impossible d'acquérir le verrou.")
        try:
            self._process_batch(batch_id)
        finally:
            self.lock.release()
        batch = self.store.load_batch(batch_id)
        return self._build_report(batch)

    def _process_batch(self, batch_id: str) -> None:
        batch = self.store.load_batch(batch_id)
        if not batch:
            raise ValueError(f"Batch {batch_id} introuvable.")
        if batch.status in (BatchStatus.COMPLETED, BatchStatus.ABORTED, BatchStatus.PARTIAL):
            self.logger.warning(f"Batch {batch_id} déjà terminé (status: {batch.status}).")
            return
        batch = ExecutionBatch(**{**batch.__dict__, "status": BatchStatus.EXECUTING, "started_at": time.time()})
        self.store.save_batch(batch)
        operations = list(batch.operations)
        aborted = False
        for idx, op in enumerate(operations):
            if op.status != OperationStatus.PENDING:
                continue
            op = ExecutionOperation(**{**op.__dict__, "status": OperationStatus.PROCESSING})
            operations[idx] = op
            batch = ExecutionBatch(**{**batch.__dict__, "operations": operations})
            self.store.save_batch(batch)
            try:
                result = self.transfer_engine.execute(op, batch.execution_mode)
                if result.success:
                    op = ExecutionOperation(**{**op.__dict__, "status": OperationStatus.SUCCESS})
                else:
                    op = ExecutionOperation(**{**op.__dict__, "status": OperationStatus.FAILED})
                    if batch.failure_policy == FailurePolicy.FAIL_FAST:
                        for j in range(idx + 1, len(operations)):
                            operations[j] = ExecutionOperation(
                                **{**operations[j].__dict__, "status": OperationStatus.CANCELLED}
                            )
                        batch = ExecutionBatch(
                            **{**batch.__dict__, "status": BatchStatus.ABORTED, "operations": operations}
                        )
                        self.store.save_batch(batch)
                        aborted = True
                        break
            except Exception as e:
                self.logger.error(f"Erreur inattendue lors de l'exécution de {op.symbol}: {e}")
                op = ExecutionOperation(
                    **{**op.__dict__, "status": OperationStatus.FAILED,
                       "metadata": {**op.metadata, "error": str(e)}}
                )
                operations[idx] = op
                if batch.failure_policy == FailurePolicy.FAIL_FAST:
                    for j in range(idx + 1, len(operations)):
                        operations[j] = ExecutionOperation(
                            **{**operations[j].__dict__, "status": OperationStatus.CANCELLED}
                        )
                    batch = ExecutionBatch(
                        **{**batch.__dict__, "status": BatchStatus.ABORTED, "operations": operations}
                    )
                    self.store.save_batch(batch)
                    aborted = True
                    break
            operations[idx] = op
            batch = ExecutionBatch(**{**batch.__dict__, "operations": operations})
            self.store.save_batch(batch)
        if not aborted:
            statuses = [op.status for op in operations]
            if all(s in (OperationStatus.SUCCESS, OperationStatus.SKIPPED) for s in statuses):
                batch_status = BatchStatus.COMPLETED
            elif any(s == OperationStatus.FAILED for s in statuses):
                batch_status = BatchStatus.PARTIAL
            else:
                batch_status = BatchStatus.PARTIAL
            batch = ExecutionBatch(
                **{**batch.__dict__, "status": batch_status, "completed_at": time.time(), "operations": operations}
            )
            self.store.save_batch(batch)
        self.logger.info(f"Batch {batch_id} terminé avec statut {batch.status.value}")

    def _build_report(self, batch: ExecutionBatch) -> TransferReport:
        results = []
        successful = 0
        failed = 0
        skipped = 0
        cancelled = 0
        for op in batch.operations:
            if op.status == OperationStatus.SUCCESS:
                successful += 1
                results.append(TransferResult(
                    operation_id=op.operation_id,
                    success=True,
                    old_budget=op.current_budget,
                    new_budget=op.target_budget,
                    is_dry_run=(batch.execution_mode == RunMode.SIMULATE)
                ))
            elif op.status == OperationStatus.FAILED:
                failed += 1
                results.append(TransferResult(
                    operation_id=op.operation_id,
                    success=False,
                    old_budget=op.current_budget,
                    new_budget=None,
                    error_message=op.metadata.get("error", "Échec inconnu"),
                    is_dry_run=(batch.execution_mode == RunMode.SIMULATE)
                ))
            elif op.status == OperationStatus.SKIPPED:
                skipped += 1
            elif op.status == OperationStatus.CANCELLED:
                cancelled += 1
        return TransferReport(
            batch_id=batch.batch_id,
            total_operations=len(batch.operations),
            successful=successful,
            failed=failed,
            skipped=skipped,
            cancelled=cancelled,
            results=results,
            mode=batch.execution_mode,
            started_at=batch.started_at or batch.timestamp,
            finished_at=batch.completed_at or time.time(),
        )

    def get_status(self, batch_id: str) -> Optional[ExecutionBatch]:
        return self.store.load_batch(batch_id)

    def cancel(self, batch_id: str) -> bool:
        batch = self.store.load_batch(batch_id)
        if not batch:
            return False
        if batch.status in (BatchStatus.COMPLETED, BatchStatus.ABORTED):
            return False
        if batch.status == BatchStatus.EXECUTING:
            return False
        operations = list(batch.operations)
        for idx, op in enumerate(operations):
            if op.status == OperationStatus.PENDING:
                operations[idx] = ExecutionOperation(
                    **{**op.__dict__, "status": OperationStatus.CANCELLED}
                )
        batch = ExecutionBatch(
            **{**batch.__dict__, "status": BatchStatus.ABORTED, "operations": operations}
        )
        self.store.save_batch(batch)
        return True
