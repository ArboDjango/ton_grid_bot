"""
tests/test_guard_public_accessors.py

Tests couvrant exclusivement la finalisation de l'API publique du
CapitalTransitionGuard : get_current_state() et get_history().

Portée strictement respectée :
  - Aucun test ne réévalue submit_transition, validate_transition_request,
    resolve_transition_value, apply_delta, CapitalTransitionJournal ou
    EconomicStateRepository en tant que tels : ces composants sont déjà
    entièrement testés indépendamment.
  - Ces tests vérifient uniquement que get_current_state et get_history
    sont de pures délégations :
      * get_current_state(bot_id) retourne exactement ce que
        Repository.load(bot_id) retourne, sans transformation ;
      * get_history(bot_id) retourne exactement ce que
        Journal.history_for(bot_id) retourne, sans transformation ;
      * aucune des deux méthodes n'écrit quoi que ce soit
        (ni sur le Repository, ni sur le Journal) ;
      * aucune des deux méthodes ne construit de TransitionRequest,
        TransitionResult ou CapitalTransitionJournalEntry ;
      * aucune des deux méthodes n'exécute de validation, de
        résolution, ou de calcul économique.
"""

import pytest

from capital_transition_guard import (
    CapitalTransitionGuard,
    CapitalTransitionJournal,
    CapitalTransitionJournalEntry,
    EconomicState,
    TransitionCause,
    TransitionOrigin,
    TransitionStatus,
)


class FakeEconomicStateRepository:
    """
    Repository factice en mémoire, enregistrant chaque appel dans un
    journal d'appels partagé pour vérifier qu'aucune écriture n'a lieu
    et que la lecture est bien déléguée telle quelle.
    """

    def __init__(self, initial_states=None, call_log=None):
        self._states = dict(initial_states or {})
        self.call_log = call_log if call_log is not None else []

    def load(self, bot_id):
        self.call_log.append(("load", bot_id))
        return self._states[bot_id]

    def save(self, bot_id, state):
        self.call_log.append(("save", bot_id, state))
        self._states[bot_id] = state


def _make_journal_entry(bot_id="bot_1"):
    return CapitalTransitionJournalEntry(
        bot_id=bot_id,
        cause=TransitionCause.REALIZED_PROFIT,
        origin=TransitionOrigin.BOT,
        status=TransitionStatus.ACCEPTED,
        requested_value=None,
        applied_value=None,
        state_before=None,
        state_after=None,
        reason=None,
        requested_at=1000.0,
        applied_at=1000.1,
    )


# ============================================================
# GET_CURRENT_STATE
# ============================================================

class TestGetCurrentState:
    def test_returns_exactly_what_the_repository_returns(self):
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=220.0)}
        )
        guard = CapitalTransitionGuard(
            repository=repository, journal=CapitalTransitionJournal()
        )

        result = guard.get_current_state("bot_1")

        assert result == EconomicState(allocated_capital=220.0)

    def test_delegates_with_the_exact_bot_id_argument(self):
        call_log = []
        repository = FakeEconomicStateRepository(
            initial_states={"specific_bot": EconomicState(allocated_capital=1.0)},
            call_log=call_log,
        )
        guard = CapitalTransitionGuard(
            repository=repository, journal=CapitalTransitionJournal()
        )

        guard.get_current_state("specific_bot")

        assert call_log == [("load", "specific_bot")]

    def test_performs_no_write_to_the_repository(self):
        call_log = []
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)},
            call_log=call_log,
        )
        guard = CapitalTransitionGuard(
            repository=repository, journal=CapitalTransitionJournal()
        )

        guard.get_current_state("bot_1")

        assert ("save", "bot_1", EconomicState(allocated_capital=100.0)) not in call_log
        assert all(entry[0] != "save" for entry in call_log)

    def test_performs_no_write_to_the_journal(self):
        journal = CapitalTransitionJournal()
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=100.0)}
        )
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.get_current_state("bot_1")

        assert journal.history_for("bot_1") == []

    def test_propagates_repository_errors_without_masking_them(self):
        repository = FakeEconomicStateRepository()  # aucun etat charge
        guard = CapitalTransitionGuard(
            repository=repository, journal=CapitalTransitionJournal()
        )

        with pytest.raises(KeyError):
            guard.get_current_state("unknown_bot")

    def test_does_not_mutate_the_returned_state(self):
        original_state = EconomicState(allocated_capital=42.0)
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": original_state}
        )
        guard = CapitalTransitionGuard(
            repository=repository, journal=CapitalTransitionJournal()
        )

        guard.get_current_state("bot_1")

        assert original_state.allocated_capital == 42.0


