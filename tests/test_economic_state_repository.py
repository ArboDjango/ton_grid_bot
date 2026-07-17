"""
tests/test_economic_state_repository.py

Tests couvrant exclusivement l'étape 5 du plan de reconstruction du
CapitalTransitionGuard : EconomicStateRepository et les fonctions de
conversion pures economic_state_to_dict / economic_state_from_dict.

Portée strictement respectée :
  - Aucun test de validation économique (un allocated_capital négatif,
    non fini, ou aberrant n'est jamais rejeté par ce composant : ces
    tests ne vérifient donc jamais un tel rejet).
  - Aucun test de décision (ACCEPTED / TRUNCATED / REJECTED n'existe
    pas dans ce composant).
  - Aucun test de journalisation.
  - Aucun test impliquant TransitionRequest, TransitionCause,
    TransitionOrigin, le MetaController ou le Bot : ce Repository ne
    connaît que bot_id (chaîne opaque) et EconomicState.
  - Aucun test du CapitalTransitionGuard lui-même (toujours hors
    périmètre, inchangé).

Chaque test vérifie uniquement le contrat du Repository : conversion
fidèle entre JSON et EconomicState, absence de validation, absence de
comblement silencieux d'erreur, isolation entre bots, écriture
atomique.
"""

import json

import pytest

from capital_transition_guard import EconomicState
from economic_state_repository import (
    EconomicStateRepository,
    economic_state_from_dict,
    economic_state_to_dict,
)


# ============================================================
# FONCTIONS DE CONVERSION PURES
# ============================================================

class TestEconomicStateToDict:
    def test_converts_to_a_dict_with_expected_key(self):
        state = EconomicState(allocated_capital=220.0)

        result = economic_state_to_dict(state)

        assert result == {"allocated_capital": 220.0}

    def test_is_pure_and_does_not_mutate_the_input(self):
        state = EconomicState(allocated_capital=220.0)

        economic_state_to_dict(state)

        assert state.allocated_capital == 220.0

    def test_calling_twice_yields_equal_results(self):
        state = EconomicState(allocated_capital=42.0)

        first = economic_state_to_dict(state)
        second = economic_state_to_dict(state)

        assert first == second


class TestEconomicStateFromDict:
    def test_converts_a_well_formed_dict_to_economic_state(self):
        result = economic_state_from_dict({"allocated_capital": 330.0})

        assert result == EconomicState(allocated_capital=330.0)

    def test_raises_type_error_when_field_is_missing(self):
        with pytest.raises(TypeError):
            economic_state_from_dict({})

    def test_raises_type_error_when_extra_field_is_present(self):
        with pytest.raises(TypeError):
            economic_state_from_dict(
                {"allocated_capital": 100.0, "unexpected_field": 1}
            )

    def test_does_not_validate_negative_allocated_capital(self):
        # Aucune validation economique n'est du ressort de ce
        # composant : une valeur negative est convertie telle quelle.
        result = economic_state_from_dict({"allocated_capital": -50.0})

        assert result.allocated_capital == -50.0

    def test_does_not_reject_non_finite_allocated_capital(self):
        # De meme, un NaN ou un infini n'est pas rejete ici : ce
        # composant ne fait que convertir, il ne juge jamais la
        # plausibilite economique de la valeur.
        result = economic_state_from_dict({"allocated_capital": float("nan")})

        assert result.allocated_capital != result.allocated_capital  # NaN


class TestRoundTripConversion:
    def test_to_dict_then_from_dict_yields_an_equal_economic_state(self):
        original = EconomicState(allocated_capital=275.5)

        round_tripped = economic_state_from_dict(economic_state_to_dict(original))

        assert round_tripped == original


# ============================================================
# ECONOMICSTATEREPOSITORY — LOAD
# ============================================================

