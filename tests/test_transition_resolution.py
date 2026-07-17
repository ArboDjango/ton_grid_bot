"""
tests/test_transition_resolution.py

Tests couvrant exclusivement l'étape 3 du plan de reconstruction du
CapitalTransitionGuard : la résolution pure d'une TransitionValue
(AbsoluteAmount ou RelativeCorrection) en AppliedDelta, contre un
EconomicState explicitement fourni.

Portée strictement respectée :
  - Aucun test de persistance (aucun état n'est lu depuis ou écrit
    vers un support quelconque ; l'EconomicState est toujours injecté
    directement par le test, comme le prévoit l'étape 3 du plan :
    « résolution des règles relatives contre un état simulé »).
  - Aucun test de journalisation.
  - Aucun test d'écriture d'état économique réel : resolve_transition_value
    ne modifie jamais l'EconomicState qu'on lui fournit.
  - Aucun test de validation structurelle (rôle de l'étape 2, déjà
    couvert par test_transition_validation.py) : les valeurs utilisées
    ici sont supposées déjà structurellement valides.
  - Aucun test de décision d'application (TransitionStatus) : la
    résolution ne se prononce jamais sur l'acceptation, la troncature
    ou le rejet d'une transition.

Chaque test vérifie uniquement le contrat de la fonction pure
resolve_transition_value : mêmes entrées, mêmes sorties, sans effet
de bord.
"""

import dataclasses

import pytest

from capital_transition_guard import (
    AbsoluteAmount,
    AppliedDelta,
    EconomicState,
    RelativeCorrection,
    resolve_transition_value,
)


# ============================================================
# RESOLUTION D'UN ABSOLUTEAMOUNT
# ============================================================

class TestResolveAbsoluteAmount:
    def test_resolves_to_an_applied_delta_with_the_same_amount(self):
        value = AbsoluteAmount(amount=42.0)
        state = EconomicState(allocated_capital=220.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=42.0)

    def test_resolves_negative_amount_unchanged(self):
        value = AbsoluteAmount(amount=-13.5)
        state = EconomicState(allocated_capital=220.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=-13.5)

    def test_resolution_is_independent_of_current_state(self):
        # Un montant absolu (profit/perte réalisés, synchronisation
        # exceptionnelle) ne dépend pas de l'état courant pour être
        # résolu : le résultat doit être identique quel que soit
        # l'EconomicState fourni.
        value = AbsoluteAmount(amount=25.0)

        result_a = resolve_transition_value(value, EconomicState(allocated_capital=0.0))
        result_b = resolve_transition_value(value, EconomicState(allocated_capital=500.0))
        result_c = resolve_transition_value(value, EconomicState(allocated_capital=-100.0))

        assert result_a == result_b == result_c == AppliedDelta(amount=25.0)

    def test_returns_an_applied_delta_instance(self):
        value = AbsoluteAmount(amount=1.0)
        state = EconomicState(allocated_capital=100.0)

        result = resolve_transition_value(value, state)

        assert isinstance(result, AppliedDelta)
        assert not isinstance(result, AbsoluteAmount)


# ============================================================
# RESOLUTION D'UNE RELATIVECORRECTION
# ============================================================

class TestResolveRelativeCorrection:
    def test_resolves_positive_fraction_against_current_state(self):
        value = RelativeCorrection(fraction=0.05)
        state = EconomicState(allocated_capital=220.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=11.0)  # 0.05 * 220.0

    def test_resolves_negative_fraction_against_current_state(self):
        value = RelativeCorrection(fraction=-0.10)
        state = EconomicState(allocated_capital=300.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=-30.0)  # -0.10 * 300.0

    def test_resolves_to_zero_when_fraction_is_zero(self):
        value = RelativeCorrection(fraction=0.0)
        state = EconomicState(allocated_capital=220.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=0.0)

    def test_resolves_to_zero_when_allocated_capital_is_zero(self):
        value = RelativeCorrection(fraction=0.5)
        state = EconomicState(allocated_capital=0.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=0.0)

    def test_same_fraction_resolves_differently_against_different_states(self):
        # Contrairement à AbsoluteAmount, une RelativeCorrection doit
        # produire des résultats différents selon l'état fourni : la
        # résolution dépend explicitement de current_state.
        value = RelativeCorrection(fraction=0.10)

        result_a = resolve_transition_value(value, EconomicState(allocated_capital=100.0))
        result_b = resolve_transition_value(value, EconomicState(allocated_capital=200.0))

        assert result_a == AppliedDelta(amount=10.0)
        assert result_b == AppliedDelta(amount=20.0)
        assert result_a != result_b

    def test_resolves_correctly_against_a_negative_allocated_capital(self):
        # Cas limite structurel : la fonction ne juge pas si un
        # allocated_capital négatif est économiquement valide (ce
        # n'est pas son rôle) ; elle applique simplement la
        # multiplication.
        value = RelativeCorrection(fraction=0.2)
        state = EconomicState(allocated_capital=-50.0)

        result = resolve_transition_value(value, state)

        assert result == AppliedDelta(amount=-10.0)


