"""
test_execution_planner.py

Tests unitaires pour l'ExecutionPlanner.
"""

import pytest
from execution_planner import (
    ExecutionPlanner,
    ExecutionPlan,
    ExecutionRecommendation,
    FundingSource,
)
from virtual_treasury_manager import AllocationResult, AllocationAction


class TestExecutionPlanner:

    def create_allocation(self, symbol: str, delta: float) -> AllocationResult:
        """Helper pour créer un AllocationResult simplifié."""
        return AllocationResult(
            symbol=symbol,
            goi=0.5,
            current_budget=100.0,
            current_allocation_pct=0.1,
            target_budget=100.0 + delta,
            target_allocation_pct=0.1 + delta / 1000.0,
            recommended_budget=100.0 + delta,
            allocation_pct=0.1 + delta / 1000.0,
            delta=delta,
            action=AllocationAction.INCREASE if delta > 0 else (
                AllocationAction.DECREASE if delta < 0 else AllocationAction.HOLD
            ),
            estimated_cycles=None,
        )

    # ---------------------------------------------------------------------
    # Cas A : Cash suffisant
    # ---------------------------------------------------------------------
    def test_cash_sufficient(self):
        """free_cash >= positive_need => toutes les augmentations sont financées par CASH."""
        allocations = [
            self.create_allocation("FIL", 19.0),
            self.create_allocation("INJ", 19.0),
            self.create_allocation("STX", -14.0),
        ]
        free_cash = 76.0

        plan = ExecutionPlanner.compute(allocations, free_cash)

        assert plan.cash_sufficient is True
        assert plan.needs_reallocation is False
        assert plan.reallocation_amount == 0.0
        assert plan.remaining_cash == pytest.approx(76.0 - 38.0)
        assert plan.execution_required is True
        assert plan.positive_need == 38.0
        assert plan.negative_supply == 14.0

        # Vérifier les recommandations
        recs = {r.symbol: r for r in plan.recommendations}
        assert recs["FIL"].funding_source == FundingSource.CASH
        assert recs["FIL"].cash_amount == 19.0
        assert recs["FIL"].reallocation_amount == 0.0

        assert recs["INJ"].funding_source == FundingSource.CASH
        assert recs["INJ"].cash_amount == 19.0
        assert recs["INJ"].reallocation_amount == 0.0

        assert recs["STX"].funding_source == FundingSource.NONE
        assert recs["STX"].cash_amount == 0.0
        assert recs["STX"].reallocation_amount == 0.0

    # ---------------------------------------------------------------------
    # Cas B : Cash insuffisant
    # ---------------------------------------------------------------------
    def test_cash_insufficient(self):
        """free_cash < positive_need => réallocation nécessaire."""
        allocations = [
            self.create_allocation("FIL", 25.0),
            self.create_allocation("INJ", 30.0),
            self.create_allocation("STX", -22.0),
            self.create_allocation("BTC", -13.0),
        ]
        free_cash = 20.0

        plan = ExecutionPlanner.compute(allocations, free_cash)

        assert plan.cash_sufficient is False
        assert plan.needs_reallocation is True
        assert plan.reallocation_amount == 35.0
        assert plan.remaining_cash == 0.0
        assert plan.execution_required is True
        assert plan.positive_need == 55.0
        assert plan.negative_supply == 35.0

        recs = {r.symbol: r for r in plan.recommendations}

        # FIL : besoin 25, cash réparti proportionnellement : (25/55)*20 ≈ 9.09
        # reallocation = 25 - 9.09 ≈ 15.91 => MIXED
        assert recs["FIL"].funding_source == FundingSource.MIXED
        assert recs["FIL"].cash_amount == pytest.approx((25/55)*20)
        assert recs["FIL"].reallocation_amount == pytest.approx(25 - (25/55)*20)

        # INJ : besoin 30, cash réparti : (30/55)*20 ≈ 10.91
        # reallocation = 30 - 10.91 ≈ 19.09 => MIXED
        assert recs["INJ"].funding_source == FundingSource.MIXED
        assert recs["INJ"].cash_amount == pytest.approx((30/55)*20)
        assert recs["INJ"].reallocation_amount == pytest.approx(30 - (30/55)*20)

        # STX et BTC : en diminution, REALLOCATION
        assert recs["STX"].funding_source == FundingSource.REALLOCATION
        assert recs["STX"].reallocation_amount == 22.0
        assert recs["BTC"].funding_source == FundingSource.REALLOCATION
        assert recs["BTC"].reallocation_amount == 13.0

    # ---------------------------------------------------------------------
    # Cas C : Aucun delta (tout à 0)
    # ---------------------------------------------------------------------
    def test_no_deltas(self):
        """Tous les deltas sont nuls => execution_required = False."""
        allocations = [
            self.create_allocation("BTC", 0.0),
            self.create_allocation("ETH", 0.0),
        ]
        free_cash = 100.0

        plan = ExecutionPlanner.compute(allocations, free_cash)

        assert plan.execution_required is False
        assert plan.cash_sufficient is True
        assert plan.needs_reallocation is False
        assert plan.positive_need == 0.0
        assert plan.negative_supply == 0.0
        assert plan.remaining_cash == 100.0

        for r in plan.recommendations:
            assert r.funding_source == FundingSource.NONE
            assert r.cash_amount == 0.0
            assert r.reallocation_amount == 0.0

    # ---------------------------------------------------------------------
    # Cas D : Cash exactement égal au besoin
    # ---------------------------------------------------------------------
    def test_cash_exact_need(self):
        """free_cash == positive_need => cash_sufficient True, remaining_cash = 0."""
        allocations = [
            self.create_allocation("FIL", 20.0),
            self.create_allocation("INJ", 20.0),
            self.create_allocation("STX", -40.0),
        ]
        free_cash = 40.0

        plan = ExecutionPlanner.compute(allocations, free_cash)

        assert plan.cash_sufficient is True
        assert plan.needs_reallocation is False
        assert plan.remaining_cash == 0.0
        assert plan.reallocation_amount == 0.0

        recs = {r.symbol: r for r in plan.recommendations}
        assert recs["FIL"].funding_source == FundingSource.CASH
        assert recs["INJ"].funding_source == FundingSource.CASH
        assert recs["STX"].funding_source == FundingSource.NONE

    # ---------------------------------------------------------------------
    # Cas E : Seulement des diminutions (ne devrait pas arriver normalement)
    # ---------------------------------------------------------------------
    def test_only_negative_deltas(self):
        """Si tous les deltas sont négatifs, positive_need = 0, execution_required = True (pour vendre)."""
        allocations = [
            self.create_allocation("BTC", -10.0),
            self.create_allocation("ETH", -5.0),
        ]
        free_cash = 0.0

        plan = ExecutionPlanner.compute(allocations, free_cash)

        assert plan.cash_sufficient is True  # 0 >= 0
        assert plan.needs_reallocation is False
        assert plan.positive_need == 0.0
        assert plan.execution_required is True  # negative_supply > 0

        for r in plan.recommendations:
            # On ne vend pas car cash suffisant (0 besoin)
            assert r.funding_source == FundingSource.NONE

    # ---------------------------------------------------------------------
    # Cas F : free_cash négatif (erreur)
    # ---------------------------------------------------------------------
    def test_negative_free_cash(self):
        allocations = [self.create_allocation("BTC", 10.0)]
        with pytest.raises(ValueError, match="free_cash ne peut pas être négatif"):
            ExecutionPlanner.compute(allocations, -5.0)

    # ---------------------------------------------------------------------
    # Cas G : Aucune allocation (erreur)
    # ---------------------------------------------------------------------
    def test_empty_allocations(self):
        with pytest.raises(ValueError, match="La liste des allocations ne peut pas être vide"):
            ExecutionPlanner.compute([], 100.0)
