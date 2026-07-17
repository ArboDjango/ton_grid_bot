"""
tests/test_meta_capital_sync.py

Tests couvrant exclusivement l'étape 4 du plan de reconstruction :
la migration des corrections de capital du MetaController vers le
CapitalTransitionGuard (TransitionType META_CORRECTION), via
meta_capital_sync.py et BotManager.apply_transaction().

Portée strictement respectée :
  - Aucun test ne réévalue validate_transition_request,
    resolve_transition_value, apply_delta, CapitalTransitionJournal en
    tant que tels : déjà testés ailleurs.
  - Aucun test ne porte sur les achats, les ventes, les calculs FIFO
    ou le PnL réalisé.
  - Ces tests vérifient :
      * build_meta_correction_request convertit correctement une
        cible absolue en RelativeCorrection ;
      * BotStateFileEconomicRepository lit/écrit allocated_capital
        directement dans le fichier d'état réel du bot, sans toucher
        aux autres champs ;
      * BotManager.apply_transaction() soumet bien une transition
        META_CORRECTION au Guard, persiste le nouvel état, produit une
        entrée de journal, et conserve la forme de son dictionnaire de
        retour (compatibilité avec transfer_engine.py) ;
      * l'absence de régression sur les autres champs du fichier
        d'état (inventaire, grille, PnL, wallet_peak).
"""

import json

import pytest

from capital_transition_guard import (
    CapitalTransitionGuard,
    CapitalTransitionJournal,
    EconomicState,
    RelativeCorrection,
    TransitionCause,
    TransitionOrigin,
    TransitionStatus,
)
from meta_capital_sync import BotStateFileEconomicRepository, build_meta_correction_request
from bot_manager import BotManager


# ============================================================
# BUILD_META_CORRECTION_REQUEST
# ============================================================

class TestBuildMetaCorrectionRequest:
    def test_produces_a_meta_correction_transition_request(self):
        request = build_meta_correction_request(
            bot_id="INJUSDC",
            current_allocated=200.0,
            new_budget=220.0,
            justification="test",
        )

        assert request.bot_id == "INJUSDC"
        assert request.cause is TransitionCause.META_CORRECTION
        assert request.origin is TransitionOrigin.META_CONTROLLER

    def test_value_is_a_relative_correction_never_an_absolute_amount(self):
        request = build_meta_correction_request(
            bot_id="bot_1", current_allocated=200.0, new_budget=220.0, justification="x"
        )

        assert isinstance(request.value, RelativeCorrection)

    def test_fraction_reproduces_the_absolute_target_once_resolved(self):
        # 220 = 200 * (1 + fraction) => fraction = 0.10
        request = build_meta_correction_request(
            bot_id="bot_1", current_allocated=200.0, new_budget=220.0, justification="x"
        )

        assert request.value.fraction == pytest.approx(0.10)

    def test_fraction_can_be_negative_for_a_downward_correction(self):
        request = build_meta_correction_request(
            bot_id="bot_1", current_allocated=200.0, new_budget=150.0, justification="x"
        )

        assert request.value.fraction == pytest.approx(-0.25)

    def test_raises_value_error_when_current_allocated_is_zero(self):
        with pytest.raises(ValueError):
            build_meta_correction_request(
                bot_id="bot_1", current_allocated=0.0, new_budget=100.0, justification="x"
            )

    def test_raises_value_error_when_current_allocated_is_negative(self):
        with pytest.raises(ValueError):
            build_meta_correction_request(
                bot_id="bot_1", current_allocated=-10.0, new_budget=100.0, justification="x"
            )

    def test_justification_is_carried_over(self):
        request = build_meta_correction_request(
            bot_id="bot_1",
            current_allocated=100.0,
            new_budget=110.0,
            justification="motif specifique",
        )
        assert request.justification == "motif specifique"


# ============================================================
# BOTSTATEFILEECONOMICREPOSITORY
# ============================================================

