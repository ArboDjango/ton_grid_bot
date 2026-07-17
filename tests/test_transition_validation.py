"""
tests/test_transition_validation.py

Tests couvrant exclusivement l'étape 2 du plan de reconstruction du
CapitalTransitionGuard : la validation pure des TransitionRequest.

Portée strictement respectée :
  - Aucun test de persistance (rien n'est lu ni écrit sur disque ou en
    mémoire d'état).
  - Aucun test de résolution d'une RelativeCorrection contre un état
    économique réel : ces demandes sont validées uniquement sur leur
    structure, jamais sur le montant qu'elles produiraient une fois
    résolues.
  - Aucun test de journalisation.
  - Aucun test de TransitionStatus (ACCEPTED / TRUNCATED / REJECTED) :
    ce statut relève de la décision d'application par le Guard, pas de
    la validation structurelle testée ici.
  - Aucun test ne vérifie de comportement du CapitalTransitionGuard
    lui-même (submit_transition reste non implémentée et hors
    périmètre de cette étape).

Chaque test vérifie uniquement le contrat de la fonction pure
validate_transition_request : mêmes entrées, mêmes sorties, sans
effet de bord.
"""

import math

import pytest

from capital_transition_guard import (
    AbsoluteAmount,
    RelativeCorrection,
    TransitionCause,
    TransitionOrigin,
    TransitionRequest,
    ValidationOutcome,
    validate_transition_request,
)


def _make_request(
    bot_id="gateio_rsrusdt",
    cause=TransitionCause.REALIZED_PROFIT,
    origin=TransitionOrigin.BOT,
    value=None,
    justification=None,
):
    if value is None:
        value = AbsoluteAmount(amount=10.0)
    return TransitionRequest(
        bot_id=bot_id,
        cause=cause,
        origin=origin,
        value=value,
        justification=justification,
    )


# ============================================================
# VALIDATIONOUTCOME — STRUCTURE
# ============================================================

