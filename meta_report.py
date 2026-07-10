"""
meta_report.py

Rapporteur du MetaController.

Responsabilité :
    Afficher les résultats produits par le MetaController de manière
    lisible et structurée pour un utilisateur humain.

Ce module ne contient aucune logique métier.
Il transforme les DTO produits par les moteurs en texte formaté.
"""

import time
from typing import Optional
from shared_types import RunMode

from meta_controller import MetaControllerResult
from virtual_treasury_manager import VirtualTreasuryManager
from execution_planner import ExecutionPlan, FundingSource
from execution_dtos import TransferReport


class MetaReportPrinter:
    """
    Rapporteur du MetaController.

    Méthodes statiques pour l'affichage console.
    """

    @staticmethod
    def print_console(result: MetaControllerResult) -> None:
        """
        Affiche un rapport complet du MetaController.

        Args:
            result: Résultat du MetaController.
        """
        # 1. En-tête général
        print("\n" + "=" * 72)
        print(f"META-CONTROLLER REPORT — {result.exchange.upper()}")
        print("=" * 72)
        print(f"Mode                : {result.mode.value}")
        print(f"Durée               : {result.summary.duration_ms:.2f} ms")
        print(f"Stratégies totales  : {result.summary.number_of_strategies}")
        print(f"Stratégies réussies : {result.summary.successful_strategies}")
        print(f"Stratégies en erreur: {result.summary.failed_strategies}")

        if result.summary.warnings:
            print("-" * 72)
            print("AVERTISSEMENTS:")
            for w in result.summary.warnings:
                print(f"  - {w}")

        if result.summary.errors:
            print("-" * 72)
            print("ERREURS GLOBALES:")
            for e in result.summary.errors:
                print(f"  - {e}")

        # 2. Section VIRTUAL TREASURY (inchangée)
        if result.treasury_result is not None:
            VirtualTreasuryManager.print_report(result.treasury_result)
        else:
            print("\n" + "=" * 72)
            print("VIRTUAL TREASURY — NON DISPONIBLE")
            print("=" * 72)

        # 3. Section EXECUTION PLAN (inchangée)
        MetaReportPrinter._print_execution_plan(result.execution_plan)

        # 4. Section TRANSFER REPORT (NOUVELLE)
        if result.transfer_report is not None:
            MetaReportPrinter._print_transfer_report(result.transfer_report)
        elif result.mode.is_execution_mode:
            print("\nAucun rapport de transfert généré (erreur lors de l'exécution).")

        print("\n" + "=" * 72)
        print("FIN DU RAPPORT")
        print("=" * 72)

    @staticmethod
    def _print_execution_plan(plan: Optional[ExecutionPlan]) -> None:
        """
        Affiche la section EXECUTION PLAN à partir du DTO.

        Args:
            plan: ExecutionPlan produit par l'ExecutionPlanner, ou None.
        """
        if plan is None:
            print("\n" + "=" * 72)
            print("EXECUTION PLAN — NON DISPONIBLE")
            print("=" * 72)
            return

        print("\n" + "=" * 72)
        print("EXECUTION PLAN")
        print("=" * 72)
        print(f"Version du plan       : {plan.plan_version}")
        print(f"Timestamp             : {time.ctime(plan.timestamp)}")
        print(f"Cash disponible       : {plan.free_cash:>12.2f} USDT")
        print(f"Besoin total          : {plan.positive_need:>12.2f} USDT")
        print(f"Cash suffisant        : {'OUI' if plan.cash_sufficient else 'NON'}")
        print(f"Réallocation nécessaire: {'OUI' if plan.needs_reallocation else 'NON'}")
        if plan.needs_reallocation:
            print(f"Montant à réallouer   : {plan.reallocation_amount:>12.2f} USDT")
        else:
            print(f"Cash restant          : {plan.remaining_cash:>12.2f} USDT")

        print("-" * 72)

        if not plan.recommendations:
            print("Aucune recommandation d'exécution.")
        else:
            for rec in plan.recommendations:
                # Construction du texte de source
                if rec.funding_source == FundingSource.NONE:
                    source_text = "AUCUNE ACTION"
                elif rec.funding_source == FundingSource.CASH:
                    source_text = "CASH"
                elif rec.funding_source == FundingSource.REALLOCATION:
                    source_text = "RÉALLOCATION"
                else:  # MIXED
                    source_text = "CASH + RÉALLOCATION"

                print(f"{rec.symbol}")
                print(f"  Action              : {rec.action.value}")
                print(f"  Source              : {source_text}")
                if rec.cash_amount > 0:
                    print(f"  Montant cash        : {rec.cash_amount:>10.2f}")
                if rec.reallocation_amount > 0:
                    print(f"  Montant réallocation : {rec.reallocation_amount:>10.2f}")
                print("-" * 36)

        print(f"Exécution requise     : {'OUI' if plan.execution_required else 'NON'}")
        print("=" * 72)

    @staticmethod
    def _print_transfer_report(report: TransferReport) -> None:
        """
        Affiche la section TRANSFER REPORT.

        Args:
            report: TransferReport produit par l'ExecutionQueue.
        """
        print("\n" + "=" * 72)
        print("TRANSFER REPORT")
        print("=" * 72)
        print(f"Batch ID            : {report.batch_id}")
        print(f"Mode                : {report.mode.value}")
        print(f"Début               : {time.ctime(report.started_at)}")
        print(f"Fin                 : {time.ctime(report.finished_at)}")
        print(f"Total opérations    : {report.total_operations}")
        print(f"Réussies            : {report.successful}")
        print(f"Échecs              : {report.failed}")
        print(f"Ignorées            : {report.skipped}")
        print(f"Annulées            : {report.cancelled}")
        print("-" * 72)

        if report.results:
            for r in report.results:
                status = "✅ SUCCESS" if r.success else "❌ FAILED"
                print(f"Opération {r.operation_id[:8]}... : {status}")
                if r.is_dry_run:
                    print("  (DRY RUN)")
                if r.error_message:
                    print(f"  Erreur : {r.error_message}")
                if r.success and r.new_budget is not None:
                    print(f"  Budget: {r.old_budget:.2f} -> {r.new_budget:.2f}")
        else:
            print("Aucun résultat détaillé.")

        print("=" * 72)
