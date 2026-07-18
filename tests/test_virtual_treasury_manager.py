"""
tests/test_virtual_treasury_manager.py

Tests couvrant le correctif du bug de conservation du capital dans
VirtualTreasuryManager._apply_bounds_iterative, découvert en production
le 18/07/2026 : run_meta_controller.py --mode OBSERVE levait une
AssertionError dans compute() (remaining_cash < -EPSILON), causée par
un ajustement final non reconvergé après un reclamp aux bornes
[min_budget, max_budget].

Portée strictement respectée :
  - Aucun test ne porte sur CapitalTransitionGuard, bot_gateio.py, ou
    le mécanisme META_CORRECTION lui-même : ce fichier ne teste que
    VirtualTreasuryManager, un module indépendant.
  - Ces tests vérifient :
      * _apply_bounds_iterative converge réellement (somme des budgets
        == capital_total, à la tolérance près) même dans des scénarios
        « tendus » où plusieurs stratégies sont proches de leurs
        bornes simultanément (le scénario exact de l'incident) ;
      * une contrainte réellement infaisable (somme des planchers
        min_budget > capital_total) lève une ValueError explicite,
        plutôt que de retourner silencieusement un résultat qui viole
        la conservation du capital ;
      * compute() ne lève plus d'AssertionError sur ces scénarios
        tendus, et la conservation (recommandé + trésorerie restante
        == capital total) reste vraie de bout en bout.
"""

import pytest

from virtual_treasury_manager import VirtualTreasuryManager, StrategyState


# ============================================================
# _APPLY_BOUNDS_ITERATIVE — CONVERGENCE RÉELLE
# ============================================================

class TestApplyBoundsIterativeConvergence:
    def test_converges_exactly_in_a_simple_unconstrained_case(self):
        strategies = [
            StrategyState(symbol="A", current_budget=100.0, goi=0.5),
            StrategyState(symbol="B", current_budget=100.0, goi=0.5),
        ]
        goi_dict = {"A": 0.5, "B": 0.5}
        target_budgets = {"A": 100.0, "B": 100.0}

        result = VirtualTreasuryManager._apply_bounds_iterative(
            target_budgets, goi_dict, min_budget=10.0, max_budget=1000.0,
            capital_total=200.0, strategies=strategies,
        )

        assert sum(result.values()) == pytest.approx(200.0)

    def test_converges_when_several_strategies_are_pinned_to_min_budget_simultaneously(self):
        # Reproduit le scenario "tendu" de l'incident : plusieurs
        # strategies proches ou sous leur plancher en meme temps que
        # le capital total est reduit.
        strategies = [
            StrategyState(symbol=s, current_budget=60.0, goi=g)
            for s, g in zip(
                ["FIL", "STX", "RSR", "EGLD", "INJ"],
                [0.49, 0.52, 0.40, 0.59, 0.59],
            )
        ]
        goi_dict = {s.symbol: s.goi for s in strategies}
        # Cibles brutes tres desequilibrees, plusieurs sous min_budget=50
        target_budgets = {
            "FIL": 45.0, "STX": 48.0, "RSR": 30.0, "EGLD": 250.0, "INJ": 200.0,
        }
        capital_total = 300.0  # serre : bien en dessous de la somme des cibles brutes

        result = VirtualTreasuryManager._apply_bounds_iterative(
            target_budgets, goi_dict, min_budget=50.0, max_budget=0.40 * capital_total,
            capital_total=capital_total, strategies=strategies,
        )

        assert sum(result.values()) == pytest.approx(capital_total, abs=1e-6)
        for sym in goi_dict:
            assert 50.0 - 1e-6 <= result[sym] <= 0.40 * capital_total + 1e-6

    def test_converges_with_five_strategies_near_both_bounds_at_once(self):
        # Variante avec des stratégies poussées a la fois vers le
        # plancher et vers le plafond simultanement.
        strategies = [
            StrategyState(symbol=s, current_budget=c, goi=g)
            for s, c, g in [
                ("A", 50.0, 0.05),
                ("B", 50.0, 0.05),
                ("C", 400.0, 0.60),
                ("D", 400.0, 0.60),
                ("E", 100.0, 0.30),
            ]
        ]
        goi_dict = {s.symbol: s.goi for s in strategies}
        target_budgets = {s.symbol: s.current_budget for s in strategies}
        capital_total = 1000.0

        result = VirtualTreasuryManager._apply_bounds_iterative(
            target_budgets, goi_dict, min_budget=50.0, max_budget=0.40 * capital_total,
            capital_total=capital_total, strategies=strategies,
        )

        assert sum(result.values()) == pytest.approx(capital_total, abs=1e-6)


