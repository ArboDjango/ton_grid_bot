"""
execution_planner.py

Moteur d'analyse stratégique des allocations.

Responsabilité unique :
    Transformer les recommandations d'allocation (VirtualTreasuryManager)
    en directives d'exécution (utilisation du cash disponible ou réallocation
    entre stratégies).

Ce moteur est purement analytique :
    - Il ne modifie pas les AllocationResult.
    - Il ne calcule pas de budgets ni de GOI.
    - Il ne génère pas d'ordres.
    - Il ne communique pas avec l'exchange.
    - Il ne produit aucune chaîne de caractères destinée à l'affichage.
      La présentation est déléguée au MetaReport.

Il répond aux questions :
    - Le cash disponible est-il suffisant ?
    - Quelle stratégie peut être financée uniquement avec le cash ?
    - Quelle stratégie nécessite une réallocation ?
    - Quelle stratégie est candidate à une diminution ?
"""

import time
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional

# Import des structures existantes (inchangées)
from virtual_treasury_manager import AllocationResult, AllocationAction


# -------------------------------------------------------------------------
# Structures de données (DTO métier, sans texte)
# -------------------------------------------------------------------------

class FundingSource(Enum):
    """
    Source de financement d'une stratégie.

    Il s'agit d'une information métier qui sera réutilisée
    par le futur TransferEngine.
    """
    NONE = "NONE"               # Aucune action (HOLD)
    CASH = "CASH"               # Financée uniquement par le cash disponible
    REALLOCATION = "REALLOCATION" # Financée uniquement par prélèvement
    MIXED = "MIXED"             # Financée partiellement par CASH et REALLOCATION


@dataclass(frozen=True)
class ExecutionRecommendation:
    """
    Directive d'exécution pour une stratégie.

    Attributes:
        symbol: Symbole de la stratégie.
        action: Action d'allocation issue du VTM (INCREASE, DECREASE, HOLD).
        funding_source: Source de financement déterminée par l'ExecutionPlanner.
        cash_amount: Montant financé par le cash (0 si aucun).
        reallocation_amount: Montant financé par réallocation (0 si aucun).
        current_budget: Budget actuel de la stratégie (avant modification).
        target_budget: Budget cible (après modification).
    """
    symbol: str
    action: AllocationAction
    funding_source: FundingSource
    cash_amount: float
    reallocation_amount: float
    current_budget: float   # NOUVEAU
    target_budget: float    # NOUVEAU


@dataclass(frozen=True)
class ExecutionPlan:
    """
    Plan stratégique d'exécution.

    Attributes:
        timestamp: Timestamp UTC de la génération.
        plan_version: Version du moteur (pour traçabilité).
        execution_required: True si au moins une action doit être exécutée.
        free_cash: Cash disponible au départ.
        remaining_cash: Cash restant après utilisation.
        positive_need: Somme des deltas positifs (besoin total).
        negative_supply: Somme des valeurs absolues des deltas négatifs.
        cash_sufficient: True si le cash couvre tout le besoin.
        needs_reallocation: True si une partie doit être financée par réallocation.
        reallocation_amount: Montant total à réallouer (si > 0).
        recommendations: Liste des recommandations par stratégie.
    """
    timestamp: float
    plan_version: str
    execution_required: bool
    free_cash: float
    remaining_cash: float
    positive_need: float
    negative_supply: float
    cash_sufficient: bool
    needs_reallocation: bool
    reallocation_amount: float
    recommendations: List[ExecutionRecommendation] = field(default_factory=list)


# -------------------------------------------------------------------------
# Moteur principal
# -------------------------------------------------------------------------

