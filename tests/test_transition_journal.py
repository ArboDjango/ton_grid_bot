"""
tests/test_transition_journal.py

Tests couvrant exclusivement le périmètre restreint de l'étape 4 du
plan de reconstruction du CapitalTransitionGuard : la journalisation
logique en mémoire, via CapitalTransitionJournal.

Portée strictement respectée :
  - Aucun test de persistance sur disque ou support externe : ce
    composant est purement en mémoire, et ces tests ne vérifient rien
    qui suppose un fichier, une base, ou tout autre support durable.
  - Aucun test du futur Repository (hors périmètre de cette étape).
  - Aucun test de construction d'entrée : les
    CapitalTransitionJournalEntry utilisées ici sont construites
    directement par les tests, jamais par le composant testé.
  - Aucun test de lecture ou modification d'état économique : ces
    tests ne vérifient jamais qu'un allocated_capital a changé.
  - Aucun test de verrouillage inter-bot ou de concurrence (prévu pour
    l'étape de persistance complète).
  - Aucun test du CapitalTransitionGuard lui-même : submit_transition,
    get_current_state et get_history restent non implémentées et hors
    périmètre de cette étape (déjà couvert par
    test_capital_transition_guard.py, inchangé).

Chaque test vérifie uniquement le contrat de CapitalTransitionJournal :
enregistrement d'entrées déjà construites, consultation par bot, dans
l'ordre d'enregistrement, sans effet de bord sur l'état économique.
"""

import pytest

from capital_transition_guard import (
    AbsoluteAmount,
    AppliedDelta,
    CapitalTransitionJournal,
    CapitalTransitionJournalEntry,
    EconomicState,
    RelativeCorrection,
    TransitionCause,
    TransitionOrigin,
    TransitionStatus,
)


def _make_entry(
    bot_id="gateio_rsrusdt",
    cause=TransitionCause.REALIZED_PROFIT,
    origin=TransitionOrigin.BOT,
    status=TransitionStatus.ACCEPTED,
    requested_value=None,
    applied_value=None,
    state_before=None,
    state_after=None,
    reason=None,
    requested_at=1000.0,
    applied_at=1000.5,
):
    if requested_value is None:
        requested_value = AbsoluteAmount(amount=10.0)
    if applied_value is None and status is not TransitionStatus.REJECTED:
        applied_value = AppliedDelta(amount=10.0)
    if state_before is None and status is not TransitionStatus.REJECTED:
        state_before = EconomicState(allocated_capital=220.0)
    if state_after is None and status is not TransitionStatus.REJECTED:
        state_after = EconomicState(allocated_capital=230.0)
    return CapitalTransitionJournalEntry(
        bot_id=bot_id,
        cause=cause,
        origin=origin,
        status=status,
        requested_value=requested_value,
        applied_value=applied_value,
        state_before=state_before,
        state_after=state_after,
        reason=reason,
        requested_at=requested_at,
        applied_at=applied_at,
    )


# ============================================================
# ENREGISTREMENT ET CONSULTATION DE BASE
# ============================================================

class TestRecordAndHistoryFor:
    def test_history_for_unknown_bot_is_an_empty_list(self):
        journal = CapitalTransitionJournal()

        assert journal.history_for("unknown_bot") == []

    def test_recording_one_entry_makes_it_retrievable(self):
        journal = CapitalTransitionJournal()
        entry = _make_entry(bot_id="gateio_rsrusdt")

        journal.record(entry)

        assert journal.history_for("gateio_rsrusdt") == [entry]

    def test_record_returns_none(self):
        journal = CapitalTransitionJournal()
        entry = _make_entry()

        result = journal.record(entry)

        assert result is None

    def test_recording_multiple_entries_for_the_same_bot_preserves_order(self):
        journal = CapitalTransitionJournal()
        entry_1 = _make_entry(bot_id="bot_1", requested_at=1000.0, applied_at=1000.1)
        entry_2 = _make_entry(bot_id="bot_1", requested_at=1001.0, applied_at=1001.1)
        entry_3 = _make_entry(bot_id="bot_1", requested_at=1002.0, applied_at=1002.1)

        journal.record(entry_1)
        journal.record(entry_2)
        journal.record(entry_3)

        assert journal.history_for("bot_1") == [entry_1, entry_2, entry_3]

    def test_entries_for_different_bots_are_isolated(self):
        journal = CapitalTransitionJournal()
        entry_bot_1 = _make_entry(bot_id="bot_1")
        entry_bot_2 = _make_entry(bot_id="bot_2")

        journal.record(entry_bot_1)
        journal.record(entry_bot_2)

        assert journal.history_for("bot_1") == [entry_bot_1]
        assert journal.history_for("bot_2") == [entry_bot_2]

    def test_recording_for_one_bot_does_not_affect_another_bots_history(self):
        journal = CapitalTransitionJournal()
        journal.record(_make_entry(bot_id="bot_1"))

        assert journal.history_for("bot_2") == []


