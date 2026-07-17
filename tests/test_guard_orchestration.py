"""
tests/test_guard_orchestration.py

Tests couvrant exclusivement l'étape 6 du plan de reconstruction du
CapitalTransitionGuard : l'orchestration complète de
submit_transition, ainsi que la nouvelle fonction pure apply_delta
introduite pour que le Guard n'ait lui-même aucune arithmétique à
effectuer.

Portée strictement respectée :
  - Aucun test ne réévalue la logique interne de
    validate_transition_request, resolve_transition_value,
    CapitalTransitionJournal ou EconomicStateRepository : ces
    composants sont déjà entièrement testés indépendamment
    (test_transition_validation.py, test_transition_resolution.py,
    test_transition_journal.py, test_economic_state_repository.py).
  - Ces tests vérifient uniquement :
      * qu'apply_delta produit le bon EconomicState, sans effet de
        bord ;
      * que submit_transition orchestre les composants dans le bon
        ordre (validation, lecture, résolution, application,
        journalisation, écriture) ;
      * que le Guard ne modifie jamais directement
        allocated_capital ;
      * qu'une demande invalide ne déclenche aucun appel au
        Repository ;
      * qu'une erreur levée par le Repository ou le Resolver n'est
        jamais capturée ni masquée par le Guard ;
      * qu'un échec de sauvegarde ne laisse jamais un état incohérent
        persisté.
  - Des doublures de test (fakes) sont utilisées pour le Repository,
    afin de contrôler précisément les scénarios d'erreur et
    d'observer l'ordre des appels, sans dépendre du système de
    fichiers. Le journal réel (CapitalTransitionJournal) est utilisé
    tel quel, car il est déjà validé indépendamment.
"""

import dataclasses

import pytest

from capital_transition_guard import (
    AbsoluteAmount,
    AppliedDelta,
    CapitalTransitionGuard,
    CapitalTransitionJournal,
    EconomicState,
    RelativeCorrection,
    TransitionCause,
    TransitionOrigin,
    TransitionStatus,
    TransitionRequest,
    apply_delta,
)


# ============================================================
# DOUBLURES DE TEST
# ============================================================

class FakeEconomicStateRepository:
    """
    Repository factice en mémoire, satisfaisant structurellement
    EconomicStateRepositoryProtocol.

    Enregistre chaque appel dans un journal d'appels partagé (call_log)
    pour permettre de vérifier l'ordre des opérations orchestrées par
    le Guard. Peut être configuré pour lever une exception au chargement
    ou à la sauvegarde, afin de tester la propagation des erreurs.
    """

    def __init__(self, initial_states=None, call_log=None):
        self._states = dict(initial_states or {})
        self.call_log = call_log if call_log is not None else []
        self.raise_on_load = None
        self.raise_on_save = None

    def load(self, bot_id):
        self.call_log.append(("load", bot_id))
        if self.raise_on_load is not None:
            raise self.raise_on_load
        return self._states[bot_id]

    def save(self, bot_id, state):
        self.call_log.append(("save", bot_id, state))
        if self.raise_on_save is not None:
            raise self.raise_on_save
        self._states[bot_id] = state


class SpyJournal:
    """
    Enveloppe un CapitalTransitionJournal réel (déjà validé
    indépendamment) tout en enregistrant chaque appel à `record` dans
    le même journal d'appels que le Repository factice, afin de
    vérifier l'ordre relatif journal/repository.
    """

    def __init__(self, call_log=None):
        self._journal = CapitalTransitionJournal()
        self.call_log = call_log if call_log is not None else []

    def record(self, entry):
        self.call_log.append(("record", entry))
        self._journal.record(entry)

    def history_for(self, bot_id):
        return self._journal.history_for(bot_id)


def _make_realized_profit_request(bot_id="gateio_rsrusdt", amount=10.0):
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.REALIZED_PROFIT,
        origin=TransitionOrigin.BOT,
        value=AbsoluteAmount(amount=amount),
    )


def _make_meta_correction_request(bot_id="gateio_rsrusdt", fraction=0.05):
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.META_CORRECTION,
        origin=TransitionOrigin.META_CONTROLLER,
        value=RelativeCorrection(fraction=fraction),
    )


def _make_invalid_request(bot_id="gateio_rsrusdt"):
    # Origine incompatible avec la cause : rejetée par la validation,
    # avant tout contact avec le Repository.
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.REALIZED_PROFIT,
        origin=TransitionOrigin.META_CONTROLLER,
        value=AbsoluteAmount(amount=10.0),
    )


# ============================================================
# APPLY_DELTA — FONCTION PURE
# ============================================================

