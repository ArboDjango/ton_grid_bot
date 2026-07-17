"""
tests/test_capital_transition_guard.py

Tests de contrat et de structure pour l'étape 1 du plan de
reconstruction du CapitalTransitionGuard (RN-022 / RN-023).

Portée strictement respectée :
  - Aucun test de logique métier (validation, résolution, contraintes
    locales, journalisation effective) : cette logique n'existe pas
    encore à l'étape 1 et ne doit pas être anticipée ici.
  - Ces tests vérifient uniquement :
      * la présence et les valeurs des énumérations ;
      * la structure des dataclasses (champs, types, valeurs par
        défaut) ;
      * l'immuabilité de tous les types de domaine (frozen=True) ;
      * que les méthodes publiques de CapitalTransitionGuard existent
        et lèvent NotImplementedError sans exécuter de logique.

Ces tests constituent une base de non-régression pour l'étape 1 :
ils devront continuer à passer, sans modification, lors des étapes
suivantes (une méthode qui cesse de lever NotImplementedError parce
qu'elle a été implémentée fera alors l'objet d'une suppression
délibérée du test correspondant, pas d'un échec silencieux).
"""

import dataclasses
import inspect
import time

import pytest

from capital_transition_guard import (
    AbsoluteAmount,
    AppliedDelta,
    CapitalTransitionGuard,
    CapitalTransitionJournalEntry,
    EconomicState,
    RelativeCorrection,
    TransitionCause,
    TransitionOrigin,
    TransitionRequest,
    TransitionResult,
    TransitionStatus,
    TransitionValue,
)


# ============================================================
# ÉNUMÉRATIONS
# ============================================================

class TestTransitionCause:
    """Vérifie le contrat de l'énumération TransitionCause (RN-023 §6)."""

    def test_contains_exactly_the_four_authorized_causes(self):
        expected = {
            "REALIZED_PROFIT",
            "REALIZED_LOSS",
            "META_CORRECTION",
            "MANUAL_SYNC",
        }
        actual = {member.value for member in TransitionCause}
        assert actual == expected

    def test_member_count_is_closed(self):
        # Toute cause supplémentaire doit être ajoutée explicitement
        # par une Requirement Note, pas de façon informelle : ce test
        # échoue volontairement si le nombre de causes change sans
        # revue.
        assert len(TransitionCause) == 4

    @pytest.mark.parametrize(
        "member_name",
        ["REALIZED_PROFIT", "REALIZED_LOSS", "META_CORRECTION", "MANUAL_SYNC"],
    )
    def test_each_expected_member_is_accessible_by_name(self, member_name):
        assert getattr(TransitionCause, member_name).name == member_name


class TestTransitionOrigin:
    """Vérifie le contrat de l'énumération TransitionOrigin (RN-023 §7)."""

    def test_contains_exactly_the_three_authorized_origins(self):
        expected = {"BOT", "META_CONTROLLER", "OPERATOR"}
        actual = {member.value for member in TransitionOrigin}
        assert actual == expected

    def test_member_count_is_closed(self):
        assert len(TransitionOrigin) == 3


class TestTransitionStatus:
    """Vérifie le contrat de l'énumération TransitionStatus (RN-023 §7-8)."""

    def test_contains_exactly_the_three_possible_statuses(self):
        expected = {"ACCEPTED", "TRUNCATED", "REJECTED"}
        actual = {member.value for member in TransitionStatus}
        assert actual == expected

    def test_member_count_is_closed(self):
        assert len(TransitionStatus) == 3


# ============================================================
# TYPES DE DOMAINE — VALEUR D'UNE TRANSITION
# ============================================================