# ============================================================
# GET_HISTORY
# ============================================================

class TestGetHistory:
    def test_returns_exactly_what_the_journal_returns(self):
        journal = CapitalTransitionJournal()
        entry = _make_journal_entry(bot_id="bot_1")
        journal.record(entry)
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        result = guard.get_history("bot_1")

        assert result == [entry]

    def test_returns_empty_list_for_a_bot_with_no_history(self):
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        result = guard.get_history("bot_without_history")

        assert result == []

    def test_preserves_chronological_order(self):
        journal = CapitalTransitionJournal()
        entry_1 = _make_journal_entry(bot_id="bot_1")
        entry_2 = _make_journal_entry(bot_id="bot_1")
        journal.record(entry_1)
        journal.record(entry_2)
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        result = guard.get_history("bot_1")

        assert result == [entry_1, entry_2]

    def test_performs_no_write_to_the_journal(self):
        journal = CapitalTransitionJournal()
        journal.record(_make_journal_entry(bot_id="bot_1"))
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        guard.get_history("bot_1")

        # Un second appel doit retourner exactement la meme chose :
        # aucune ecriture n'a pu se produire entre les deux appels.
        assert guard.get_history("bot_1") == [journal.history_for("bot_1")[0]]
        assert len(journal.history_for("bot_1")) == 1

    def test_performs_no_write_to_the_repository(self):
        call_log = []
        repository = FakeEconomicStateRepository(call_log=call_log)
        journal = CapitalTransitionJournal()
        journal.record(_make_journal_entry(bot_id="bot_1"))
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.get_history("bot_1")

        assert call_log == []

    def test_does_not_isolate_bots_incorrectly(self):
        journal = CapitalTransitionJournal()
        journal.record(_make_journal_entry(bot_id="bot_1"))
        journal.record(_make_journal_entry(bot_id="bot_2"))
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        assert len(guard.get_history("bot_1")) == 1
        assert len(guard.get_history("bot_2")) == 1

    def test_mutating_the_returned_list_does_not_affect_the_journal(self):
        # CapitalTransitionJournal.history_for retourne deja une copie
        # defensive ; ce test confirme que le Guard ne casse pas cette
        # garantie en retournant une reference partagee par un autre
        # chemin.
        journal = CapitalTransitionJournal()
        journal.record(_make_journal_entry(bot_id="bot_1"))
        guard = CapitalTransitionGuard(
            repository=FakeEconomicStateRepository(), journal=journal
        )

        result = guard.get_history("bot_1")
        result.append(_make_journal_entry(bot_id="bot_1"))

        assert len(guard.get_history("bot_1")) == 1


# ============================================================
# ABSENCE DE LOGIQUE METIER DANS LES DEUX ACCESSEURS
# ============================================================

class TestPublicAccessorsContainNoBusinessLogic:
    def test_get_current_state_does_not_create_any_transition_request(self):
        # Garde-fou : get_current_state ne doit jamais construire de
        # TransitionRequest, TransitionResult ou entree de journal.
        call_log = []
        repository = FakeEconomicStateRepository(
            initial_states={"bot_1": EconomicState(allocated_capital=50.0)},
            call_log=call_log,
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.get_current_state("bot_1")

        assert journal.history_for("bot_1") == []
        assert call_log == [("load", "bot_1")]

    def test_get_history_does_not_read_the_repository_at_all(self):
        call_log = []
        repository = FakeEconomicStateRepository(call_log=call_log)
        journal = CapitalTransitionJournal()
        journal.record(_make_journal_entry(bot_id="bot_1"))
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        guard.get_history("bot_1")

        assert call_log == []
