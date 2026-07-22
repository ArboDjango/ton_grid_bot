"""
tests/test_reconciliation_step4_skeleton.py

Tests couvrant exclusivement l'étape 4 de l'implémentation de
RN-027/RN-028 : le squelette de _reconcile_deltas(), qui ne contient
que les validations de préconditions. L'algorithme lui-même n'est pas
encore implémenté (NotImplementedError attendue pour tout appel
respectant les préconditions).

Portée strictement respectée :
  - Ne teste que les préconditions du contrat (§3 de la spécification
    RN-027/RN-028 v2).
  - Ne teste ni les postconditions (I1-I3), ni TreasuryReconciliationError
    en tant que cas déclenché par un algorithme (l'algorithme n'existe
    pas encore) — uniquement que la classe existe et est correctement
    formée, et que _reconcile_deltas() lève bien NotImplementedError
    une fois les préconditions validées.
  - Ne câble pas _reconcile_deltas() dans compute() (étape 5).
"""

import pytest

from virtual_treasury_manager import VirtualTreasuryManager, TreasuryReconciliationError


def _valid_inputs():
    return dict(
        decided_deltas={"A": 10.0, "B": -10.0},
        current_budgets={"A": 100.0, "B": 100.0},
        min_budget=50.0,
        max_budget=150.0,
        capital_total=200.0,
        goi_dict={"A": 0.5, "B": 0.5},
    )


class TestReconcileDeltasPreconditions:
    def test_mismatched_keys_between_decided_deltas_and_current_budgets_raises(self):
        kwargs = _valid_inputs()
        kwargs["decided_deltas"] = {"A": 10.0, "C": -10.0}  # "C" au lieu de "B"

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_mismatched_keys_between_goi_dict_and_current_budgets_raises(self):
        kwargs = _valid_inputs()
        kwargs["goi_dict"] = {"A": 0.5}  # "B" manquant

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_non_positive_capital_total_raises(self):
        kwargs = _valid_inputs()
        kwargs["capital_total"] = 0.0

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_negative_capital_total_raises(self):
        kwargs = _valid_inputs()
        kwargs["capital_total"] = -50.0

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_min_budget_greater_than_max_budget_raises(self):
        kwargs = _valid_inputs()
        kwargs["min_budget"] = 200.0
        kwargs["max_budget"] = 100.0

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_negative_min_budget_raises(self):
        kwargs = _valid_inputs()
        kwargs["min_budget"] = -10.0

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_current_budget_below_min_budget_raises(self):
        kwargs = _valid_inputs()
        kwargs["current_budgets"] = {"A": 10.0, "B": 100.0}  # A < min_budget=50

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_current_budget_above_max_budget_raises(self):
        kwargs = _valid_inputs()
        kwargs["current_budgets"] = {"A": 300.0, "B": 100.0}  # A > max_budget=150

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_negative_goi_raises(self):
        kwargs = _valid_inputs()
        kwargs["goi_dict"] = {"A": -0.1, "B": 0.5}

        with pytest.raises(ValueError):
            VirtualTreasuryManager._reconcile_deltas(**kwargs)

    def test_zero_goi_is_accepted_by_preconditions(self):
        # goi_dict[sym] >= 0 est la precondition (pas > 0) : zero doit
        # etre accepte au niveau des preconditions elles-memes.
        #
        # MISE A JOUR DELIBEREE (etape 6) : l'algorithme est desormais
        # implemente ; un GOI nul est gere par le repli "distribution
        # equitable" au sein du groupe (cf. algorithme), donc un
        # resultat valide est retourne, plus une NotImplementedError.
        kwargs = _valid_inputs()
        kwargs["goi_dict"] = {"A": 0.0, "B": 0.5}

        result = VirtualTreasuryManager._reconcile_deltas(**kwargs)

        assert set(result.keys()) == {"A", "B"}
        total_current = sum(kwargs["current_budgets"].values())
        assert sum(result.values()) == pytest.approx(
            kwargs["capital_total"] - total_current, abs=1e-6
        )


class TestReconcileDeltasSkeletonNotYetImplemented:
    def test_valid_inputs_return_a_reconciled_result(self):
        # MISE A JOUR DELIBEREE (etape 6) : l'algorithme de
        # reconciliation est desormais implemente (Famille C). Ce test
        # verifiait auparavant NotImplementedError (etape 4, squelette
        # sans algorithme) ; il verifie maintenant qu'un resultat
        # conforme au contrat (§3) est bien retourne.
        kwargs = _valid_inputs()

        result = VirtualTreasuryManager._reconcile_deltas(**kwargs)

        assert set(result.keys()) == set(kwargs["current_budgets"].keys())
        total_current = sum(kwargs["current_budgets"].values())
        assert sum(result.values()) == pytest.approx(
            kwargs["capital_total"] - total_current, abs=1e-6
        )

    def test_not_implemented_error_is_raised_after_preconditions_not_instead_of(self):
        # Garde-fou : verifie que des preconditions invalides levent
        # bien ValueError (pas NotImplementedError) — c'est-a-dire que
        # les validations sont reellement executees avant le
        # NotImplementedError final, pas court-circuitees.
        kwargs = _valid_inputs()
        kwargs["capital_total"] = -1.0

        with pytest.raises(ValueError) as exc_info:
            VirtualTreasuryManager._reconcile_deltas(**kwargs)
        assert not isinstance(exc_info.value, NotImplementedError)


class TestTreasuryReconciliationErrorShape:
    def test_is_a_value_error_subclass(self):
        assert issubclass(TreasuryReconciliationError, ValueError)

    def test_carries_residual_and_saturation_attributes(self):
        err = TreasuryReconciliationError(
            "message de test",
            residual=12.5,
            saturation={"A": {"delta": 5.0, "lo": 0.0, "hi": 5.0}},
        )

        assert err.residual == 12.5
        assert err.saturation == {"A": {"delta": 5.0, "lo": 0.0, "hi": 5.0}}
        assert str(err) == "message de test"

    def test_can_be_raised_and_caught_as_value_error(self):
        with pytest.raises(ValueError):
            raise TreasuryReconciliationError("x", residual=1.0, saturation={})