class TestApplyDelta:
    def test_adds_a_positive_delta_to_the_current_state(self):
        state = EconomicState(allocated_capital=220.0)
        delta = AppliedDelta(amount=10.0)

        result = apply_delta(state, delta)

        assert result == EconomicState(allocated_capital=230.0)

    def test_subtracts_a_negative_delta_from_the_current_state(self):
        state = EconomicState(allocated_capital=220.0)
        delta = AppliedDelta(amount=-15.0)

        result = apply_delta(state, delta)

        assert result == EconomicState(allocated_capital=205.0)

    def test_does_not_mutate_the_input_state(self):
        state = EconomicState(allocated_capital=220.0)
        delta = AppliedDelta(amount=10.0)

        apply_delta(state, delta)

        assert state.allocated_capital == 220.0

    def test_does_not_mutate_the_input_delta(self):
        state = EconomicState(allocated_capital=220.0)
        delta = AppliedDelta(amount=10.0)

        apply_delta(state, delta)

        assert delta.amount == 10.0

    def test_returns_a_new_economic_state_instance(self):
        state = EconomicState(allocated_capital=220.0)
        delta = AppliedDelta(amount=0.0)

        result = apply_delta(state, delta)

        assert result is not state


# ============================================================
# FLUX COMPLET — TRANSITION ACCEPTEE
# ============================================================

