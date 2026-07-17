# capital_view.py
# RN-105.1 : Entité métier officielle représentant l'état économique complet du moteur.
# RN-019 : Ajout de l'alpha (performance) et alignement sur allocated_capital.
# RN-020 : Single Source of Economic Truth — compute_capital_view() devient la seule
#          fonction de calcul économique, partagée par bot_gateio.py et analyse.py.
#          Elle est pure : indépendante de tout exchange, elle ne reçoit que des
#          primitives (state, price, soldes réels) et ne fait plus d'appel réseau.

from dataclasses import dataclass
from typing import Optional
import time

import inventory_manager as inv_mgr


# ============================================================
# SINGLE SOURCE OF ECONOMIC TRUTH (RN-020)
# ============================================================
def compute_capital_view(
    state: dict,
    price: float,
    quote_balance: float,
    base_balance: float,
    update_peak: bool = False,
) -> dict:
    """
    Calcule la vue économique unique du bot (RN-020).

    Fonction pure et indépendante de l'exchange : elle ne reçoit que des
    primitives (state, price, soldes réels déjà obtenus par l'appelant) et
    n'effectue elle-même aucun appel réseau.

    Paramètres :
      - state          : dictionnaire d'état du bot
      - price          : prix actuel du BASE/QUOTE
      - quote_balance  : solde réel en QUOTE (ex: USDC), déjà récupéré par l'appelant
      - base_balance   : solde réel en BASE (ex: INJ), déjà récupéré par l'appelant
      - update_peak    : si True, met à jour state["wallet_peak"] si un nouveau
                          sommet est atteint (effet de bord explicite et optionnel)

    Retourne un dictionnaire conforme au contrat RN-019 (clés historiques
    conservées pour compatibilité avec CapitalViewBuilder, get_balances, etc.)
    """
    # ---- Invariants RN-019 ----
    allocated_capital = state.get("allocated_capital", 0.0)
    wallet_real = quote_balance + base_balance * price
    alpha = wallet_real - allocated_capital
    capital_for_grid = min(wallet_real, allocated_capital)
    # ---------------------------

    # Données FIFO / PnL (historique, via inventory_manager)
    inventory_qty = inv_mgr.inventory_qty(state)
    inventory_cost = inv_mgr.inventory_cost(state)
    inventory_value = inv_mgr.inventory_value(state, price)
    unrealized_pnl = inv_mgr.inventory_unrealized_pnl(state, price)
    total_pnl = state.get("total_pnl", 0.0)

    # Wallet peak (mémoire) — mise à jour uniquement si explicitement demandée
    wallet_peak = state.get("wallet_peak", 0.0)
    if update_peak and (wallet_peak == 0.0 or wallet_real > wallet_peak):
        state["wallet_peak"] = wallet_real
        wallet_peak = wallet_real

    # Indicateurs dérivés
    drawdown = max(0.0, min(1.0, 1.0 - wallet_real / wallet_peak)) if wallet_peak > 0 else 0.0
    pnl_pct = (wallet_real - allocated_capital) / allocated_capital if allocated_capital > 0 else 0.0

    return {
        # Nouvelles clés (RN-019 / RN-020)
        "wallet_real": wallet_real,
        "allocated_capital": allocated_capital,
        "alpha": alpha,
        "capital_for_grid": capital_for_grid,
        "quote_balance": quote_balance,
        "base_quantity": base_balance,

        # Clés héritées (conservées pour compatibilité ascendante)
        "capital_usdc": allocated_capital,        # ancien nom
        "total_wallet": wallet_real,              # ancien nom
        "quote_available": quote_balance,         # ancien nom → aligné sur le solde réel
        "base_available": base_balance,           # ancien nom → aligné sur la quantité réelle
        "inventory_qty": inventory_qty,
        "inventory_cost": inventory_cost,
        "inventory_value": inventory_value,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "wallet_peak": wallet_peak,
        "drawdown": drawdown,
        "pnl_pct": pnl_pct,
    }


@dataclass(frozen=True)
class CapitalView:
    timestamp: float
    symbol: str

    wallet_balance: float
    reference_budget: float          # allocated_capital (anciennement capital_usdc)
    alpha: float                     # wallet_balance - reference_budget (performance)
    grid_budget: float

    strategic_budget: float

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

        active_capital_ratio = state.get("ACTIVE_CAPITAL_RATIO", 0.9)
        # Étape 9 (suppression de l'intégration CapitalTargetController) :
        # allocated_capital reflète déjà les corrections du MetaController
        # via CapitalTransitionGuard (META_CORRECTION) ; aucun facteur de
        # ratio supplémentaire n'est plus calculé ici (cf. audit étape 7,
        # désactivation étape 8).
        strategic_budget = grid_budget * active_capital_ratio
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
            strategic_budget=strategic_budget,
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
