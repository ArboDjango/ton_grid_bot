"""
tests/test_reconciliation_rn029_unallocated_capital.py

Tests couvrant RN-029 : le capital non alloué (Σ décisions <
capital_total) est un état légitime, jamais comblé par la
réconciliation en amplifiant des décisions déjà prises (I7, révision
de I2 de RN-028).

Contexte : incident du 21/07/2026 — un cycle avec 333.82 USDT de cash
libre contre ~67 USDT de décisions nettes issues du lissage forçait la
réconciliation à multiplier certaines décisions par plus de trois pour
satisfaire l'ancienne égalité stricte de I2.

Portée strictement respectée :
  - Ne teste que le comportement introduit par RN-029 (non-amplification,
    remaining_cash, dépassement toujours corrigé).
  - Ne réévalue pas les invariants déjà couverts par les étapes
    précédentes (I1, plancher strict, etc.), sauf pour vérifier qu'ils
    restent valides en présence de capital non alloué.
"""

import pytest

from virtual_treasury_manager import (
    VirtualTreasuryManager,
    StrategyState,
    AllocationAction,
    TreasuryReconciliationError,
)


class TestUnallocatedCapitalIsNeverFilled:
    def test_real_incident_scenario_no_longer_amplifies_decisions(self):
        # Reproduit exactement l'incident du 21/07/2026. Avant RN-029,
        # RSR/EGLD/INJ voyaient leurs deltas decides (~+36/+35/+32)
        # amplifies a plus de trois fois leur valeur (~+116/+119/+118)
        # pour combler le capital non alloue. Desormais, les deltas
        # doivent rester exactement ceux decides par le lissage.
        strategies = [
            StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
            StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
            StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
            StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
            StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
        ]
        free_usdt = 333.82

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        rsr = next(a for a in result.allocations if a.symbol == "RSRUSDT")
        egld = next(a for a in result.allocations if a.symbol == "EGLDUSDT")
        inj = next(a for a in result.allocations if a.symbol == "INJUSDT")

        # Les deltas doivent rester proches de ce que le lissage seul
        # decide (SMOOTHING_FACTOR=0.20 applique a l'ecart brut), pas
        # amplifies pour combler le cash libre. On verifie une borne
        # large (delta < 50) qui exclurait toute amplification a
        # plus de +100 observee avant le correctif.
        assert rsr.delta < 50.0
        assert egld.delta < 50.0
        assert inj.delta < 50.0

    def test_remaining_cash_is_positive_and_substantial_on_the_real_incident(self):
        strategies = [
            StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
            StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
            StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
            StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
            StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
        ]
        free_usdt = 333.82

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        # RN-029 : remaining_cash doit etre expose, positif, et
        # substantiel (pas juste une tolerance numerique).
        assert result.summary.remaining_cash > 100.0

    def test_reconcile_deltas_returns_decided_values_unchanged_when_no_overshoot(self):
        # Verification directe sur _reconcile_deltas() : si la somme
        # des decisions deja prises est deja <= capital_total, la
        # fonction ne doit rien modifier.
        decided_deltas = {"A": 15.0, "B": -20.0}
        current_budgets = {"A": 100.0, "B": 100.0}
        goi_dict = {"A": 0.5, "B": 0.5}
        capital_total = 300.0  # tres superieur a 100+15 + 100-20 = 195

        result = VirtualTreasuryManager._reconcile_deltas(
            decided_deltas, current_budgets,
            min_budget=10.0, max_budget=250.0,
            capital_total=capital_total, goi_dict=goi_dict,
        )

        assert result["A"] == pytest.approx(15.0, abs=1e-9)
        assert result["B"] == pytest.approx(-20.0, abs=1e-9)


class TestOvershootIsStillCorrected:
    """
    RN-029 ne change rien au cas du depassement : il doit toujours
    etre corrige par la reconciliation (I1, I3, I4 restent pleinement
    applicables), seule la direction "combler" est desormais interdite.
    """

    def test_overshoot_is_still_reduced(self):
        # Meme scenario que les tests de plancher strict, avec un
        # cash tres reduit. Verification que la conservation (I2,
        # version inegalite) reste respectee — meme ici, avec un
        # cash tres reduit, les fortes baisses de FIL/STX compensent
        # suffisamment pour qu'un peu de capital reste legitimement
        # non alloue (pas un depassement strict a resorber a
        # l'exact) : la propriete testee est simplement l'inegalite,
        # jamais violee.
        strategies = [
            StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
            StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
            StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
            StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
            StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
        ]
        free_usdt = 20.0

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        capital_total = sum(s.current_budget for s in strategies) + free_usdt
        sum_recommended = sum(a.recommended_budget for a in result.allocations)

        assert sum_recommended <= capital_total + 1e-6

    def test_infeasible_overshoot_still_raises(self):
        # Un depassement qui ne peut pas etre resorbe sans neutraliser
        # une decision doit toujours lever TreasuryReconciliationError
        # (I4, inchange par RN-029).
        decided_deltas = {"A": -50.0}
        current_budgets = {"A": 15.0}
        goi_dict = {"A": 0.5}

        with pytest.raises(TreasuryReconciliationError):
            VirtualTreasuryManager._reconcile_deltas(
                decided_deltas, current_budgets,
                min_budget=10.0, max_budget=500.0,
                capital_total=15.0, goi_dict=goi_dict,
            )

    def test_reconcile_deltas_reduces_when_decided_sum_exceeds_capital(self):
        # Verification directe : si la somme des decisions depasse
        # capital_total, la reduction doit s'appliquer (sens oppose au
        # cas de capital non alloue).
        decided_deltas = {"A": 80.0, "B": 80.0}
        current_budgets = {"A": 100.0, "B": 100.0}
        goi_dict = {"A": 0.5, "B": 0.5}
        capital_total = 250.0  # 100+80+100+80=360 > 250 : depassement de 110

        result = VirtualTreasuryManager._reconcile_deltas(
            decided_deltas, current_budgets,
            min_budget=10.0, max_budget=300.0,
            capital_total=capital_total, goi_dict=goi_dict,
        )

        total = sum(current_budgets[sym] + result[sym] for sym in current_budgets)
        assert total <= capital_total + 1e-6
        # Les deltas doivent avoir ete reduits (pas rester a 80 chacun).
        assert result["A"] < 80.0
        assert result["B"] < 80.0
        # Mais rester positifs (I1 : signe jamais inverse, jamais neutralise).
        assert result["A"] >= VirtualTreasuryManager.MIN_DELTA_ACTION - 1e-6
        assert result["B"] >= VirtualTreasuryManager.MIN_DELTA_ACTION - 1e-6
