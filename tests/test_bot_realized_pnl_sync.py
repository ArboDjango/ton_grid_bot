"""
tests/test_bot_realized_pnl_sync.py

Tests couvrant exclusivement l'étape 5 du plan de reconstruction : la
migration des profits réalisés (bot_gateio.py) vers le
CapitalTransitionGuard, TransitionType REALIZED_PROFIT.

Portée strictement respectée :
  - Aucun test ne réévalue le calcul du profit lui-même (FIFO, frais,
    prix moyen, Gv) : ces calculs restent entièrement dans
    bot_gateio.py et ne sont pas exercés ici. Les montants utilisés
    dans ces tests sont fournis directement, comme le ferait
    bot_gateio.py après avoir calculé pnl_trade.
  - Aucun test ne porte sur REALIZED_LOSS (hors périmètre de cette
    étape).
  - Aucun test ne réévalue validate_transition_request,
    resolve_transition_value, apply_delta, CapitalTransitionJournal en
    tant que tels : déjà testés ailleurs.
  - Ces tests vérifient :
      * build_realized_profit_request produit une TransitionRequest
        REALIZED_PROFIT correcte, avec le montant transmis strictement
        inchangé ;
      * l'intégration bout-en-bout (Guard + StateDictEconomicRepository,
        déjà validé à l'étape 3) applique correctement le montant à
        allocated_capital ;
      * une entrée de journal est créée avec le bon montant ;
      * le Repository (state dict) est mis à jour sans toucher aux
        autres champs (FIFO, PnL cumulé, grille, etc.) ;
      * l'absence de régression sur les calculs économiques adjacents.
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
from bot_capital_sync import StateDictEconomicRepository
from bot_realized_pnl_sync import build_realized_profit_request


# ============================================================
# BUILD_REALIZED_PROFIT_REQUEST
# ============================================================

class TestBuildRealizedProfitRequest:
    def test_produces_a_realized_profit_transition_request(self):
        request = build_realized_profit_request(
            bot_id="gateio_rsrusdt", amount=12.3456, justification="test"
        )

        assert request.bot_id == "gateio_rsrusdt"
        assert request.cause is TransitionCause.REALIZED_PROFIT
        assert request.origin is TransitionOrigin.BOT

    def test_amount_is_transmitted_exactly_unchanged(self):
        # Le montant transmis au Guard doit etre exactement celui deja
        # calcule par bot_gateio.py (pnl_trade), sans arrondi ni
        # transformation d'aucune sorte.
        pnl_trade = 7.123456789
        request = build_realized_profit_request(bot_id="bot_1", amount=pnl_trade)

        assert isinstance(request.value, AbsoluteAmount)
        assert request.value.amount == pnl_trade

    def test_justification_defaults_to_empty_string(self):
        request = build_realized_profit_request(bot_id="bot_1", amount=5.0)
        assert request.justification == ""

    def test_justification_can_be_provided(self):
        request = build_realized_profit_request(
            bot_id="bot_1", amount=5.0, justification="motif"
        )
        assert request.justification == "motif"

    def test_does_not_validate_or_reject_a_non_positive_amount(self):
        # Cette fonction ne decide jamais si un montant est un profit :
        # c'est a l'appelant (bot_gateio.py) de ne l'invoquer que
        # lorsque pnl_trade > 0. La fonction elle-meme reste purement
        # mecanique et ne rejette pas un montant negatif ou nul.
        request = build_realized_profit_request(bot_id="bot_1", amount=-3.0)
        assert request.value.amount == -3.0

    def test_is_pure_same_inputs_yield_equal_requests_value(self):
        request_a = build_realized_profit_request(bot_id="bot_1", amount=10.0)
        request_b = build_realized_profit_request(bot_id="bot_1", amount=10.0)
        assert request_a.value == request_b.value
        assert request_a.cause == request_b.cause
        assert request_a.origin == request_b.origin


# ============================================================
# INTEGRATION BOUT-EN-BOUT (Guard + StateDictEconomicRepository)
# ============================================================

class TestRealizedProfitEndToEnd:
    def _make_guard(self, state, bot_id):
        repository = StateDictEconomicRepository(state, bot_id, save_fn=lambda s: None)
        journal = CapitalTransitionJournal()
        guard = CapitalTransitionGuard(repository=repository, journal=journal)
        return guard, journal

    def test_profit_increases_allocated_capital_by_exactly_the_amount(self):
        state = {"allocated_capital": 220.0}
        guard, journal = self._make_guard(state, "gateio_rsrusdt")

        pnl_trade = 8.4321  # tel que calcule par bot_gateio.py (FIFO, net de frais)
        request = build_realized_profit_request(
            bot_id="gateio_rsrusdt", amount=pnl_trade
        )
        result = guard.submit_transition(request)

        assert result.status is TransitionStatus.ACCEPTED
        assert state["allocated_capital"] == pytest.approx(220.0 + pnl_trade)

    def test_result_applied_value_matches_pnl_trade_exactly(self):
        state = {"allocated_capital": 100.0}
        guard, journal = self._make_guard(state, "bot_1")

        pnl_trade = 3.14159
        request = build_realized_profit_request(bot_id="bot_1", amount=pnl_trade)
        result = guard.submit_transition(request)

        assert result.applied_value.amount == pytest.approx(pnl_trade)

    def test_creates_exactly_one_journal_entry_with_the_correct_cause(self):
        state = {"allocated_capital": 100.0}
        guard, journal = self._make_guard(state, "bot_1")

        request = build_realized_profit_request(bot_id="bot_1", amount=5.5)
        guard.submit_transition(request)

        history = journal.history_for("bot_1")
        assert len(history) == 1
        entry = history[0]
        assert entry.cause is TransitionCause.REALIZED_PROFIT
        assert entry.origin is TransitionOrigin.BOT
        assert entry.status is TransitionStatus.ACCEPTED
        assert entry.state_before == EconomicState(allocated_capital=100.0)
        assert entry.state_after == EconomicState(allocated_capital=105.5)

    def test_is_retrievable_via_guard_get_history(self):
        state = {"allocated_capital": 100.0}
        guard, journal = self._make_guard(state, "bot_1")

        request = build_realized_profit_request(bot_id="bot_1", amount=20.0)
        guard.submit_transition(request)

        history = guard.get_history("bot_1")
        assert len(history) == 1
        assert history[0].cause is TransitionCause.REALIZED_PROFIT

    def test_repository_state_dict_reflects_new_allocated_capital(self):
        state = {"allocated_capital": 50.0}
        guard, journal = self._make_guard(state, "bot_1")

        request = build_realized_profit_request(bot_id="bot_1", amount=12.5)
        guard.submit_transition(request)

        assert state["allocated_capital"] == pytest.approx(62.5)

    def test_other_state_fields_remain_untouched(self):
        # Absence de regression : seuls allocated_capital est modifie ;
        # tout ce qui concerne FIFO/inventaire/PnL cumule/grille reste
        # strictement inchange par ce mecanisme.
        state = {
            "allocated_capital": 100.0,
            "total_pnl": 42.0,
            "inventory_lots": [{"qty": 10.0, "buy_price": 1.0}],
            "sell_grid": [1.1, 1.2],
            "buy_grid": [0.9, 0.8],
            "wallet_peak": 999.0,
        }
        guard, journal = self._make_guard(state, "bot_1")

        request = build_realized_profit_request(bot_id="bot_1", amount=7.0)
        guard.submit_transition(request)

        assert state["total_pnl"] == 42.0
        assert state["inventory_lots"] == [{"qty": 10.0, "buy_price": 1.0}]
        assert state["sell_grid"] == [1.1, 1.2]
        assert state["buy_grid"] == [0.9, 0.8]
        assert state["wallet_peak"] == 999.0
        assert state["allocated_capital"] == pytest.approx(107.0)

    def test_multiple_successive_profits_accumulate_correctly(self):
        # Simule plusieurs ventes profitables successives : chaque
        # transition s'applique sur l'etat resultant de la precedente,
        # jamais sur un etat fige.
        state = {"allocated_capital": 200.0}
        guard, journal = self._make_guard(state, "bot_1")

        for pnl_trade in [5.0, 3.25, 1.75]:
            request = build_realized_profit_request(bot_id="bot_1", amount=pnl_trade)
            guard.submit_transition(request)

        assert state["allocated_capital"] == pytest.approx(200.0 + 5.0 + 3.25 + 1.75)
        assert len(journal.history_for("bot_1")) == 3

    def test_does_not_affect_a_different_bot(self):
        state_bot_1 = {"allocated_capital": 100.0}
        state_bot_2 = {"allocated_capital": 300.0}
        guard_1, _ = self._make_guard(state_bot_1, "bot_1")
        guard_2, _ = self._make_guard(state_bot_2, "bot_2")

        guard_1.submit_transition(
            build_realized_profit_request(bot_id="bot_1", amount=10.0)
        )

        assert state_bot_1["allocated_capital"] == pytest.approx(110.0)
        assert state_bot_2["allocated_capital"] == pytest.approx(300.0)