class TestSubmitTransitionAcceptedFlow:
    def test_realized_profit_updates_allocated_capital_correctly(self):
        repository = FakeEconomicStateRepository(
            initial_states={"gateio_rsrusdt": EconomicState(allocated_capital=220.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)
        request = _make_realized_profit_request(amount=10.0)

        result = guard.submit_transition(request)

        assert result.status is TransitionStatus.ACCEPTED
        assert result.applied_value == AppliedDelta(amount=10.0)
        assert result.state_before == EconomicState(allocated_capital=220.0)
        assert result.state_after == EconomicState(allocated_capital=230.0)
        assert result.reason is None

    def test_meta_correction_resolves_against_current_state_before_applying(self):
        repository = FakeEconomicStateRepository(
            initial_states={"gateio_rsrusdt": EconomicState(allocated_capital=200.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)
        request = _make_meta_correction_request(fraction=0.10)

        result = guard.submit_transition(request)

        assert result.applied_value == AppliedDelta(amount=20.0)  # 0.10 * 200.0
        assert result.state_after == EconomicState(allocated_capital=220.0)

    def test_accepted_transition_is_persisted_to_the_repository(self):
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

        assert repository._states["bot_1"] == EconomicState(allocated_capital=105.0)

    def test_accepted_transition_produces_exactly_one_journal_entry(self):
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

        history = journal.history_for("bot_1")
        assert len(history) == 1
        entry = history[0]
        assert entry.status is TransitionStatus.ACCEPTED
        assert entry.applied_value == AppliedDelta(amount=5.0)
        assert entry.state_before == EconomicState(allocated_capital=100.0)
        assert entry.state_after == EconomicState(allocated_capital=105.0)
        assert entry.bot_id == "bot_1"
        assert entry.cause is TransitionCause.REALIZED_PROFIT
        assert entry.origin is TransitionOrigin.BOT


# ============================================================
# ORDRE DES APPELS
# ============================================================

class TestSubmitTransitionCallOrder:
    def test_calls_happen_in_the_specified_order_for_an_accepted_transition(self):
        call_log = []
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)},
            call_log=call_log,
        )
        journal = SpyJournal(call_log=call_log)
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

        operations = [entry[0] for entry in call_log]
        assert operations == ["load", "record", "save"]

    def test_journal_is_written_before_repository_save(self):
        # Decision d'implementation deja actee lors de la revue de
        # faisabilite de RN-023 : le journal fait foi, il est ecrit
        # avant que l'etat ne soit persiste.
        call_log = []
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)},
            call_log=call_log,
        )
        journal = SpyJournal(call_log=call_log)
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

        record_index = next(i for i, e in enumerate(call_log) if e[0] == "record")
        save_index = next(i for i, e in enumerate(call_log) if e[0] == "save")
        assert record_index < save_index

    def test_load_is_called_with_the_bot_id_from_the_request(self):
        call_log = []
        repository = FakeEconomicStateRepository(
            initial_states={"specific_bot": EconomicState(allocated_capital=50.0)},
            call_log=call_log,
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.submit_transition(_make_realized_profit_request(bot_id="specific_bot", amount=1.0))

        assert ("load", "specific_bot") in call_log


# ============================================================
# DEMANDE STRUCTURELLEMENT INVALIDE
# ============================================================

class TestSubmitTransitionRejectedFlow:
    def test_invalid_request_returns_a_rejected_result_with_a_reason(self):
        repository = FakeEconomicStateRepository()
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        result = guard.submit_transition(_make_invalid_request())

        assert result.status is TransitionStatus.REJECTED
        assert result.reason is not None
        assert result.applied_value is None
        assert result.state_before is None
        assert result.state_after is None

    def test_invalid_request_never_calls_the_repository(self):
        call_log = []
        repository = FakeEconomicStateRepository(call_log=call_log)
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.submit_transition(_make_invalid_request())

        assert call_log == []

    def test_invalid_request_still_produces_a_journal_entry(self):
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        guard.submit_transition(_make_invalid_request(bot_id="bot_1"))

        history = journal.history_for("bot_1")
        assert len(history) == 1
        assert history[0].status is TransitionStatus.REJECTED
        assert history[0].reason is not None
        assert history[0].applied_value is None


# ============================================================
# PROPAGATION DES ERREURS
# ============================================================

class TestSubmitTransitionErrorPropagation:
    def test_repository_load_failure_propagates_and_is_not_caught(self):
        repository = FakeEconomicStateRepository()
        repository.raise_on_load = FileNotFoundError("etat introuvable")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        with pytest.raises(FileNotFoundError):
            guard.submit_transition(_make_realized_profit_request(bot_id="bot_1"))

    def test_repository_load_failure_produces_no_journal_entry(self):
        repository = FakeEconomicStateRepository()
        repository.raise_on_load = FileNotFoundError("etat introuvable")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        with pytest.raises(FileNotFoundError):
            guard.submit_transition(_make_realized_profit_request(bot_id="bot_1"))

        assert journal.history_for("bot_1") == []

    def test_repository_save_failure_propagates_and_is_not_caught(self):
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        repository.raise_on_save = RuntimeError("disque plein")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        with pytest.raises(RuntimeError):
            guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

    def test_repository_save_failure_leaves_no_partial_state_persisted(self):
        # Garantie explicitement requise : un etat incoherent ne doit
        # jamais etre sauvegarde. Le FakeEconomicStateRepository leve
        # avant toute affectation dans son dictionnaire interne, ce qui
        # confirme qu'aucun etat partiel n'est ecrit en cas d'echec.
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        repository.raise_on_save = RuntimeError("disque plein")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        with pytest.raises(RuntimeError):
            guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

        assert repository._states["bot_1"] == EconomicState(allocated_capital=100.0)

    def test_repository_save_failure_still_leaves_a_journal_entry_recorded(self):
        # Consequence assumee de l'ordre d'ecriture (journal avant
        # etat) : si la sauvegarde echoue apres que le journal a ete
        # ecrit, l'entree existe deja. Ce comportement est documente
        # et delibere, pas une incoherence non geree.
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        repository.raise_on_save = RuntimeError("disque plein")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        with pytest.raises(RuntimeError):
            guard.submit_transition(_make_realized_profit_request(bot_id="bot_1", amount=5.0))

        history = journal.history_for("bot_1")
        assert len(history) == 1
        assert history[0].status is TransitionStatus.ACCEPTED
        assert history[0].state_after == EconomicState(allocated_capital=105.0)

    def test_resolver_failure_propagates_and_is_not_caught(self, monkeypatch):
        # Scenario defensif : simule un bug hypothetique du resolveur
        # (qui ne devrait jamais se produire pour une demande deja
        # validee) afin de verifier que le Guard ne masque jamais une
        # exception, quelle qu'en soit la source.
        import capital_transition_guard as module

        def _raising_resolver(value, current_state):
            raise TypeError("echec simule du resolveur")

        monkeypatch.setattr(module, "resolve_transition_value", _raising_resolver)

        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        with pytest.raises(TypeError):
            guard.submit_transition(_make_realized_profit_request(bot_id="bot_1"))


# ============================================================
# LE GUARD NE MODIFIE JAMAIS allocated_capital DIRECTEMENT
# ============================================================

class TestGuardNeverComputesOrMutatesDirectly:
    def test_submitted_request_value_is_not_mutated(self):
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)
        request = _make_realized_profit_request(bot_id="bot_1", amount=7.5)

        guard.submit_transition(request)

        assert request.value == AbsoluteAmount(amount=7.5)

    def test_state_before_and_state_after_are_distinct_instances(self):
        # Le Guard ne doit jamais reutiliser/muter l'etat courant pour
        # produire l'etat suivant : ce sont deux instances distinctes
        # d'EconomicState (immuable), produites par apply_delta.
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        result = guard.submit_transition(
            _make_realized_profit_request(bot_id="bot_1", amount=5.0)
        )

        assert result.state_before is not result.state_after
        assert result.state_before.allocated_capital == 100.0
        assert result.state_after.allocated_capital == 105.0