class TestValidationOutcomeStructure:
    def test_is_a_frozen_dataclass_with_expected_fields(self):
        import dataclasses

        assert dataclasses.is_dataclass(ValidationOutcome)
        assert ValidationOutcome.__dataclass_params__.frozen is True
        field_names = {f.name for f in dataclasses.fields(ValidationOutcome)}
        assert field_names == {"is_valid", "reason"}

    def test_reason_defaults_to_none(self):
        outcome = ValidationOutcome(is_valid=True)
        assert outcome.reason is None

    def test_is_immutable(self):
        import dataclasses

        outcome = ValidationOutcome(is_valid=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.is_valid = False


# ============================================================
# CAS VALIDES — UN PAR CAUSE
# ============================================================

class TestValidRequestsPassForEachCause:
    def test_realized_profit_with_bot_origin_and_absolute_amount_is_valid(self):
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=12.3),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is True
        assert outcome.reason is None

    def test_realized_loss_with_bot_origin_and_negative_absolute_amount_is_valid(self):
        request = _make_request(
            cause=TransitionCause.REALIZED_LOSS,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=-4.5),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is True

    def test_meta_correction_with_metacontroller_origin_and_relative_correction_is_valid(self):
        request = _make_request(
            cause=TransitionCause.META_CORRECTION,
            origin=TransitionOrigin.META_CONTROLLER,
            value=RelativeCorrection(fraction=0.05),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is True

    def test_manual_sync_with_operator_origin_absolute_amount_and_justification_is_valid(self):
        request = _make_request(
            cause=TransitionCause.MANUAL_SYNC,
            origin=TransitionOrigin.OPERATOR,
            value=AbsoluteAmount(amount=50.0),
            justification="Reconciliation post-incident #482",
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is True


# ============================================================
# BOT_ID
# ============================================================

class TestBotIdValidation:
    def test_empty_bot_id_is_invalid(self):
        request = _make_request(bot_id="")
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "bot_id" in outcome.reason

    def test_whitespace_only_bot_id_is_invalid(self):
        request = _make_request(bot_id="   ")
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "bot_id" in outcome.reason


# ============================================================
# ORIGINE INCOMPATIBLE AVEC LA CAUSE
# ============================================================

class TestOriginCauseCompatibility:
    @pytest.mark.parametrize(
        "cause,invalid_origin,value",
        [
            (
                TransitionCause.REALIZED_PROFIT,
                TransitionOrigin.META_CONTROLLER,
                AbsoluteAmount(amount=1.0),
            ),
            (
                TransitionCause.REALIZED_LOSS,
                TransitionOrigin.OPERATOR,
                AbsoluteAmount(amount=-1.0),
            ),
            (
                TransitionCause.META_CORRECTION,
                TransitionOrigin.BOT,
                RelativeCorrection(fraction=0.1),
            ),
            (
                TransitionCause.MANUAL_SYNC,
                TransitionOrigin.BOT,
                AbsoluteAmount(amount=1.0),
            ),
        ],
    )
    def test_incompatible_origin_is_rejected(self, cause, invalid_origin, value):
        request = _make_request(
            cause=cause,
            origin=invalid_origin,
            value=value,
            justification="motif" if cause is TransitionCause.MANUAL_SYNC else None,
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "origine" in outcome.reason or "origin" in outcome.reason.lower()


# ============================================================
# TYPE DE VALEUR INCOMPATIBLE AVEC LA CAUSE
# ============================================================

class TestValueTypeCauseCompatibility:
    def test_realized_profit_with_relative_correction_is_invalid(self):
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=RelativeCorrection(fraction=0.1),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "AbsoluteAmount" in outcome.reason

    def test_meta_correction_with_absolute_amount_is_invalid(self):
        request = _make_request(
            cause=TransitionCause.META_CORRECTION,
            origin=TransitionOrigin.META_CONTROLLER,
            value=AbsoluteAmount(amount=10.0),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "RelativeCorrection" in outcome.reason

    def test_manual_sync_with_relative_correction_is_invalid(self):
        request = _make_request(
            cause=TransitionCause.MANUAL_SYNC,
            origin=TransitionOrigin.OPERATOR,
            value=RelativeCorrection(fraction=0.1),
            justification="motif",
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "AbsoluteAmount" in outcome.reason


# ============================================================
# VALEURS NUMERIQUES NON FINIES
# ============================================================

class TestNonFiniteNumericValues:
    @pytest.mark.parametrize("bad_amount", [math.nan, math.inf, -math.inf])
    def test_absolute_amount_rejects_non_finite_values(self, bad_amount):
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=bad_amount),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "fini" in outcome.reason

    @pytest.mark.parametrize("bad_fraction", [math.nan, math.inf, -math.inf])
    def test_relative_correction_rejects_non_finite_values(self, bad_fraction):
        request = _make_request(
            cause=TransitionCause.META_CORRECTION,
            origin=TransitionOrigin.META_CONTROLLER,
            value=RelativeCorrection(fraction=bad_fraction),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "fini" in outcome.reason

    def test_boolean_amount_is_rejected_despite_being_an_int_subclass(self):
        # bool est une sous-classe d'int en Python ; ce test garantit
        # qu'un booleen n'est pas silencieusement accepte comme un
        # montant valide.
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=True),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False


# ============================================================
# JUSTIFICATION OBLIGATOIRE POUR MANUAL_SYNC
# ============================================================

class TestManualSyncJustification:
    def test_manual_sync_without_justification_is_invalid(self):
        request = _make_request(
            cause=TransitionCause.MANUAL_SYNC,
            origin=TransitionOrigin.OPERATOR,
            value=AbsoluteAmount(amount=10.0),
            justification=None,
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "justification" in outcome.reason

    def test_manual_sync_with_whitespace_only_justification_is_invalid(self):
        request = _make_request(
            cause=TransitionCause.MANUAL_SYNC,
            origin=TransitionOrigin.OPERATOR,
            value=AbsoluteAmount(amount=10.0),
            justification="   ",
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is False
        assert "justification" in outcome.reason

    def test_other_causes_do_not_require_justification(self):
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=10.0),
            justification=None,
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is True


# ============================================================
# PURETE DE LA FONCTION
# ============================================================

class TestValidationFunctionPurity:
    def test_calling_twice_with_same_request_yields_identical_outcome(self):
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=42.0),
        )
        first = validate_transition_request(request)
        second = validate_transition_request(request)
        assert first == second

    def test_validation_does_not_mutate_the_request(self):
        request = _make_request(
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=42.0),
        )
        snapshot_value = request.value
        snapshot_bot_id = request.bot_id
        validate_transition_request(request)
        assert request.value == snapshot_value
        assert request.bot_id == snapshot_bot_id

    def test_validation_does_not_resolve_relative_correction_into_an_amount(self):
        # La validation ne doit produire aucune valeur resolue : elle
        # ne fait que constater que la structure est coherente pour la
        # cause META_CORRECTION, sans jamais calculer le montant que
        # cette correction produirait une fois appliquee.
        request = _make_request(
            cause=TransitionCause.META_CORRECTION,
            origin=TransitionOrigin.META_CONTROLLER,
            value=RelativeCorrection(fraction=0.2),
        )
        outcome = validate_transition_request(request)
        assert outcome.is_valid is True
        assert not hasattr(outcome, "applied_value")
        assert not hasattr(outcome, "resolved_amount")
