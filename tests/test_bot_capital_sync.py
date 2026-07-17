"""
tests/test_bot_capital_sync.py

Tests couvrant la première intégration réelle du CapitalTransitionGuard
dans bot_gateio.py : le mécanisme --sync-capital, via bot_capital_sync.py
(StateDictEconomicRepository et build_manual_sync_request), ainsi que le
correctif transitoire merge_allocated_capital_from_disk (protection
contre l'écrasement silencieux d'allocated_capital par un save_state()
routinier, cf. TODO/RN à créer dans bot_gateio.py).

Portée strictement respectée :
  - Aucun test n'importe ou n'exécute bot_gateio.py lui-même (script
    exécutable au niveau module, avec appels réseau et lecture de
    sys.argv — non importable proprement dans des tests). Ces tests
    couvrent le module d'intégration bot_capital_sync.py et son usage
    du CapitalTransitionGuard déjà validé indépendamment.
  - Aucun test ne réévalue validate_transition_request,
    resolve_transition_value, apply_delta, CapitalTransitionJournal en
    tant que tels : déjà testés ailleurs.
  - Ces tests vérifient :
      * build_manual_sync_request produit une TransitionRequest
        MANUAL_SYNC correcte (bot_id, origin, delta signé, justification) ;
      * StateDictEconomicRepository lit/écrit correctement le state
        dict existant, et persiste via la fonction save_fn fournie ;
      * l'intégration bout-en-bout (Guard + adaptateur) reproduit
        exactement le comportement de l'ancien mécanisme
        (round(new_allocated, 2)) ;
      * une entrée de journal est bien créée dans le journal du Guard ;
      * l'absence de régression : le state dict n'est pas modifié en
        dehors du champ allocated_capital ;
      * merge_allocated_capital_from_disk adopte correctement la
        valeur sur disque, ne touche à rien d'autre, et gère les cas
        limites (disque illisible, champ absent) sans erreur.
"""

import pytest

from capital_transition_guard import (
    AbsoluteAmount,
    CapitalTransitionGuard,
    CapitalTransitionJournal,
    EconomicState,
    TransitionCause,
    TransitionOrigin,
    TransitionStatus,
)
from bot_capital_sync import (
    StateDictEconomicRepository,
    build_manual_sync_request,
    merge_allocated_capital_from_disk,
)


# ============================================================
# BUILD_MANUAL_SYNC_REQUEST
# ============================================================

class TestBuildManualSyncRequest:
    def test_produces_a_manual_sync_transition_request(self):
        request = build_manual_sync_request(
            bot_id="gateio_rsrusdt",
            old_allocated=220.0,
            new_allocated=235.789,
            justification="test",
        )

        assert request.bot_id == "gateio_rsrusdt"
        assert request.cause is TransitionCause.MANUAL_SYNC
        assert request.origin is TransitionOrigin.OPERATOR

    def test_delta_reproduces_rounding_to_two_decimals(self):
        # L'ancien mecanisme faisait : state["allocated_capital"] =
        # round(new_allocated, 2). Le delta doit donc etre tel que
        # old_allocated + delta == round(new_allocated, 2).
        request = build_manual_sync_request(
            bot_id="bot_1",
            old_allocated=220.0,
            new_allocated=235.789,
            justification="test",
        )

        assert isinstance(request.value, AbsoluteAmount)
        resulting_value = 220.0 + request.value.amount
        assert resulting_value == pytest.approx(round(235.789, 2))

    def test_delta_can_be_negative(self):
        request = build_manual_sync_request(
            bot_id="bot_1",
            old_allocated=300.0,
            new_allocated=250.0,
            justification="test",
        )

        assert request.value.amount == pytest.approx(-50.0)

    def test_justification_is_carried_over(self):
        request = build_manual_sync_request(
            bot_id="bot_1",
            old_allocated=100.0,
            new_allocated=110.0,
            justification="motif specifique",
        )

        assert request.justification == "motif specifique"

    def test_requires_no_prior_state_read_by_itself(self):
        # Fonction pure : ne lit ni n'ecrit rien, se contente de
        # calculer a partir des arguments fournis.
        request_a = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=110.0, justification="x"
        )
        request_b = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=110.0, justification="x"
        )
        assert request_a.value == request_b.value


# ============================================================
# STATEDICTECONOMICREPOSITORY
# ============================================================