class TestRepositoryLoad:
    def test_loads_a_previously_written_json_file(self, tmp_path):
        state_file = tmp_path / "capital_state_bot_1.json"
        state_file.write_text(json.dumps({"allocated_capital": 220.0}))
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        result = repository.load("bot_1")

        assert result == EconomicState(allocated_capital=220.0)

    def test_raises_file_not_found_error_when_no_file_exists(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        with pytest.raises(FileNotFoundError):
            repository.load("unknown_bot")

    def test_raises_json_decode_error_on_malformed_json(self, tmp_path):
        state_file = tmp_path / "capital_state_bot_1.json"
        state_file.write_text("{ this is not valid json")
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        with pytest.raises(json.JSONDecodeError):
            repository.load("bot_1")

    def test_raises_type_error_when_json_structure_does_not_match(self, tmp_path):
        state_file = tmp_path / "capital_state_bot_1.json"
        state_file.write_text(json.dumps({"something_else": 1}))
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        with pytest.raises(TypeError):
            repository.load("bot_1")

    def test_does_not_silently_default_on_missing_file(self, tmp_path):
        # Garde-fou explicite contre le defaut identifie dans l'ancien
        # CapitalTargetController (fichier absent ignore
        # silencieusement) : l'absence de fichier doit lever une
        # exception, jamais retourner une valeur par defaut.
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        with pytest.raises(Exception):
            repository.load("bot_without_file")


# ============================================================
# ECONOMICSTATEREPOSITORY — SAVE
# ============================================================

class TestRepositorySave:
    def test_save_creates_a_readable_json_file(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))
        state = EconomicState(allocated_capital=220.0)

        repository.save("bot_1", state)

        state_file = tmp_path / "capital_state_bot_1.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text()) == {"allocated_capital": 220.0}

    def test_save_then_load_round_trips_correctly(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))
        state = EconomicState(allocated_capital=317.42)

        repository.save("bot_1", state)
        result = repository.load("bot_1")

        assert result == state

    def test_save_overwrites_previous_content_for_the_same_bot(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        repository.save("bot_1", EconomicState(allocated_capital=100.0))
        repository.save("bot_1", EconomicState(allocated_capital=200.0))

        assert repository.load("bot_1") == EconomicState(allocated_capital=200.0)

    def test_save_does_not_leave_a_temporary_file_behind(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        repository.save("bot_1", EconomicState(allocated_capital=100.0))

        leftover_tmp_files = list(tmp_path.glob("*.tmp"))
        assert leftover_tmp_files == []

    def test_save_does_not_mutate_the_provided_state(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))
        state = EconomicState(allocated_capital=150.0)

        repository.save("bot_1", state)

        assert state.allocated_capital == 150.0

    def test_save_does_not_validate_a_negative_allocated_capital(self, tmp_path):
        # Aucune validation economique : le Repository persiste la
        # valeur telle quelle.
        repository = EconomicStateRepository(state_dir=str(tmp_path))
        state = EconomicState(allocated_capital=-42.0)

        repository.save("bot_1", state)

        assert repository.load("bot_1") == state


# ============================================================
# ISOLATION ENTRE BOTS
# ============================================================

class TestRepositoryIsolatesBots:
    def test_saving_for_one_bot_does_not_affect_another_bots_file(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        repository.save("bot_1", EconomicState(allocated_capital=100.0))
        repository.save("bot_2", EconomicState(allocated_capital=999.0))

        assert repository.load("bot_1") == EconomicState(allocated_capital=100.0)
        assert repository.load("bot_2") == EconomicState(allocated_capital=999.0)

    def test_each_bot_has_its_own_distinct_file(self, tmp_path):
        repository = EconomicStateRepository(state_dir=str(tmp_path))

        repository.save("bot_1", EconomicState(allocated_capital=1.0))
        repository.save("bot_2", EconomicState(allocated_capital=2.0))

        assert (tmp_path / "capital_state_bot_1.json").exists()
        assert (tmp_path / "capital_state_bot_2.json").exists()


# ============================================================
# ABSENCE DE CONNAISSANCE DU BOT OU DU META-CONTROLLER
# ============================================================

class TestRepositoryHasNoDomainKnowledge:
    def test_load_and_save_are_the_only_public_methods(self):
        import inspect

        public_methods = {
            name
            for name, member in inspect.getmembers(
                EconomicStateRepository, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }
        assert public_methods == {"load", "save"}

    def test_repository_module_does_not_import_transition_related_symbols(self):
        # Garde-fou structurel : le Repository ne doit rien connaitre
        # des transitions, causes, origines ou du MetaController.
        import economic_state_repository as module

        forbidden_symbols = {
            "TransitionCause",
            "TransitionOrigin",
            "TransitionRequest",
            "TransitionResult",
            "TransitionStatus",
            "TransitionValue",
            "AbsoluteAmount",
            "RelativeCorrection",
            "AppliedDelta",
            "CapitalTransitionJournal",
            "CapitalTransitionJournalEntry",
            "CapitalTransitionGuard",
        }
        module_attributes = set(dir(module))
        assert forbidden_symbols.isdisjoint(module_attributes)
