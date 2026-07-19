"""Tests de non-régression de l'agrégation économique RN-026."""

import unittest

from virtual_treasury_manager import StrategyState, VirtualTreasuryManager


class TestRN026EconomicCapital(unittest.TestCase):
    def test_capital_total_counts_shared_cash_once(self):
        """Les budgets transmis au VTM sont les inventaires, pas des consignes."""
        result = VirtualTreasuryManager.compute(
            strategies=[
                StrategyState(symbol="AAAUSDT", current_budget=600.0, goi=0.5),
                StrategyState(symbol="BBBUSDT", current_budget=900.0, goi=0.5),
            ],
            free_usdt=74.77,
        )

        self.assertAlmostEqual(result.summary.capital_total, 1574.77)

    def test_strategy_value_cannot_be_replaced_by_a_pilot_instruction(self):
        """Une hausse de consigne n'est pas une entrée du calcul VTM."""
        inventory_values = [600.0, 900.0]
        result = VirtualTreasuryManager.compute(
            strategies=[
                StrategyState(symbol="AAAUSDT", current_budget=inventory_values[0], goi=0.5),
                StrategyState(symbol="BBBUSDT", current_budget=inventory_values[1], goi=0.5),
            ],
            free_usdt=74.77,
        )

        self.assertAlmostEqual(
            result.summary.capital_total, sum(inventory_values) + 74.77
        )