class TestStateDictEconomicRepository:
    def test_load_reads_allocated_capital_from_the_state_dict(self):
        state = {"allocated_capital": 220.0, "other_field": "untouched"}
        repository = StateDictEconomicRepository(state, "bot_1", save_fn=lambda s: None)

        result = repository.load("bot_1")

        assert result == EconomicState(allocated_capital=220.0)

    def test_load_defaults_to_zero_when_field_absent(self):
        state = {}
        repository = StateDictEconomicRepository(state, "bot_1", save_fn=lambda s: None)

        result = repository.load("bot_1")

        assert result == EconomicState(allocated_capital=0.0)

    def test_load_raises_key_error_for_a_different_bot_id(self):
        state = {"allocated_capital": 100.0}
        repository = StateDictEconomicRepository(state, "bot_1", save_fn=lambda s: None)

        with pytest.raises(KeyError):
            repository.load("bot_2")

    def test_save_writes_allocated_capital_into_the_state_dict(self):
        state = {"allocated_capital": 100.0, "other_field": "untouched"}
        repository = StateDictEconomicRepository(state, "bot_1", save_fn=lambda s: None)

        repository.save("bot_1", EconomicState(allocated_capital=150.0))

        assert state["allocated_capital"] == 150.0

    def test_save_does_not_touch_other_fields_of_the_state_dict(self):
        state = {"allocated_capital": 100.0, "wallet_peak": 999.0, "total_pnl": 5.0}
        repository = StateDictEconomicRepository(state, "bot_1", save_fn=lambda s: None)

        repository.save("bot_1", EconomicState(allocated_capital=150.0))

        assert state["wallet_peak"] == 999.0
        assert state["total_pnl"] == 5.0

    def test_save_calls_the_provided_save_function_with_the_state_dict(self):
        calls = []
        state = {"allocated_capital": 100.0}
        repository = StateDictEconomicRepository(
            state, "bot_1", save_fn=lambda s: calls.append(dict(s))
        )

        repository.save("bot_1", EconomicState(allocated_capital=150.0))

        assert len(calls) == 1
        assert calls[0]["allocated_capital"] == 150.0

    def test_save_raises_key_error_for_a_different_bot_id(self):
        state = {"allocated_capital": 100.0}
        repository = StateDictEconomicRepository(state, "bot_1", save_fn=lambda s: None)

        with pytest.raises(KeyError):
            repository.save("bot_2", EconomicState(allocated_capital=150.0))

    def test_save_does_not_call_save_fn_when_bot_id_mismatches(self):
        calls = []
        state = {"allocated_capital": 100.0}
        repository = StateDictEconomicRepository(
            state, "bot_1", save_fn=lambda s: calls.append(s)
        )

        with pytest.raises(KeyError):
            repository.save("bot_2", EconomicState(allocated_capital=150.0))

        assert calls == []


# ============================================================
# INTEGRATION BOUT-EN-BOUT (Guard + adaptateur), reproduisant
# exactement le mecanisme --sync-capital
# ============================================================

class TestManualSyncEndToEnd:
    def _make_guard(self, state, bot_id, save_calls):
        repository = StateDictEconomicRepository(
            state, bot_id, save_fn=lambda s: save_calls.append(dict(s))
        )
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)
        return guard, journal

    def test_sync_reproduces_the_exact_behavior_of_the_old_mechanism(self):
        # Reproduit le scenario exact de bot_gateio.py :
        # new_allocated = wallet_real - total_pnl - unrealized_pnl
        # puis state["allocated_capital"] = round(new_allocated, 2)
        state = {"allocated_capital": 220.0}
        save_calls = []
        guard, journal = self._make_guard(state, "gateio_rsrusdt", save_calls)

        old_allocated = state.get("allocated_capital", 0.0)
        new_allocated = 235.789  # wallet_real - total_pnl - unrealized_pnl

        request = build_manual_sync_request(
            bot_id="gateio_rsrusdt",
            old_allocated=old_allocated,
            new_allocated=new_allocated,
            justification="--sync-capital : recalcul depuis le wallet reel",
        )
        result = guard.submit_transition(request)

        assert result.status is TransitionStatus.ACCEPTED
        assert state["allocated_capital"] == pytest.approx(round(new_allocated, 2))

    def test_sync_persists_via_the_save_function(self):
        state = {"allocated_capital": 100.0}
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=150.0, justification="x"
        )
        guard.submit_transition(request)

        assert len(save_calls) == 1
        assert save_calls[0]["allocated_capital"] == 150.0

    def test_sync_creates_exactly_one_journal_entry(self):
        state = {"allocated_capital": 100.0}
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=150.0, justification="x"
        )
        guard.submit_transition(request)

        history = journal.history_for("bot_1")
        assert len(history) == 1
        entry = history[0]
        assert entry.cause is TransitionCause.MANUAL_SYNC
        assert entry.origin is TransitionOrigin.OPERATOR
        assert entry.status is TransitionStatus.ACCEPTED
        assert entry.state_before == EconomicState(allocated_capital=100.0)
        assert entry.state_after == EconomicState(allocated_capital=150.0)

    def test_sync_is_retrievable_via_guard_get_history(self):
        state = {"allocated_capital": 100.0}
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=120.0, justification="x"
        )
        guard.submit_transition(request)

        history = guard.get_history("bot_1")
        assert len(history) == 1
        assert history[0].cause is TransitionCause.MANUAL_SYNC

    def test_sync_is_retrievable_via_guard_get_current_state(self):
        state = {"allocated_capital": 100.0}
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=175.5, justification="x"
        )
        guard.submit_transition(request)

        current = guard.get_current_state("bot_1")
        assert current == EconomicState(allocated_capital=175.5)

    def test_sync_without_justification_is_rejected_and_does_not_touch_state(self):
        # Garde-fou : MANUAL_SYNC exige une justification (RN-022).
        # Verifie qu'un oubli ne modifierait jamais le state.
        state = {"allocated_capital": 100.0}
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=150.0, justification=""
        )
        result = guard.submit_transition(request)

        assert result.status is TransitionStatus.REJECTED
        assert state["allocated_capital"] == 100.0
        assert save_calls == []

    def test_other_state_fields_remain_untouched_after_sync(self):
        # Absence de regression : seuls allocated_capital et rien
        # d'autre ne doivent changer via ce mecanisme.
        state = {
            "allocated_capital": 100.0,
            "wallet_peak": 500.0,
            "total_pnl": 12.34,
            "sell_grid": [1, 2, 3],
            "buy_grid": [4, 5, 6],
        }
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=130.0, justification="x"
        )
        guard.submit_transition(request)

        assert state["wallet_peak"] == 500.0
        assert state["total_pnl"] == 12.34
        assert state["sell_grid"] == [1, 2, 3]
        assert state["buy_grid"] == [4, 5, 6]

    def test_repeated_sync_calls_accumulate_journal_history_in_order(self):
        state = {"allocated_capital": 100.0}
        save_calls = []
        guard, journal = self._make_guard(state, "bot_1", save_calls)

        request_1 = build_manual_sync_request(
            bot_id="bot_1", old_allocated=100.0, new_allocated=120.0, justification="x"
        )
        guard.submit_transition(request_1)

        request_2 = build_manual_sync_request(
            bot_id="bot_1",
            old_allocated=state["allocated_capital"],
            new_allocated=90.0,
            justification="x",
        )
        guard.submit_transition(request_2)

        history = journal.history_for("bot_1")
        assert len(history) == 2
        assert history[0].state_after == EconomicState(allocated_capital=120.0)
        assert history[1].state_after == EconomicState(allocated_capital=90.0)
        assert state["allocated_capital"] == 90.0


