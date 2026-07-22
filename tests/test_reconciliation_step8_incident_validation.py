"""
tests/test_reconciliation_step8_incident_validation.py

Étape 8 (finale) de l'implémentation de RN-027/RN-028 : validation
exhaustive du scénario réel de l'incident du 20/07/2026
(FIL/STX/RSR/EGLD/INJ), rassemblant en un seul endroit toutes les
propriétés qui devaient être rétablies.

Ce fichier ne réintroduit aucune nouvelle logique de test — il
consolide, sur le scénario réel exact (valeurs de current_budget/goi
telles qu'observées en production), l'ensemble des vérifications déjà
couvertes séparément aux étapes 1 à 6, pour disposer d'un test de
référence unique et exhaustif prouvant la résolution complète de
l'incident.

Rappel de l'incident original :
    FILUSDT : Budget actuel=368.85, Budget cible=254.78,
              Budget recommandé rapporté=395.56, Delta=+26.71
    Une stratégie déjà sur-allouée (26.8% > cible 18.5%) recevait une
    recommandation d'augmentation — signe inversé par la réconciliation.
"""

import pytest

from virtual_treasury_manager import (
    VirtualTreasuryManager,
    StrategyState,
    AllocationAction,
)


REAL_INCIDENT_STRATEGIES = [
    StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
    StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
    StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
    StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
    StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
]
REAL_INCIDENT_FREE_USDT = 333.82


class TestIncidentFullyResolved:
    """
    Validation de bout en bout sur les valeurs exactes de l'incident
    réel. Chaque test vérifie une propriété distincte ; ensemble, ils
    couvrent l'intégralité de ce que RN-027/RN-028 exigeaient.
    """

    def test_no_exception_is_raised_on_the_real_scenario(self):
        # Le scenario reel doit etre feasible de bout en bout (aucune
        # TreasuryReconciliationError, aucune AssertionError).
        VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

    def test_fil_and_stx_remain_in_decrease(self):
        # Le coeur de l'incident original : FIL et STX, deja
        # sur-alloues par rapport a leur cible, doivent rester en
        # DECREASE — jamais INCREASE.
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        fil = next(a for a in result.allocations if a.symbol == "FILUSDT")
        stx = next(a for a in result.allocations if a.symbol == "STXUSDT")

        assert fil.action is AllocationAction.DECREASE
        assert stx.action is AllocationAction.DECREASE

    def test_fil_and_stx_recommended_budget_is_consistent_with_decrease(self):
        # Pas seulement `action` correct : recommended_budget doit
        # etre reellement inferieur a current_budget (coherence
        # complete entre le champ action et le champ recommended_budget).
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        fil = next(a for a in result.allocations if a.symbol == "FILUSDT")
        stx = next(a for a in result.allocations if a.symbol == "STXUSDT")

        assert fil.recommended_budget < fil.current_budget
        assert stx.recommended_budget < stx.current_budget

    def test_fil_and_stx_decision_is_not_neutralized(self):
        # Au-dela du simple signe : la decision ne doit pas non plus
        # etre neutralisee (delta ramene a une magnitude negligeable).
        # Le plancher retenu est MIN_DELTA_ACTION.
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        fil = next(a for a in result.allocations if a.symbol == "FILUSDT")
        stx = next(a for a in result.allocations if a.symbol == "STXUSDT")

        assert fil.delta <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6
        assert stx.delta <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6

    def test_rsr_egld_inj_remain_in_increase(self):
        # Ces trois strategies etaient legitimement sous-allouees
        # (actuelle < cible) : elles doivent rester en INCREASE, comme
        # deja observe correctement meme avant la correction.
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        for sym in ("RSRUSDT", "EGLDUSDT", "INJUSDT"):
            alloc = next(a for a in result.allocations if a.symbol == sym)
            assert alloc.action is AllocationAction.INCREASE
            assert alloc.recommended_budget > alloc.current_budget

    def test_capital_is_not_exceeded_and_unallocated_remainder_is_legitimate(self):
        # MISE A JOUR DELIBEREE (RN-029) : I2 est desormais une
        # inegalite (Σ ≤ capital_total), pas une egalite stricte. Sur
        # ce scenario reel, le lissage n'a decide de bouger qu'environ
        # 67 USDT au net alors que le cash libre est de 333.82 USDT :
        # le reste (~267 USDT) est legitimement non alloue ce cycle,
        # et NE DOIT PAS etre comble en amplifiant RSR/EGLD/INJ au-dela
        # de ce que le lissage a decide (c'est precisement l'incident
        # qui a motive RN-029).
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        capital_total = sum(s.current_budget for s in REAL_INCIDENT_STRATEGIES) + REAL_INCIDENT_FREE_USDT
        sum_recommended = sum(a.recommended_budget for a in result.allocations)

        assert sum_recommended <= capital_total + 1e-6
        # Le capital non alloue doit etre substantiel ici (pas juste
        # une tolerance numerique) : preuve que rien n'a ete comble.
        assert capital_total - sum_recommended > 100.0

    def test_no_strategy_exceeds_its_bounds(self):
        # I3 (RN-028) : aucune stratégie ne doit dépasser
        # [MIN_BUDGET, MAX_BUDGET_PCT × capital_total].
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        capital_total = sum(s.current_budget for s in REAL_INCIDENT_STRATEGIES) + REAL_INCIDENT_FREE_USDT
        max_budget = VirtualTreasuryManager.MAX_BUDGET_PCT * capital_total
        min_budget = VirtualTreasuryManager.MIN_BUDGET

        for alloc in result.allocations:
            assert min_budget - 1e-6 <= alloc.recommended_budget <= max_budget + 1e-6

    def test_action_matches_delta_sign_for_every_strategy(self):
        # Garde-fou general : pour chaque strategie, action et delta
        # doivent etre mutuellement coherents (plus jamais de
        # AllocationAction.DECREASE avec un delta positif, ou l'inverse).
        result = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        for alloc in result.allocations:
            if alloc.action is AllocationAction.INCREASE:
                assert alloc.delta > 0
            elif alloc.action is AllocationAction.DECREASE:
                assert alloc.delta < 0
            else:
                assert alloc.delta == 0

    def test_result_is_deterministic_across_repeated_calls(self):
        # I5 (RN-028) : meme scenario, memes resultats, a chaque appel.
        result_a = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)
        result_b = VirtualTreasuryManager.compute(REAL_INCIDENT_STRATEGIES, REAL_INCIDENT_FREE_USDT)

        for a, b in zip(result_a.allocations, result_b.allocations):
            assert a.symbol == b.symbol
            assert a.action == b.action
            assert a.recommended_budget == pytest.approx(b.recommended_budget, abs=1e-9)