class TestAbsoluteAmount:
    """AbsoluteAmount : montant absolu (REALIZED_PROFIT / REALIZED_LOSS / MANUAL_SYNC)."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(AbsoluteAmount)

    def test_is_frozen(self):
        assert AbsoluteAmount.__dataclass_params__.frozen is True

    def test_has_exactly_one_field_named_amount(self):
        field_names = {f.name for f in dataclasses.fields(AbsoluteAmount)}
        assert field_names == {"amount"}

    def test_accepts_positive_amount(self):
        value = AbsoluteAmount(amount=42.0)
        assert value.amount == 42.0

    def test_accepts_negative_amount(self):
        # Une perte réalisée est représentée par un montant négatif.
        value = AbsoluteAmount(amount=-13.5)
        assert value.amount == -13.5

    def test_is_immutable(self):
        value = AbsoluteAmount(amount=10.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.amount = 99.0

    def test_equality_is_value_based(self):
        assert AbsoluteAmount(amount=5.0) == AbsoluteAmount(amount=5.0)
        assert AbsoluteAmount(amount=5.0) != AbsoluteAmount(amount=6.0)


class TestRelativeCorrection:
    """RelativeCorrection : règle relative (META_CORRECTION), non résolue à ce stade."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(RelativeCorrection)

    def test_is_frozen(self):
        assert RelativeCorrection.__dataclass_params__.frozen is True

    def test_has_exactly_one_field_named_fraction(self):
        field_names = {f.name for f in dataclasses.fields(RelativeCorrection)}
        assert field_names == {"fraction"}

    def test_accepts_positive_and_negative_fraction(self):
        assert RelativeCorrection(fraction=0.05).fraction == 0.05
        assert RelativeCorrection(fraction=-0.03).fraction == -0.03

    def test_is_immutable(self):
        value = RelativeCorrection(fraction=0.1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.fraction = 0.2

    def test_does_not_expose_any_resolution_method(self):
        # L'étape 1 ne doit contenir aucune logique de résolution :
        # ce type ne doit porter aucune méthode publique de calcul.
        public_methods = [
            name
            for name, member in inspect.getmembers(RelativeCorrection)
            if not name.startswith("_") and inspect.isfunction(member)
        ]
        assert public_methods == []


class TestTransitionValueUnion:
    """TransitionValue : union fermée AbsoluteAmount | RelativeCorrection."""

    def test_absolute_amount_is_a_valid_transition_value(self):
        value: TransitionValue = AbsoluteAmount(amount=1.0)
        assert isinstance(value, AbsoluteAmount)

    def test_relative_correction_is_a_valid_transition_value(self):
        value: TransitionValue = RelativeCorrection(fraction=0.02)
        assert isinstance(value, RelativeCorrection)


class TestAppliedDelta:
    """AppliedDelta : montant résolu, toujours concret (jamais une règle relative)."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(AppliedDelta)

    def test_is_frozen(self):
        assert AppliedDelta.__dataclass_params__.frozen is True

    def test_has_exactly_one_field_named_amount(self):
        field_names = {f.name for f in dataclasses.fields(AppliedDelta)}
        assert field_names == {"amount"}

    def test_is_immutable(self):
        value = AppliedDelta(amount=7.5)
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.amount = 1.0

    def test_is_distinct_type_from_absolute_amount(self):
        # AppliedDelta et AbsoluteAmount représentent des concepts
        # différents (valeur demandée vs valeur résolue appliquée) et
        # ne doivent pas être confondus, même s'ils portent la même
        # forme structurelle.
        assert AppliedDelta is not AbsoluteAmount
        assert not isinstance(AppliedDelta(amount=1.0), AbsoluteAmount)


# ============================================================
# TYPE DE DOMAINE — ÉTAT ÉCONOMIQUE
# ============================================================

class TestEconomicState:
    """EconomicState : état économique du bot (RN-023 §9)."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(EconomicState)

    def test_is_frozen(self):
        assert EconomicState.__dataclass_params__.frozen is True

    def test_has_exactly_one_field_named_allocated_capital(self):
        field_names = {f.name for f in dataclasses.fields(EconomicState)}
        assert field_names == {"allocated_capital"}

    def test_stores_allocated_capital_value(self):
        state = EconomicState(allocated_capital=220.0)
        assert state.allocated_capital == 220.0

    def test_is_immutable(self):
        state = EconomicState(allocated_capital=220.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.allocated_capital = 999.0


# ============================================================
# DEMANDE DE TRANSITION
# ============================================================

class TestTransitionRequest:
    """TransitionRequest : structure de la demande (RN-023 §6)."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(TransitionRequest)

    def test_is_frozen(self):
        assert TransitionRequest.__dataclass_params__.frozen is True

    def test_has_expected_fields(self):
        field_names = {f.name for f in dataclasses.fields(TransitionRequest)}
        assert field_names == {
            "bot_id",
            "cause",
            "origin",
            "value",
            "requested_at",
            "justification",
            "metadata",
        }

    def test_can_be_constructed_with_absolute_amount(self):
        request = TransitionRequest(
            bot_id="gateio_rsrusdt",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=12.3),
        )
        assert request.bot_id == "gateio_rsrusdt"
        assert request.cause is TransitionCause.REALIZED_PROFIT
        assert request.origin is TransitionOrigin.BOT
        assert request.value == AbsoluteAmount(amount=12.3)

    def test_can_be_constructed_with_relative_correction(self):
        request = TransitionRequest(
            bot_id="gateio_rsrusdt",
            cause=TransitionCause.META_CORRECTION,
            origin=TransitionOrigin.META_CONTROLLER,
            value=RelativeCorrection(fraction=0.05),
        )
        assert request.value == RelativeCorrection(fraction=0.05)

    def test_justification_defaults_to_none(self):
        request = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_LOSS,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=-5.0),
        )
        assert request.justification is None

    def test_metadata_defaults_to_empty_mapping(self):
        request = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_LOSS,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=-5.0),
        )
        assert request.metadata == {}

    def test_requested_at_defaults_to_a_recent_timestamp(self):
        before = time.time()
        request = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=1.0),
        )
        after = time.time()
        assert before <= request.requested_at <= after

    def test_two_default_timestamps_are_independent_per_instance(self):
        # Le default_factory doit produire un horodatage propre à
        # chaque instance, pas une valeur partagée figée à
        # l'importation du module.
        request_a = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=1.0),
        )
        time.sleep(0.001)
        request_b = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=1.0),
        )
        assert request_b.requested_at >= request_a.requested_at

    def test_is_immutable(self):
        request = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=1.0),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.bot_id = "other_bot"


# ============================================================
# RÉSULTAT D'UNE TRANSITION
# ============================================================

class TestTransitionResult:
    """TransitionResult : structure unique pour les trois statuts (RN-023 §7-8)."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(TransitionResult)

    def test_is_frozen(self):
        assert TransitionResult.__dataclass_params__.frozen is True

    def test_has_expected_fields(self):
        field_names = {f.name for f in dataclasses.fields(TransitionResult)}
        assert field_names == {
            "status",
            "requested_value",
            "applied_value",
            "state_before",
            "state_after",
            "reason",
            "applied_at",
        }

    def test_can_represent_an_accepted_transition(self):
        result = TransitionResult(
            status=TransitionStatus.ACCEPTED,
            requested_value=AbsoluteAmount(amount=10.0),
            applied_value=AppliedDelta(amount=10.0),
            state_before=EconomicState(allocated_capital=220.0),
            state_after=EconomicState(allocated_capital=230.0),
            reason=None,
        )
        assert result.status is TransitionStatus.ACCEPTED
        assert result.reason is None

    def test_can_represent_a_truncated_transition(self):
        result = TransitionResult(
            status=TransitionStatus.TRUNCATED,
            requested_value=RelativeCorrection(fraction=0.5),
            applied_value=AppliedDelta(amount=5.0),
            state_before=EconomicState(allocated_capital=220.0),
            state_after=EconomicState(allocated_capital=225.0),
            reason="plafond de correction atteint",
        )
        assert result.status is TransitionStatus.TRUNCATED
        assert result.reason == "plafond de correction atteint"

    def test_can_represent_a_rejected_transition_with_no_state_change(self):
        # Même structure que les deux autres statuts : aucun champ
        # distinct n'est requis pour représenter un rejet.
        result = TransitionResult(
            status=TransitionStatus.REJECTED,
            requested_value=AbsoluteAmount(amount=10.0),
            applied_value=None,
            state_before=None,
            state_after=None,
            reason="origine incompatible avec la cause déclarée",
        )
        assert result.status is TransitionStatus.REJECTED
        assert result.applied_value is None
        assert result.state_before is None
        assert result.state_after is None

    def test_applied_at_defaults_to_a_recent_timestamp(self):
        before = time.time()
        result = TransitionResult(
            status=TransitionStatus.REJECTED,
            requested_value=AbsoluteAmount(amount=1.0),
            applied_value=None,
            state_before=None,
            state_after=None,
            reason="motif quelconque",
        )
        after = time.time()
        assert before <= result.applied_at <= after

    def test_is_immutable(self):
        result = TransitionResult(
            status=TransitionStatus.REJECTED,
            requested_value=AbsoluteAmount(amount=1.0),
            applied_value=None,
            state_before=None,
            state_after=None,
            reason="motif quelconque",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = TransitionStatus.ACCEPTED


# ============================================================
# ENTRÉE DE JOURNAL
# ============================================================

class TestCapitalTransitionJournalEntry:
    """CapitalTransitionJournalEntry : forme logique de l'entrée de journal (RN-023 §10)."""

    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(CapitalTransitionJournalEntry)

    def test_is_frozen(self):
        assert CapitalTransitionJournalEntry.__dataclass_params__.frozen is True

    def test_has_expected_fields(self):
        field_names = {
            f.name for f in dataclasses.fields(CapitalTransitionJournalEntry)
        }
        assert field_names == {
            "bot_id",
            "cause",
            "origin",
            "status",
            "requested_value",
            "applied_value",
            "state_before",
            "state_after",
            "reason",
            "requested_at",
            "applied_at",
        }

    def test_can_be_constructed_for_an_accepted_transition(self):
        entry = CapitalTransitionJournalEntry(
            bot_id="gateio_rsrusdt",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            status=TransitionStatus.ACCEPTED,
            requested_value=AbsoluteAmount(amount=10.0),
            applied_value=AppliedDelta(amount=10.0),
            state_before=EconomicState(allocated_capital=220.0),
            state_after=EconomicState(allocated_capital=230.0),
            reason=None,
            requested_at=1000.0,
            applied_at=1000.5,
        )
        assert entry.bot_id == "gateio_rsrusdt"
        assert entry.applied_at == 1000.5

    def test_can_be_constructed_for_a_rejected_transition(self):
        entry = CapitalTransitionJournalEntry(
            bot_id="gateio_rsrusdt",
            cause=TransitionCause.MANUAL_SYNC,
            origin=TransitionOrigin.OPERATOR,
            status=TransitionStatus.REJECTED,
            requested_value=AbsoluteAmount(amount=100.0),
            applied_value=None,
            state_before=None,
            state_after=None,
            reason="justification manquante",
            requested_at=1000.0,
            applied_at=1000.1,
        )
        assert entry.status is TransitionStatus.REJECTED
        assert entry.reason == "justification manquante"

    def test_is_immutable(self):
        entry = CapitalTransitionJournalEntry(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_LOSS,
            origin=TransitionOrigin.BOT,
            status=TransitionStatus.ACCEPTED,
            requested_value=AbsoluteAmount(amount=-5.0),
            applied_value=AppliedDelta(amount=-5.0),
            state_before=EconomicState(allocated_capital=220.0),
            state_after=EconomicState(allocated_capital=215.0),
            reason=None,
            requested_at=1000.0,
            applied_at=1000.2,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.reason = "modifié après coup"


# ============================================================
# SQUELETTE DU CAPITALTRANSITIONGUARD
# ============================================================

class TestCapitalTransitionGuardSkeleton:
    """
    Vérifie que le squelette du Guard expose exactement l'API attendue
    et qu'aucune méthode ne contient de logique à l'étape 1.
    """

    def test_exposes_submit_transition_method(self):
        assert hasattr(CapitalTransitionGuard, "submit_transition")

    def test_exposes_get_current_state_method(self):
        assert hasattr(CapitalTransitionGuard, "get_current_state")

    def test_exposes_get_history_method(self):
        assert hasattr(CapitalTransitionGuard, "get_history")

    def test_submit_transition_raises_not_implemented_error(self):
        guard = CapitalTransitionGuard()
        request = TransitionRequest(
            bot_id="bot_1",
            cause=TransitionCause.REALIZED_PROFIT,
            origin=TransitionOrigin.BOT,
            value=AbsoluteAmount(amount=1.0),
        )
        with pytest.raises(NotImplementedError):
            guard.submit_transition(request)

    def test_get_current_state_raises_not_implemented_error(self):
        guard = CapitalTransitionGuard()
        with pytest.raises(NotImplementedError):
            guard.get_current_state("bot_1")

    def test_get_history_raises_not_implemented_error(self):
        guard = CapitalTransitionGuard()
        with pytest.raises(NotImplementedError):
            guard.get_history("bot_1")

    def test_public_api_is_limited_to_the_three_specified_methods(self):
        # Garde-fou structurel : aucune méthode publique
        # supplémentaire ne doit apparaître à l'étape 1 (ex: pas de
        # méthode "set_target", "force_value", ou tout équivalent qui
        # romprait l'unicité du point d'entrée défini par RN-023 §3).
        public_methods = {
            name
            for name, member in inspect.getmembers(
                CapitalTransitionGuard, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }
        assert public_methods == {
            "submit_transition",
            "get_current_state",
            "get_history",
        }

    def test_guard_instance_has_no_persistent_instance_state_after_construction(self):
        # Le Guard doit être stateless entre deux cycles (RN-023,
        # garantie G4). À l'étape 1, une instance fraîchement construite
        # ne doit porter aucun attribut d'instance (pas de mémoire de
        # convergence, pas de cache implicite).
        guard = CapitalTransitionGuard()
        assert vars(guard) == {}


# ============================================================
# GARDE-FOU GLOBAL : AUCUNE LOGIQUE MÉTIER DANS CE MODULE
# ============================================================

class TestNoBusinessLogicAtThisStage:
    """
    Vérifie que les types de domaine restent de purs conteneurs de
    données : aucune méthode de calcul, de validation ou de résolution
    ne doit être présente à l'étape 1.
    """

    @pytest.mark.parametrize(
        "domain_type",
        [
            AbsoluteAmount,
            RelativeCorrection,
            AppliedDelta,
            EconomicState,
            TransitionRequest,
            TransitionResult,
            CapitalTransitionJournalEntry,
        ],
    )
    def test_domain_type_exposes_no_public_method(self, domain_type):
        public_methods = [
            name
            for name, member in inspect.getmembers(domain_type)
            if not name.startswith("_") and inspect.isfunction(member)
        ]
        assert public_methods == []