# ============================================================
# MERGE_ALLOCATED_CAPITAL_FROM_DISK (correctif transitoire)
# ============================================================

class TestMergeAllocatedCapitalFromDisk:
    def test_adopts_the_on_disk_value_when_present(self):
        # Scenario reproduisant exactement l'incident observe en
        # production : le state en memoire est obsolete (160.09),
        # une correction externe (META_CORRECTION) a deja ecrit
        # 191.86 sur disque entre-temps.
        state = {"allocated_capital": 160.09}
        on_disk_state = {"allocated_capital": 191.86}

        merge_allocated_capital_from_disk(state, on_disk_state)

        assert state["allocated_capital"] == 191.86

    def test_does_nothing_when_on_disk_state_is_none(self):
        # Cas du tout premier save (fichier pas encore cree) : ne doit
        # pas lever d'exception ni modifier state.
        state = {"allocated_capital": 220.0}

        merge_allocated_capital_from_disk(state, None)

        assert state["allocated_capital"] == 220.0

    def test_does_nothing_when_allocated_capital_absent_from_disk(self):
        state = {"allocated_capital": 220.0}
        on_disk_state = {"other_field": "value"}

        merge_allocated_capital_from_disk(state, on_disk_state)

        assert state["allocated_capital"] == 220.0

    def test_does_not_touch_other_fields_of_state(self):
        state = {
            "allocated_capital": 160.09,
            "wallet_peak": 500.0,
            "total_pnl": 12.0,
            "sell_grid": [1, 2, 3],
        }
        on_disk_state = {
            "allocated_capital": 191.86,
            "wallet_peak": 999.0,  # ne doit jamais etre adopte par cette fonction
            "total_pnl": 42.0,      # idem
        }

        merge_allocated_capital_from_disk(state, on_disk_state)

        assert state["allocated_capital"] == 191.86
        assert state["wallet_peak"] == 500.0
        assert state["total_pnl"] == 12.0
        assert state["sell_grid"] == [1, 2, 3]

    def test_adopts_on_disk_value_even_if_it_is_lower(self):
        # La fonction ne juge pas si la nouvelle valeur est plus
        # grande ou plus petite : le disque fait autorite, dans les
        # deux sens.
        state = {"allocated_capital": 300.0}
        on_disk_state = {"allocated_capital": 250.0}

        merge_allocated_capital_from_disk(state, on_disk_state)

        assert state["allocated_capital"] == 250.0

    def test_is_a_no_op_when_values_already_match(self):
        state = {"allocated_capital": 220.0}
        on_disk_state = {"allocated_capital": 220.0}

        merge_allocated_capital_from_disk(state, on_disk_state)

        assert state["allocated_capital"] == 220.0

    def test_does_not_return_a_value_mutates_in_place(self):
        state = {"allocated_capital": 100.0}
        on_disk_state = {"allocated_capital": 150.0}

        result = merge_allocated_capital_from_disk(state, on_disk_state)

        assert result is None
        assert state["allocated_capital"] == 150.0