class ExecutionPlanner:
    """
    Moteur d'analyse stratégique des allocations.

    Méthode publique :
        compute(allocations, free_cash) -> ExecutionPlan

    La politique de répartition du cash (proportionnelle aux deltas positifs)
    est isolée dans la méthode privée _allocate_cash pour faciliter
    son évolution ultérieure.
    """

    VERSION = "ExecutionPlanner-v1"
    EPSILON = 1e-9

    @staticmethod
    def compute(
        allocations: List[AllocationResult],
        free_cash: float
    ) -> ExecutionPlan:
        """
        Analyse les allocations recommandées et détermine la stratégie de financement.

        Args:
            allocations: Liste des AllocationResult produits par le VirtualTreasuryManager.
            free_cash: Solde USDT libre disponible.

        Returns:
            ExecutionPlan contenant l'analyse stratégique.

        Raises:
            ValueError: Si allocations est vide ou si free_cash est négatif.
        """
        if not allocations:
            raise ValueError("La liste des allocations ne peut pas être vide.")

        if free_cash < -ExecutionPlanner.EPSILON:
            raise ValueError(f"free_cash ne peut pas être négatif : {free_cash}")

        # Nettoyage des valeurs négligeables pour éviter les erreurs d'arrondi
        if free_cash < 0 and free_cash > -ExecutionPlanner.EPSILON:
            free_cash = 0.0

        # 1. Séparer les deltas et calculer les agrégats
        positive_deltas = [a for a in allocations if a.delta > ExecutionPlanner.EPSILON]
        negative_deltas = [a for a in allocations if a.delta < -ExecutionPlanner.EPSILON]
        zero_deltas = [a for a in allocations if abs(a.delta) <= ExecutionPlanner.EPSILON]

        positive_need = sum(a.delta for a in positive_deltas)
        negative_supply = sum(abs(a.delta) for a in negative_deltas)

        # Vérification de cohérence (conservation du capital)
        total_delta = sum(a.delta for a in allocations)
        if abs(total_delta) > ExecutionPlanner.EPSILON * max(1.0, len(allocations)):
            # On ne lève pas d'exception pour ne pas bloquer le pipeline,
            # mais on peut logger un warning (le logger est statique, on logge dans le MetaController).
            pass

        # 2. Déterminer si le cash est suffisant
        cash_sufficient = (free_cash + ExecutionPlanner.EPSILON) >= positive_need

        # 3. Générer les recommandations
        if cash_sufficient:
            recommendations = ExecutionPlanner._build_cash_sufficient_plan(
                allocations, positive_deltas, negative_deltas, zero_deltas,
                positive_need
            )
            remaining_cash = free_cash - positive_need
            needs_reallocation = False
            reallocation_amount = 0.0
        else:
            recommendations = ExecutionPlanner._build_cash_insufficient_plan(
                allocations, positive_deltas, negative_deltas, zero_deltas,
                positive_need, free_cash
            )
            remaining_cash = 0.0
            needs_reallocation = True
            reallocation_amount = positive_need - free_cash

        # 4. Déterminer si une exécution est requise
        execution_required = (
            positive_need > ExecutionPlanner.EPSILON or
            negative_supply > ExecutionPlanner.EPSILON
        )

        # 5. Construction du plan
        return ExecutionPlan(
            timestamp=time.time(),
            plan_version=ExecutionPlanner.VERSION,
            execution_required=execution_required,
            free_cash=free_cash,
            remaining_cash=remaining_cash,
            positive_need=positive_need,
            negative_supply=negative_supply,
            cash_sufficient=cash_sufficient,
            needs_reallocation=needs_reallocation,
            reallocation_amount=reallocation_amount,
            recommendations=recommendations,
        )

    # ---------------------------------------------------------------------
    # Méthodes privées
    # ---------------------------------------------------------------------

    @staticmethod
    def _build_cash_sufficient_plan(
        allocations: List[AllocationResult],
        positive_deltas: List[AllocationResult],
        negative_deltas: List[AllocationResult],
        zero_deltas: List[AllocationResult],
        positive_need: float,
    ) -> List[ExecutionRecommendation]:
        """Construit le plan quand le cash est suffisant."""
        recommendations = []

        # Stratégies en augmentation
        for alloc in positive_deltas:
            recommendations.append(
                ExecutionRecommendation(
                    symbol=alloc.symbol,
                    action=alloc.action,
                    funding_source=FundingSource.CASH,
                    cash_amount=alloc.delta,
                    reallocation_amount=0.0,
                    current_budget=alloc.current_budget,
                    target_budget=alloc.recommended_budget,
                )
            )

        # Stratégies en diminution : on ne vend pas
        for alloc in negative_deltas:
            recommendations.append(
                ExecutionRecommendation(
                    symbol=alloc.symbol,
                    action=alloc.action,
                    funding_source=FundingSource.NONE,
                    cash_amount=0.0,
                    reallocation_amount=0.0,
                    current_budget=alloc.current_budget,
                    target_budget=alloc.current_budget,  # inchangé
                )
            )

        # Stratégies sans mouvement
        for alloc in zero_deltas:
            recommendations.append(
                ExecutionRecommendation(
                    symbol=alloc.symbol,
                    action=alloc.action,
                    funding_source=FundingSource.NONE,
                    cash_amount=0.0,
                    reallocation_amount=0.0,
                    current_budget=alloc.current_budget,
                    target_budget=alloc.current_budget,
                )
            )

        return recommendations

    @staticmethod
    def _build_cash_insufficient_plan(
        allocations: List[AllocationResult],
        positive_deltas: List[AllocationResult],
        negative_deltas: List[AllocationResult],
        zero_deltas: List[AllocationResult],
        positive_need: float,
        free_cash: float,
    ) -> List[ExecutionRecommendation]:
        """Construit le plan quand le cash est insuffisant."""
        recommendations = []

        # 1. Répartir le cash proportionnellement aux deltas positifs
        cash_allocation = ExecutionPlanner._allocate_cash(
            positive_deltas, positive_need, free_cash
        )

        # 2. Construire les recommandations pour les stratégies en augmentation
        for alloc in positive_deltas:
            cash_amt = cash_allocation.get(alloc.symbol, 0.0)
            realloc_amt = alloc.delta - cash_amt

            # Arrondi pour éviter les -0.0
            if abs(cash_amt) < ExecutionPlanner.EPSILON:
                cash_amt = 0.0
            if abs(realloc_amt) < ExecutionPlanner.EPSILON:
                realloc_amt = 0.0

            # Déterminer la source
            if cash_amt > 0 and realloc_amt > 0:
                source = FundingSource.MIXED
            elif cash_amt > 0:
                source = FundingSource.CASH
            elif realloc_amt > 0:
                source = FundingSource.REALLOCATION
            else:
                source = FundingSource.NONE

            recommendations.append(
                ExecutionRecommendation(
                    symbol=alloc.symbol,
                    action=alloc.action,
                    funding_source=source,
                    cash_amount=cash_amt,
                    reallocation_amount=realloc_amt,
                    current_budget=alloc.current_budget,
                    target_budget=alloc.recommended_budget,
                )
            )

        # 3. Construire les recommandations pour les stratégies en diminution
        for alloc in negative_deltas:
            recommendations.append(
                ExecutionRecommendation(
                    symbol=alloc.symbol,
                    action=alloc.action,
                    funding_source=FundingSource.REALLOCATION,
                    cash_amount=0.0,
                    reallocation_amount=abs(alloc.delta),
                    current_budget=alloc.current_budget,
                    target_budget=alloc.recommended_budget,
                )
            )

        # 4. Stratégies sans mouvement
        for alloc in zero_deltas:
            recommendations.append(
                ExecutionRecommendation(
                    symbol=alloc.symbol,
                    action=alloc.action,
                    funding_source=FundingSource.NONE,
                    cash_amount=0.0,
                    reallocation_amount=0.0,
                    current_budget=alloc.current_budget,
                    target_budget=alloc.current_budget,
                )
            )

        return recommendations

    @staticmethod
    def _allocate_cash(
        positive_deltas: List[AllocationResult],
        positive_need: float,
        free_cash: float,
    ) -> Dict[str, float]:
        """
        Répartit le cash disponible entre les stratégies bénéficiaires.

        Politique actuelle : proportionnelle aux deltas positifs.
        Cette méthode est isolée pour faciliter le remplacement futur de la politique.

        Args:
            positive_deltas: Liste des allocations avec delta > 0.
            positive_need: Somme des deltas positifs.
            free_cash: Montant de cash à répartir.

        Returns:
            Dictionnaire {symbole: montant_cash_attribué}.
        """
        if not positive_deltas or positive_need <= ExecutionPlanner.EPSILON:
            return {}

        cash_alloc = {}
        # On s'assure de ne pas dépasser le besoin total
        cash_to_allocate = min(free_cash, positive_need)

        for alloc in positive_deltas:
            # Proportion du besoin représenté par cette stratégie
            ratio = alloc.delta / positive_need
            cash_alloc[alloc.symbol] = ratio * cash_to_allocate

        return cash_alloc
