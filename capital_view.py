# capital_view.py
# RN-105.1 : Entité métier officielle représentant l'état économique complet du moteur.
# RN-019 : Ajout de l'alpha (performance) et alignement sur allocated_capital.

from dataclasses import dataclass
from typing import Optional
import time


@dataclass(frozen=True)
class CapitalView:
    timestamp: float
    symbol: str

    wallet_balance: float
    reference_budget: float          # allocated_capital (anciennement capital_usdc)
    alpha: float                     # wallet_balance - reference_budget (performance)
    grid_budget: float
    target_budget: Optional[float]

    strategic_budget: float
    capital_ratio: float

    buy_exposure: float
    sell_exposure: float

    effective_buy_budget: float
    effective_sell_budget: float

    gv_buy: float
    gv_sell: float

    stress: float
    adx: float
    regime: str

    open_orders: int
    engaged_capital: float

    health_status: str


class CapitalViewBuilder:
    compute_gv_fn = None

    @staticmethod
    def build(
        *,
        symbol: str,
        state: dict,
        price: float,
        capital_view_aggregates: dict,
        capital_target_controller,
        macro_data: dict,
        stress: float,
        buy_exposure: float,
        sell_exposure: float,
        regime: str,
        grid_sell_len: int,
        grid_buy_len: int,
        timestamp: Optional[float] = None,
    ) -> CapitalView:
        if CapitalViewBuilder.compute_gv_fn is None:
            raise RuntimeError(
                "CapitalViewBuilder.compute_gv_fn n'a pas été injectée. "
                "Appeler CapitalViewBuilder.compute_gv_fn = compute_gv depuis bot_gateio."
            )

        if timestamp is None:
            timestamp = time.time()

        wallet_balance = capital_view_aggregates["total_wallet"]          # wallet_real
        reference_budget = state.get("allocated_capital", 0.0)            # ← MODIFIÉ : allocated_capital
        alpha = wallet_balance - reference_budget                         # ← NOUVEAU
        grid_budget = capital_view_aggregates.get("capital_for_grid", 0.0)

        target_budget = (
            capital_target_controller.current_target
            if capital_target_controller
            else None
        )
        capital_ratio = (
            capital_target_controller.get_ratio()
            if capital_target_controller
            else 1.0
        )

        active_capital_ratio = state.get("ACTIVE_CAPITAL_RATIO", 0.9)
        strategic_budget = grid_budget * active_capital_ratio * capital_ratio
        effective_buy_budget = strategic_budget * buy_exposure
        effective_sell_budget = strategic_budget * sell_exposure

        P0 = state.get("P0", price)
        Gul = state.get("Gul", price * 1.05)
        Gll = state.get("Gll", price * 0.95)
        nu = state.get("nu", 5)
        nl = state.get("nl", 5)
        density_k = state.get("density_k", 0.65)

        gv_buy = 0.0
        gv_sell = 0.0
        if effective_buy_budget > 0 and P0 > 0:
            gv_buy = CapitalViewBuilder.compute_gv_fn(
                effective_buy_budget, P0, Gul, Gll, nu, nl, density_k
            )
        if effective_sell_budget > 0 and P0 > 0:
            gv_sell = CapitalViewBuilder.compute_gv_fn(
                effective_sell_budget, P0, Gul, Gll, nu, nl, density_k
            )

        open_orders = grid_sell_len + grid_buy_len
        inventory_cost = capital_view_aggregates.get("inventory_cost", 0.0)
        quote_available = capital_view_aggregates.get("quote_available", 0.0)
        engaged_capital = inventory_cost + quote_available

        drawdown = capital_view_aggregates.get("drawdown", 0.0)
        if drawdown >= 0.25 or stress > 0.8:
            health_status = "DEGRADED"
        elif drawdown >= 0.10 or stress >= 0.5:
            health_status = "WARNING"
        else:
            health_status = "HEALTHY"

        return CapitalView(
            timestamp=timestamp,
            symbol=symbol,
            wallet_balance=wallet_balance,
            reference_budget=reference_budget,
            alpha=alpha,                                                # ← NOUVEAU
            grid_budget=grid_budget,
            target_budget=target_budget,
            strategic_budget=strategic_budget,
            capital_ratio=capital_ratio,
            buy_exposure=buy_exposure,
            sell_exposure=sell_exposure,
            effective_buy_budget=effective_buy_budget,
            effective_sell_budget=effective_sell_budget,
            gv_buy=gv_buy,
            gv_sell=gv_sell,
            stress=stress,
            adx=macro_data.get("adx", 0.0),
            regime=regime,
            open_orders=open_orders,
            engaged_capital=engaged_capital,
            health_status=health_status,
        )