class TestBotStateFileEconomicRepository:
    def _write_fixture_state(self, path, allocated_capital=220.0, extra=None):
        data = {"allocated_capital": allocated_capital, "symbol": "INJUSDC"}
        if extra:
            data.update(extra)
        path.write_text(json.dumps(data))

    def test_load_reads_allocated_capital_from_the_real_state_file(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        self._write_fixture_state(state_file, allocated_capital=220.0)
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        result = repository.load("INJUSDC")

        assert result == EconomicState(allocated_capital=220.0)

    def test_load_raises_key_error_for_a_different_bot_id(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        self._write_fixture_state(state_file)
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        with pytest.raises(KeyError):
            repository.load("EGLDUSDC")

    def test_load_raises_os_error_when_file_is_missing(self, tmp_path):
        state_file = tmp_path / "does_not_exist.json"
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        with pytest.raises(OSError):
            repository.load("INJUSDC")

    def test_load_raises_key_error_when_allocated_capital_absent(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        state_file.write_text(json.dumps({"symbol": "INJUSDC"}))
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        with pytest.raises(KeyError):
            repository.load("INJUSDC")

    def test_save_writes_allocated_capital_into_the_real_state_file(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        self._write_fixture_state(state_file, allocated_capital=200.0)
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        repository.save("INJUSDC", EconomicState(allocated_capital=220.0))

        result = repository.load("INJUSDC")
        assert result == EconomicState(allocated_capital=220.0)

    def test_save_does_not_touch_other_fields_of_the_state_file(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        self._write_fixture_state(
            state_file,
            allocated_capital=200.0,
            extra={
                "sell_grid": [1.1, 1.2],
                "buy_grid": [0.9, 0.8],
                "total_pnl": 12.34,
                "wallet_peak": 555.0,
                "inventory_lots": [{"qty": 10.0, "buy_price": 1.0}],
            },
        )
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        repository.save("INJUSDC", EconomicState(allocated_capital=220.0))

        raw = json.loads(state_file.read_text())
        assert raw["sell_grid"] == [1.1, 1.2]
        assert raw["buy_grid"] == [0.9, 0.8]
        assert raw["total_pnl"] == 12.34
        assert raw["wallet_peak"] == 555.0
        assert raw["inventory_lots"] == [{"qty": 10.0, "buy_price": 1.0}]
        assert raw["allocated_capital"] == 220.0

    def test_save_raises_key_error_for_a_different_bot_id(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        self._write_fixture_state(state_file)
        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")

        with pytest.raises(KeyError):
            repository.save("EGLDUSDC", EconomicState(allocated_capital=100.0))


# ============================================================
# INTEGRATION BOUT-EN-BOUT (Guard + adaptateur), reproduisant
# le scenario META_CORRECTION complet
# ============================================================

class TestMetaCorrectionEndToEnd:
    def test_correction_updates_allocated_capital_to_the_target(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        state_file.write_text(json.dumps({"allocated_capital": 200.0}))

        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        current = guard.get_current_state("INJUSDC")
        request = build_meta_correction_request(
            bot_id="INJUSDC",
            current_allocated=current.allocated_capital,
            new_budget=230.0,
            justification="correction MetaController",
        )
        result = guard.submit_transition(request)

        assert result.status is TransitionStatus.ACCEPTED
        assert result.state_after.allocated_capital == pytest.approx(230.0)

        raw = json.loads(state_file.read_text())
        assert raw["allocated_capital"] == pytest.approx(230.0)

    def test_correction_creates_exactly_one_journal_entry(self, tmp_path):
        state_file = tmp_path / "state_gateio_injusdc.json"
        state_file.write_text(json.dumps({"allocated_capital": 200.0}))

        repository = BotStateFileEconomicRepository(state_file, "INJUSDC")
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)

        request = build_meta_correction_request(
            bot_id="INJUSDC", current_allocated=200.0, new_budget=180.0, justification="x"
        )
        guard.submit_transition(request)

        history = journal.history_for("INJUSDC")
        assert len(history) == 1
        entry = history[0]
        assert entry.cause is TransitionCause.META_CORRECTION
        assert entry.origin is TransitionOrigin.META_CONTROLLER
        assert entry.status is TransitionStatus.ACCEPTED
        assert entry.state_before == EconomicState(allocated_capital=200.0)
        assert entry.state_after.allocated_capital == pytest.approx(180.0)


# ============================================================
# BOTMANAGER.APPLY_TRANSACTION — INTEGRATION REELLE
# ============================================================

class TestBotManagerApplyTransaction:
    def _make_manager_with_bot(self, tmp_path, symbol="INJUSDC", allocated_capital=200.0, extra=None):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        lock_dir = tmp_path / "lock"
        lock_dir.mkdir()

        data = {"allocated_capital": allocated_capital}
        if extra:
            data.update(extra)
        state_file = state_dir / f"state_gateio_{symbol.lower()}.json"
        state_file.write_text(json.dumps(data))

        manager = BotManager(exchange="gateio", state_dir=str(state_dir), lock_dir=str(lock_dir))
        return manager, state_file

    def test_apply_transaction_returns_success_true_on_acceptance(self, tmp_path):
        manager, state_file = self._make_manager_with_bot(tmp_path, allocated_capital=200.0)

        result = manager.apply_transaction("INJUSDC", 220.0)

        assert result["success"] is True
        assert result["symbol"] == "INJUSDC"
        assert result["old_budget"] == pytest.approx(200.0)
        assert result["new_budget"] == pytest.approx(220.0)

    def test_apply_transaction_persists_the_new_allocated_capital(self, tmp_path):
        manager, state_file = self._make_manager_with_bot(tmp_path, allocated_capital=200.0)

        manager.apply_transaction("INJUSDC", 220.0)

        raw = json.loads(state_file.read_text())
        assert raw["allocated_capital"] == pytest.approx(220.0)

    def test_apply_transaction_does_not_write_a_control_file_anymore(self, tmp_path):
        manager, state_file = self._make_manager_with_bot(tmp_path, allocated_capital=200.0)

        manager.apply_transaction("INJUSDC", 220.0)

        control_file = state_file.parent / "control_injusdc.json"
        assert not control_file.exists()

    def test_apply_transaction_does_not_touch_other_state_fields(self, tmp_path):
        manager, state_file = self._make_manager_with_bot(
            tmp_path,
            allocated_capital=200.0,
            extra={
                "sell_grid": [1.1, 1.2],
                "total_pnl": 42.0,
                "wallet_peak": 999.0,
            },
        )

        manager.apply_transaction("INJUSDC", 250.0)

        raw = json.loads(state_file.read_text())
        assert raw["sell_grid"] == [1.1, 1.2]
        assert raw["total_pnl"] == 42.0
        assert raw["wallet_peak"] == 999.0

    def test_apply_transaction_for_unknown_bot_returns_success_false(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        lock_dir = tmp_path / "lock"
        lock_dir.mkdir()
        manager = BotManager(exchange="gateio", state_dir=str(state_dir), lock_dir=str(lock_dir))

        result = manager.apply_transaction("UNKNOWNUSDC", 100.0)

        assert result["success"] is False
        assert result["error"] is not None

    def test_apply_transaction_with_zero_current_capital_returns_success_false(self, tmp_path):
        manager, state_file = self._make_manager_with_bot(tmp_path, allocated_capital=0.0)

        result = manager.apply_transaction("INJUSDC", 100.0)

        assert result["success"] is False
        # L'etat n'a pas ete modifie par cet echec.
        raw = json.loads(state_file.read_text())
        assert raw["allocated_capital"] == 0.0

    def test_apply_transaction_result_shape_is_compatible_with_transfer_engine(self, tmp_path):
        # transfer_engine.py lit result["success"], result["old_budget"]
        # et result["new_budget"] : la forme du dictionnaire retourne
        # doit rester stable.
        manager, state_file = self._make_manager_with_bot(tmp_path, allocated_capital=200.0)

        result = manager.apply_transaction("INJUSDC", 220.0)

        assert set(["symbol", "success", "new_budget", "old_budget", "steps"]).issubset(result.keys())