# ============================================================
# RESOLUTION CONTRE L'ETAT AU MOMENT DE L'APPLICATION,
# JAMAIS CONTRE UN ETAT FIGE A L'EMISSION (clarification RN-023)
# ============================================================

class TestResolutionUsesOnlyTheProvidedState:
    def test_resolution_never_reads_any_state_other_than_the_one_provided(self):
        # La fonction ne doit avoir aucune notion d'un état "mémorisé"
        # d'un appel précédent : chaque appel est résolu uniquement
        # contre l'EconomicState explicitement passé en paramètre à
        # cet appel précis.
        value = RelativeCorrection(fraction=0.10)

        stale_state = EconomicState(allocated_capital=100.0)
        fresh_state = EconomicState(allocated_capital=350.0)

        # Un premier appel avec un état "ancien" ne doit influencer en
        # rien un second appel avec un état "frais" : pas de mémoire
        # interne, pas de cache.
        resolve_transition_value(value, stale_state)
        result = resolve_transition_value(value, fresh_state)

        assert result == AppliedDelta(amount=35.0)  # 0.10 * 350.0, jamais * 100.0


# ============================================================
# TYPE NON PRIS EN CHARGE
# ============================================================

class TestUnsupportedValueType:
    def test_raises_type_error_for_a_plain_float(self):
        state = EconomicState(allocated_capital=100.0)

        with pytest.raises(TypeError):
            resolve_transition_value(10.0, state)  # type: ignore[arg-type]

    def test_raises_type_error_for_none(self):
        state = EconomicState(allocated_capital=100.0)

        with pytest.raises(TypeError):
            resolve_transition_value(None, state)  # type: ignore[arg-type]


# ============================================================
# ABSENCE D'EFFET DE BORD
# ============================================================

class TestResolutionHasNoSideEffects:
    def test_does_not_mutate_the_provided_state(self):
        value = RelativeCorrection(fraction=0.1)
        state = EconomicState(allocated_capital=220.0)

        resolve_transition_value(value, state)

        # EconomicState est un frozen dataclass : toute tentative de
        # mutation aurait de toute facon leve une exception ; ce test
        # confirme simplement que la valeur reste inchangee.
        assert state.allocated_capital == 220.0

    def test_does_not_mutate_the_provided_value(self):
        value = RelativeCorrection(fraction=0.1)
        state = EconomicState(allocated_capital=220.0)

        resolve_transition_value(value, state)

        assert value.fraction == 0.1

    def test_state_remains_a_frozen_instance_after_resolution(self):
        value = AbsoluteAmount(amount=5.0)
        state = EconomicState(allocated_capital=220.0)

        resolve_transition_value(value, state)

        with pytest.raises(dataclasses.FrozenInstanceError):
            state.allocated_capital = 999.0


# ============================================================
# PURETE DE LA FONCTION
# ============================================================

class TestResolutionFunctionPurity:
    def test_calling_twice_with_same_inputs_yields_identical_result(self):
        value = RelativeCorrection(fraction=0.05)
        state = EconomicState(allocated_capital=220.0)

        first = resolve_transition_value(value, state)
        second = resolve_transition_value(value, state)

        assert first == second

    def test_result_does_not_carry_any_reference_to_a_status_decision(self):
        # La résolution ne doit produire aucune notion d'acceptation,
        # de troncature ou de rejet : uniquement un montant résolu.
        value = RelativeCorrection(fraction=0.05)
        state = EconomicState(allocated_capital=220.0)

        result = resolve_transition_value(value, state)

        assert not hasattr(result, "status")
        assert not hasattr(result, "reason")
