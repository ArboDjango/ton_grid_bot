"""
tests/test_reconciliation_step1_classify_delta_sign.py

Tests couvrant exclusivement l'étape 1 de l'implémentation de
RN-027/RN-028 : _classify_delta_sign(), fonction pure et isolée,
pas encore câblée dans le pipeline de compute().

Portée strictement respectée :
  - Ne teste que _classify_delta_sign() elle-même.
  - Ne réévalue aucune autre partie de VirtualTreasuryManager.compute()
    (le câblage dans le pipeline fait l'objet des étapes 2 et 3).
"""

import pytest

from virtual_treasury_manager import VirtualTreasuryManager, AllocationAction


class TestClassifyDeltaSign:
    def test_large_positive_delta_is_increase(self):
        result = VirtualTreasuryManager._classify_delta_sign(50.0)
        assert result is AllocationAction.INCREASE

    def test_large_negative_delta_is_decrease(self):
        result = VirtualTreasuryManager._classify_delta_sign(-50.0)
        assert result is AllocationAction.DECREASE

    def test_zero_delta_is_hold(self):
        result = VirtualTreasuryManager._classify_delta_sign(0.0)
        assert result is AllocationAction.HOLD

    def test_small_positive_delta_below_threshold_is_hold(self):
        # MIN_DELTA_ACTION = 10.0
        result = VirtualTreasuryManager._classify_delta_sign(5.0)
        assert result is AllocationAction.HOLD

    def test_small_negative_delta_below_threshold_is_hold(self):
        result = VirtualTreasuryManager._classify_delta_sign(-5.0)
        assert result is AllocationAction.HOLD

    def test_delta_exactly_at_threshold_is_not_hold(self):
        # |delta| < MIN_DELTA_ACTION est la condition de HOLD (strict) ;
        # une valeur egale au seuil ne doit donc pas etre HOLD.
        threshold = VirtualTreasuryManager.MIN_DELTA_ACTION
        result = VirtualTreasuryManager._classify_delta_sign(threshold)
        assert result is AllocationAction.INCREASE

    def test_delta_just_below_threshold_is_hold(self):
        threshold = VirtualTreasuryManager.MIN_DELTA_ACTION
        result = VirtualTreasuryManager._classify_delta_sign(threshold - 0.01)
        assert result is AllocationAction.HOLD

    def test_negative_delta_exactly_at_threshold_is_not_hold(self):
        threshold = VirtualTreasuryManager.MIN_DELTA_ACTION
        result = VirtualTreasuryManager._classify_delta_sign(-threshold)
        assert result is AllocationAction.DECREASE

    def test_is_pure_same_input_yields_same_output(self):
        a = VirtualTreasuryManager._classify_delta_sign(42.0)
        b = VirtualTreasuryManager._classify_delta_sign(42.0)
        assert a is b  # meme membre d'enum, comparaison par identite valide

    def test_returns_an_allocation_action_instance(self):
        result = VirtualTreasuryManager._classify_delta_sign(100.0)
        assert isinstance(result, AllocationAction)
