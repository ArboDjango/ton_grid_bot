"""
tests/test_reconciliation_step2_action_at_deadband.py

Tests couvrant exclusivement l'étape 2 de l'implémentation de
RN-027/RN-028 : le calcul de `action` est déplacé à l'étape Deadband
et n'est plus jamais recalculé à partir du budget final.

Portée strictement respectée :
  - Ne teste que le déplacement du calcul de `action`. La réconciliation
    elle-même (le mécanisme qui produit encore, à ce stade, un
    `recommended_budget` incohérent pour FIL/STX) n'est PAS corrigée
    à cette étape — elle le sera aux étapes 5/6. C'est pourquoi ces
    tests vérifient uniquement `action`, jamais la valeur numérique de
    `recommended_budget`/`delta`.
  - Ne réévalue pas _classify_delta_sign() elle-même (déjà testée à
    l'étape 1).
"""

import pytest

from virtual_treasury_manager import VirtualTreasuryManager, StrategyState, AllocationAction


class TestActionReflectsDeadbandDecision:
    def test_action_matches_raw_delta_sign_even_when_reconciliation_disagrees(self):
        # Reproduit exactement le scenario de l'incident FIL/STX/RSR/
        # EGLD/INJ du 20/07/2026. Avant l'etape 2, `action` etait
        # recalcule depuis `recommended_budget - current_budget` (le
        # resultat de l'ancienne reconciliation buggee), ce qui pouvait
        # inverser le signe. Desormais, `action` doit refleter
        # uniquement la decision prise a l'etape Deadband (le delta
        # lisse, avant reconciliation), independamment de ce que la
        # reconciliation (encore non corrigee a ce stade) produit
        # ensuite comme recommended_budget.
        strategies = [
            StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
            StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
            StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
            StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
            StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
        ]
        free_usdt = 333.82

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        fil = next(a for a in result.allocations if a.symbol == "FILUSDT")
        stx = next(a for a in result.allocations if a.symbol == "STXUSDT")

        # Le coeur de l'etape 2 : FIL et STX doivent maintenant etre
        # rapportes en DECREASE (leur decision Deadband reelle),
        # meme si `recommended_budget` (pas encore corrige par la
        # reconciliation) peut encore etre superieur a `current_budget`.
        assert fil.action is AllocationAction.DECREASE
        assert stx.action is AllocationAction.DECREASE

    def test_action_is_increase_for_strategies_correctly_needing_more_capital(self):
        strategies = [
            StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
            StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
            StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
            StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
            StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
        ]
        free_usdt = 333.82

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        for sym in ("RSRUSDT", "EGLDUSDT", "INJUSDT"):
            alloc = next(a for a in result.allocations if a.symbol == sym)
            assert alloc.action is AllocationAction.INCREASE

    def test_action_is_hold_when_raw_delta_is_within_deadband(self):
        # Trois strategies a poids et budget egaux : la cible naturelle
        # (33.3% chacune) est deja atteinte exactement par le budget
        # actuel, et reste sous le plafond MAX_BUDGET_PCT=40% (un cas a
        # seulement 2 strategies serait structurellement infaisable,
        # puisque 50% > 40%). Le delta lisse doit rester nul, donc
        # HOLD, quel que soit ce que produit ensuite la reconciliation.
        strategies = [
            StrategyState(symbol="A", current_budget=200.0, goi=0.5),
            StrategyState(symbol="B", current_budget=200.0, goi=0.5),
            StrategyState(symbol="C", current_budget=200.0, goi=0.5),
        ]
        free_usdt = 0.0

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        for alloc in result.allocations:
            assert alloc.action is AllocationAction.HOLD

    def test_action_is_never_recomputed_from_recommended_minus_current(self):
        # Garde-fou explicite : si une future modification reintroduisait
        # un recalcul de `action` a partir de `recommended_budget -
        # current_budget`, ce test le detecterait des que la
        # reconciliation produit un recommended_budget de signe oppose
        # au delta Deadband reel — exactement le scenario FIL/STX.
        #
        # MISE A JOUR DELIBEREE (etape 6) : la reconciliation est
        # desormais corrigee (Famille C, RN-027) ; recommended_budget
        # de FIL est donc lui aussi correctement inferieur a
        # current_budget. Ce test verifie maintenant que action ET
        # recommended_budget sont cohérents entre eux (les deux en
        # baisse), la preuve que la correction de bout en bout
        # fonctionne — plus seulement que action seul est correct
        # pendant que recommended_budget restait incoherent (etat
        # transitoire de l'etape 2, desormais resolu).
        strategies = [
            StrategyState(symbol="FILUSDT", current_budget=368.85, goi=0.634),
            StrategyState(symbol="STXUSDT", current_budget=328.81, goi=0.631),
            StrategyState(symbol="RSRUSDT", current_budget=92.24, goi=0.687),
            StrategyState(symbol="EGLDUSDT", current_budget=112.99, goi=0.724),
            StrategyState(symbol="INJUSDT", current_budget=137.14, goi=0.744),
        ]
        free_usdt = 333.82

        result = VirtualTreasuryManager.compute(strategies, free_usdt)

        fil = next(a for a in result.allocations if a.symbol == "FILUSDT")
        assert fil.recommended_budget < fil.current_budget  # corrige : coherent avec DECREASE
        assert fil.action is AllocationAction.DECREASE