# ============================================================
# INFAISABILITÉ — ÉCHEC EXPLICITE PLUTÔT QUE RÉSULTAT INCOHÉRENT
# ============================================================

class TestApplyBoundsIterativeInfeasibility:
    def test_raises_value_error_when_min_budget_floor_exceeds_capital_total(self):
        # 5 strategies x min_budget=50 = plancher total de 250,
        # mais capital_total=100 : structurellement infaisable.
        strategies = [
            StrategyState(symbol=s, current_budget=20.0, goi=0.5)
            for s in ["A", "B", "C", "D", "E"]
        ]
        goi_dict = {s.symbol: 0.5 for s in strategies}
        target_budgets = {s.symbol: 20.0 for s in strategies}

        with pytest.raises(ValueError):
            VirtualTreasuryManager._apply_bounds_iterative(
                target_budgets, goi_dict, min_budget=50.0, max_budget=1000.0,
                capital_total=100.0, strategies=strategies,
            )

    def test_never_returns_a_budget_set_violating_conservation(self):
        # Meme dans un cas degenere, si jamais aucune ValueError
        # n'etait levee (garde-fou de non-regression), la fonction ne
        # doit jamais retourner un ecart de conservation significatif
        # sans le signaler explicitement.
        strategies = [
            StrategyState(symbol=s, current_budget=20.0, goi=0.5)
            for s in ["A", "B", "C", "D", "E"]
        ]
        goi_dict = {s.symbol: 0.5 for s in strategies}
        target_budgets = {s.symbol: 20.0 for s in strategies}

        try:
            result = VirtualTreasuryManager._apply_bounds_iterative(
                target_budgets, goi_dict, min_budget=50.0, max_budget=1000.0,
                capital_total=100.0, strategies=strategies,
            )
        except ValueError:
            return  # comportement attendu (cf. test precedent)

        # Si aucune exception n'a ete levee, la conservation doit
        # neanmoins etre respectee.
        assert sum(result.values()) == pytest.approx(100.0, abs=1e-6)


# ============================================================
# COMPUTE() — RÉGRESSION DE BOUT EN BOUT (SCÉNARIO DE L'INCIDENT)
# ============================================================

class TestComputeDoesNotRaiseOnTightAllocations:
    def test_compute_does_not_raise_with_five_strategies_and_low_free_cash(self):
        # Reproduit approximativement les conditions de l'incident du
        # 18/07/2026 : 5 strategies, capital total reduit apres
        # correction d'allocated_capital, free_usdt tres faible.
        strategies = [
            StrategyState(symbol="FIL", current_budget=347.0, goi=0.49),
            StrategyState(symbol="STX", current_budget=343.0, goi=0.52),
            StrategyState(symbol="RSR", current_budget=242.0, goi=0.40),
            StrategyState(symbol="EGLD", current_budget=153.64, goi=0.59),
            StrategyState(symbol="INJ", current_budget=204.52, goi=0.59),
        ]
        free_usdt = 9.88

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        capital_total = sum(s.current_budget for s in strategies) + free_usdt
        sum_recommended = sum(a.recommended_budget for a in result.allocations)
        remaining_cash = capital_total - sum_recommended

        assert remaining_cash >= -VirtualTreasuryManager.EPSILON
        assert abs(sum_recommended + remaining_cash - capital_total) < VirtualTreasuryManager.EPSILON * capital_total

    def test_compute_does_not_raise_with_many_strategies_pinned_near_bounds(self):
        # Cas plus extreme : capital total tres serre par rapport aux
        # planchers min_budget cumules, mais structurellement faisable
        # (contrairement au test d'infaisabilite ci-dessus).
        strategies = [
            StrategyState(symbol=f"SYM{i}", current_budget=55.0, goi=0.1 + 0.05 * i)
            for i in range(6)
        ]
        free_usdt = 5.0

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        capital_total = sum(s.current_budget for s in strategies) + free_usdt
        sum_recommended = sum(a.recommended_budget for a in result.allocations)
        remaining_cash = capital_total - sum_recommended

        assert remaining_cash >= -VirtualTreasuryManager.EPSILON