# ============================================================
# NEUTRALITE VIS-A-VIS DU STATUT
# ============================================================

class TestJournalIsNeutralAboutStatus:
    @pytest.mark.parametrize(
        "status",
        [
            TransitionStatus.ACCEPTED,
            TransitionStatus.TRUNCATED,
            TransitionStatus.REJECTED,
        ],
    )
    def test_records_entries_regardless_of_status(self, status):
        journal = CapitalTransitionJournal()
        entry = _make_entry(bot_id="bot_1", status=status, reason=(
            "motif" if status is not TransitionStatus.ACCEPTED else None
        ))

        journal.record(entry)

        assert journal.history_for("bot_1") == [entry]

    def test_a_rejected_entry_with_no_state_change_is_recorded_like_any_other(self):
        journal = CapitalTransitionJournal()
        entry = _make_entry(
            bot_id="bot_1",
            status=TransitionStatus.REJECTED,
            applied_value=None,
            state_before=None,
            state_after=None,
            reason="origine incompatible avec la cause declaree",
        )

        journal.record(entry)

        assert journal.history_for("bot_1") == [entry]


# ============================================================
# TYPE NON PRIS EN CHARGE
# ============================================================

class TestRecordRejectsWrongType:
    def test_raises_type_error_for_a_plain_dict(self):
        journal = CapitalTransitionJournal()

        with pytest.raises(TypeError):
            journal.record({"bot_id": "bot_1"})  # type: ignore[arg-type]

    def test_raises_type_error_for_none(self):
        journal = CapitalTransitionJournal()

        with pytest.raises(TypeError):
            journal.record(None)  # type: ignore[arg-type]

    def test_rejected_call_does_not_alter_existing_history(self):
        journal = CapitalTransitionJournal()
        entry = _make_entry(bot_id="bot_1")
        journal.record(entry)

        with pytest.raises(TypeError):
            journal.record("not an entry")  # type: ignore[arg-type]

        assert journal.history_for("bot_1") == [entry]


# ============================================================
# ABSENCE D'EFFET DE BORD SUR L'ETAT ECONOMIQUE
# ============================================================

class TestJournalHasNoEffectOnEconomicState:
    def test_recording_does_not_mutate_state_before_or_state_after(self):
        journal = CapitalTransitionJournal()
        state_before = EconomicState(allocated_capital=220.0)
        state_after = EconomicState(allocated_capital=230.0)
        entry = _make_entry(state_before=state_before, state_after=state_after)

        journal.record(entry)

        assert state_before.allocated_capital == 220.0
        assert state_after.allocated_capital == 230.0

    def test_journal_exposes_no_method_to_read_or_write_allocated_capital(self):
        # Garde-fou structurel : le journal ne doit exposer aucune
        # methode qui laisserait croire qu'il gere l'etat economique
        # lui-meme (ce role appartient exclusivement au Guard, une
        # fois les etapes de persistance completees).
        import inspect

        public_methods = {
            name
            for name, member in inspect.getmembers(
                CapitalTransitionJournal, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }
        assert public_methods == {"record", "history_for"}


# ============================================================
# DEFENSIVE COPY DE L'HISTORIQUE RETOURNE
# ============================================================

class TestHistoryForReturnsADefensiveCopy:
    def test_mutating_the_returned_list_does_not_affect_the_journal(self):
        journal = CapitalTransitionJournal()
        entry = _make_entry(bot_id="bot_1")
        journal.record(entry)

        history = journal.history_for("bot_1")
        history.append(_make_entry(bot_id="bot_1", requested_at=9999.0))
        history.clear()

        assert journal.history_for("bot_1") == [entry]

    def test_two_calls_to_history_for_return_equal_but_independent_lists(self):
        journal = CapitalTransitionJournal()
        entry = _make_entry(bot_id="bot_1")
        journal.record(entry)

        first_call = journal.history_for("bot_1")
        second_call = journal.history_for("bot_1")

        assert first_call == second_call
        assert first_call is not second_call


# ============================================================
# INDEPENDANCE ENTRE INSTANCES DE JOURNAL
# ============================================================

class TestJournalInstancesAreIndependent:
    def test_two_separate_journal_instances_do_not_share_state(self):
        journal_a = CapitalTransitionJournal()
        journal_b = CapitalTransitionJournal()

        journal_a.record(_make_entry(bot_id="bot_1"))

        assert journal_a.history_for("bot_1") != []
        assert journal_b.history_for("bot_1") == []
