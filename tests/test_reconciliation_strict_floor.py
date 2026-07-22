"""
tests/test_reconciliation_strict_floor.py

Tests couvrant une règle décidée explicitement après la première
implémentation de l'algorithme de réconciliation (étape 6,
RN-027/RN-028) : une décision INCREASE ou DECREASE ne peut jamais être
neutralisée (ramenée à un delta nul, ou à une magnitude inférieure à
MIN_DELTA_ACTION) par la réconciliation. Seule une stratégie déjà en
HOLD peut avoir un delta nul.

Contexte de la décision : la première version de l'algorithme
autorisait un delta décidé (ex: DECREASE) à être réduit jusqu'à
exactement 0 pour absorber un résidu important — ce qui ne viole pas
le signe au sens strict (0 n'est pas positif), mais neutralise la
décision, contrairement à l'esprit de RN-027 ("la réconciliation
préserve les décisions, elle ne les réinterprète pas"). Le plancher
retenu est MIN_DELTA_ACTION — le même seuil qui qualifie déjà une
décision comme INCREASE/DECREASE plutôt que HOLD à l'étape Deadband.

Portée strictement respectée :
  - Ne teste que cette règle précise (le plancher strict), pas les
    autres invariants déjà couverts par les étapes précédentes.
"""

import pytest

from virtual_treasury_manager import (
    VirtualTreasuryManager,
    StrategyState,
    AllocationAction,
    TreasuryReconciliationError,
)


class TestDecisionNeverNeutralized:
    def test_real_incident_scenario_fil_stx_never_neutralized(self):
        # Reproduit exactement l'incident FIL/STX/RSR/EGLD/INJ. Avant
        # cette regle, FIL et STX voyaient leur DECREASE neutralise a
        # exactement 0 (residu tres important a absorber). Desormais,
        # les deux doivent rester a un delta strictement inferieur ou
        # egal a -MIN_DELTA_ACTION.
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

        assert fil.action is AllocationAction.DECREASE
        assert stx.action is AllocationAction.DECREASE
        assert fil.delta <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6
        assert stx.delta <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6

    def test_decrease_delta_never_falls_in_the_dead_zone(self):
        # MISE A JOUR (RN-029, revue d'architecture) : une DECREASE
        # n'est plus seulement protegee par un plancher, elle est
        # desormais entierement gelee — la reconciliation ne la touche
        # jamais, dans aucune direction (I8). L'assertion est renforcee
        # en consequence : non seulement A reste sous le plancher, mais
        # elle reste exactement egale a sa decision d'origine.
        decided_deltas = {"A": -50.0, "B": 100.0}
        current_budgets = {"A": 100.0, "B": 100.0}
        goi_dict = {"A": 0.3, "B": 0.7}

        result = VirtualTreasuryManager._reconcile_deltas(
            decided_deltas, current_budgets,
            min_budget=10.0, max_budget=500.0,
            capital_total=250.0, goi_dict=goi_dict,
        )

        assert result["A"] <= -VirtualTreasuryManager.MIN_DELTA_ACTION + 1e-6
        assert result["A"] == pytest.approx(decided_deltas["A"], abs=1e-9)

    def test_increase_delta_never_falls_in_the_dead_zone(self):
        # MISE A JOUR DELIBEREE (RN-029, revue d'architecture) : le
        # scenario original reposait sur une strategie B en DECREASE
        # amplifiee pour absorber le depassement — exactement ce que
        # RN-029 interdit desormais (I8 : une DECREASE n'est plus
        # jamais modifiee par la reconciliation). Reconstruit avec deux
        # strategies INCREASE, dont la capacite combinee suffit a
        # absorber le depassement sans qu'aucune ne tombe sous
        # MIN_DELTA_ACTION.
        decided_deltas = {"A": 50.0, "B": 30.0}
        current_budgets = {"A": 100.0, "B": 100.0}
        goi_dict = {"A": 0.3, "B": 0.7}
        capital_total = 260.0  # depassement de 20 (200+80=280 > 260)

        result = VirtualTreasuryManager._reconcile_deltas(
            decided_deltas, current_budgets,
            min_budget=10.0, max_budget=500.0,
            capital_total=capital_total, goi_dict=goi_dict,
        )

        assert result["A"] >= VirtualTreasuryManager.MIN_DELTA_ACTION - 1e-6

    def test_hold_strategy_can_still_have_a_zero_delta(self):
        # La regle du plancher ne s'applique qu'aux decisions deja
        # prises (INCREASE/DECREASE). Une strategie en HOLD
        # (decided_delta=0) doit rester a 0, ce qui reste parfaitement
        # valide et distinct d'une "neutralisation".
        #
        # capital_total est fixe pour correspondre exactement a la
        # somme des decisions deja prises (A=0, B=+50), afin qu'aucun
        # residu ne soit necessaire et que la reconciliation n'ait
        # rien a ajuster.
        decided_deltas = {"A": 0.0, "B": 50.0}
        current_budgets = {"A": 100.0, "B": 100.0}
        goi_dict = {"A": 0.3, "B": 0.7}

        result = VirtualTreasuryManager._reconcile_deltas(
            decided_deltas, current_budgets,
            min_budget=10.0, max_budget=500.0,
            capital_total=250.0, goi_dict=goi_dict,
        )

        assert result["A"] == 0.0

    def test_raises_when_no_room_for_the_minimal_floor(self):
        # Une strategie DECREASE dont current_budget est deja tres
        # proche de min_budget ne dispose pas de MIN_DELTA_ACTION de
        # marge : la reconciliation doit echouer explicitement plutot
        # que de neutraliser sa decision.
        decided_deltas = {"A": -50.0}
        current_budgets = {"A": 15.0}  # min_budget=10 -> marge de 5 seulement
        goi_dict = {"A": 0.5}

        with pytest.raises(TreasuryReconciliationError):
            VirtualTreasuryManager._reconcile_deltas(
                decided_deltas, current_budgets,
                min_budget=10.0, max_budget=500.0,
                capital_total=15.0, goi_dict=goi_dict,
            )

    def test_infeasibility_diagnostic_identifies_the_constrained_strategy(self):
        decided_deltas = {"A": -50.0}
        current_budgets = {"A": 15.0}
        goi_dict = {"A": 0.5}

        with pytest.raises(TreasuryReconciliationError) as exc_info:
            VirtualTreasuryManager._reconcile_deltas(
                decided_deltas, current_budgets,
                min_budget=10.0, max_budget=500.0,
                capital_total=15.0, goi_dict=goi_dict,
            )

        assert "A" in exc_info.value.saturation
