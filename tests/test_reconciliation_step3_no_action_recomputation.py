"""
tests/test_reconciliation_step3_no_action_recomputation.py

Test de régression structurel pour l'étape 3 de l'implémentation de
RN-027/RN-028 : vérifier, de façon permanente et automatisée, qu'aucun
recalcul indépendant de `action` ne peut être réintroduit à l'avenir
dans virtual_treasury_manager.py ou ses consommateurs directs.

Cette étape ne modifie aucun code — elle fige, sous forme de test,
une vérification déjà faite manuellement (grep exhaustif) au moment
de l'implémentation, pour qu'un futur commit ne puisse pas
silencieusement réintroduire la classe de bug corrigée à l'étape 2
(action dérivé de recommended_budget - current_budget).

Portée strictement respectée :
  - Vérifie uniquement des propriétés structurelles du code source
    (comme test_stop_loss_rn025.py l'a déjà fait pour un sujet
    différent), pas de nouveau comportement fonctionnel.
"""

from pathlib import Path

VTM_SOURCE = Path(__file__).parent.parent / "virtual_treasury_manager.py"
EXECUTION_PLANNER_SOURCE = Path(__file__).parent.parent / "execution_planner.py"
META_REPORT_SOURCE = Path(__file__).parent.parent / "meta_report.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestActionAssignedExactlyOnceInCompute:
    def test_action_is_assigned_from_the_actions_dict_exactly_once(self):
        source = _read(VTM_SOURCE)
        # La seule affectation legitime de la variable locale `action`
        # doit etre la lecture depuis le dictionnaire deja construit a
        # l'etape Deadband.
        assert source.count("action = actions[s.symbol]") == 1

    def test_no_conditional_classification_of_action_remains_in_compute(self):
        # Motif exact de l'ancien recalcul supprime a l'etape 2 :
        # "if abs(delta) < ...MIN_DELTA_ACTION: action = ...HOLD"
        # suivi d'un elif/else construisant INCREASE/DECREASE a partir
        # d'un delta local. Ce motif ne doit plus exister nulle part.
        source = _read(VTM_SOURCE)
        assert "action = AllocationAction.HOLD" not in source
        assert "action = AllocationAction.INCREASE" not in source
        assert "action = AllocationAction.DECREASE" not in source

    def test_classify_delta_sign_remains_the_only_place_returning_these_three_values(self):
        # En dehors de _classify_delta_sign() elle-meme (et de la
        # definition de l'enum), aucune autre fonction ne doit
        # retourner/construire directement un AllocationAction a partir
        # d'un test de signe.
        source = _read(VTM_SOURCE)
        occurrences = source.count("return AllocationAction.INCREASE if delta > 0 else AllocationAction.DECREASE")
        assert occurrences == 1


class TestDownstreamConsumersNeverRecomputeAction:
    def test_execution_planner_only_passes_through_action(self):
        source = _read(EXECUTION_PLANNER_SOURCE)
        # execution_planner.py ne doit contenir aucune construction
        # independante d'AllocationAction : uniquement des lectures
        # (action=alloc.action) transmises telles quelles.
        assert "AllocationAction.HOLD" not in source
        assert "AllocationAction.INCREASE" not in source
        assert "AllocationAction.DECREASE" not in source
        assert "action=alloc.action" in source

    def test_meta_report_only_displays_action_never_recomputes_it(self):
        source = _read(META_REPORT_SOURCE)
        assert "AllocationAction.HOLD" not in source
        assert "AllocationAction.INCREASE" not in source
        assert "AllocationAction.DECREASE" not in source
