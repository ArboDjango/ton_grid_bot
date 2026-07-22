"""
tests/test_execution_planner_integration_rn027.py

Test d'intégration confirmant que ExecutionPlanner ne recalcule jamais
indépendamment le montant de réallocation d'une stratégie DECREASE —
il se contente de lire abs(alloc.delta), la valeur déjà décidée et
réconciliée par VirtualTreasuryManager._reconcile_deltas() (RN-027/
RN-028/RN-029).

MISE A JOUR (RN-029) : avec le meme scenario reel qu'a l'origine
(free_usdt=333.82), le capital non alloue n'est plus artificiellement
comble (RN-029) — positive_need (RSR+EGLD+INJ, ~104.67) devient
largement couvert par le cash libre, rendant "cash_sufficient=True".
Dans ce cas, ExecutionPlanner applique son propre choix de conception
preexistant ("on ne vend pas") : les strategies en DECREASE ne sont
pas vendues tant que ce n'est pas necessaire pour financer les
hausses. Ce fichier distingue desormais explicitement les deux
chemins legitimes d'ExecutionPlanner :
  - cash suffisant : DECREASE => funding_source=NONE, aucune vente ;
  - cash insuffisant : DECREASE => funding_source=REALLOCATION,
    reallocation_amount == abs(delta) exactement.
"""

import pytest

from virtual_treasury_manager import VirtualTreasuryManager, StrategyState, AllocationAction
from execution_planner import ExecutionPlanner, FundingSource


REAL_SCENARIO_STRATEGIES = [
    StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
    StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
    StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
    StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
    StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
]


class TestExecutionPlannerWhenCashIsSufficient:
    """
    RN-029 : avec le cash libre reel de l'incident (333.82), le
    capital non alloue n'est plus comble artificiellement — le besoin
    des seules strategies en INCREASE est largement couvert par le
    cash disponible. ExecutionPlanner applique alors son comportement
    preexistant : ne pas vendre les strategies en DECREASE.
    """

    def test_decrease_strategies_are_not_sold_when_cash_is_sufficient(self):
        free_usdt = 333.82

        treasury_result = VirtualTreasuryManager.compute(REAL_SCENARIO_STRATEGIES, free_usdt)
        plan = ExecutionPlanner.compute(treasury_result.allocations, free_usdt)

        assert plan.cash_sufficient is True

        fil_alloc = next(a for a in treasury_result.allocations if a.symbol == "FILUSDT")
        fil_reco = next(r for r in plan.recommendations if r.symbol == "FILUSDT")

        assert fil_alloc.action is AllocationAction.DECREASE
        # Comportement preexistant d'ExecutionPlanner, inchange par
        # RN-027/028/029 : pas de vente forcee quand le cash suffit.
        assert fil_reco.funding_source == FundingSource.NONE
        assert fil_reco.target_budget == fil_reco.current_budget

    def test_treasury_delta_itself_still_correctly_preserves_the_decrease_decision(self):
        # Meme si ExecutionPlanner choisit de ne pas vendre, la
        # decision de VirtualTreasuryManager elle-meme (le delta
        # reconcilie) doit rester correcte : FIL et STX restent bien
        # en DECREASE, avec un delta au moins egal au plancher
        # MIN_DELTA_ACTION (RN-027), et desormais preserve integralement
        # tel que decide par le lissage (RN-029, aucun ecrasement).
        free_usdt = 333.82

        treasury_result = VirtualTreasuryManager.compute(REAL_SCENARIO_STRATEGIES, free_usdt)

        fil = next(a for a in treasury_result.allocations if a.symbol == "FILUSDT")
        stx = next(a for a in treasury_result.allocations if a.symbol == "STXUSDT")

        assert fil.action is AllocationAction.DECREASE
        assert stx.action is AllocationAction.DECREASE
        assert fil.delta <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6
        assert stx.delta <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6


class TestExecutionPlannerWhenCashIsInsufficient:
    """
    Meme scenario de strategies, mais avec un cash libre volontairement
    reduit pour forcer ExecutionPlanner dans son chemin de reallocation
    reelle — celui qui exerce concretement le pass-through
    reallocation_amount = abs(alloc.delta).
    """

    def test_reallocation_amount_matches_treasury_delta_exactly(self):
        free_usdt = 20.0  # insuffisant pour couvrir positive_need seul

        treasury_result = VirtualTreasuryManager.compute(REAL_SCENARIO_STRATEGIES, free_usdt)
        plan = ExecutionPlanner.compute(treasury_result.allocations, free_usdt)

        assert plan.cash_sufficient is False

        fil_alloc = next(a for a in treasury_result.allocations if a.symbol == "FILUSDT")
        fil_reco = next(r for r in plan.recommendations if r.symbol == "FILUSDT")

        assert fil_alloc.action is AllocationAction.DECREASE
        # Le montant de réallocation d'ExecutionPlanner doit être
        # EXACTEMENT abs(delta) tel que décidé par VirtualTreasuryManager
        # — aucun recalcul indépendant.
        assert fil_reco.reallocation_amount == pytest.approx(abs(fil_alloc.delta), abs=1e-9)
        assert fil_reco.funding_source == FundingSource.REALLOCATION

    def test_reallocation_amount_never_exceeds_the_strategys_own_delta_for_all_decreases(self):
        free_usdt = 20.0

        treasury_result = VirtualTreasuryManager.compute(REAL_SCENARIO_STRATEGIES, free_usdt)
        plan = ExecutionPlanner.compute(treasury_result.allocations, free_usdt)

        decrease_allocations = [
            a for a in treasury_result.allocations if a.action is AllocationAction.DECREASE
        ]
        assert len(decrease_allocations) >= 1

        for alloc in decrease_allocations:
            reco = next(r for r in plan.recommendations if r.symbol == alloc.symbol)
            assert reco.reallocation_amount == pytest.approx(abs(alloc.delta), abs=1e-9)
