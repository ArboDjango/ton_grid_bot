"""Réconciliation idempotente des ordres normalisés d'un exchange.

Le seul contrat accepté est celui de ``ExchangeBase.get_open_orders`` :
``order_id``, ``side``, ``orig_qty``, ``executed_qty``, ``price`` et ``status``.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Optional

import inventory_manager as inv_mgr


_REQUIRED_FIELDS = frozenset({"order_id", "side", "orig_qty", "executed_qty", "price", "status"})
_EPSILON = 1e-8


def reconcile_open_orders(
    state: dict[str, Any],
    orders: Iterable[dict[str, Any]],
    *,
    buy_side: str,
    sell_side: str,
    cancel_order: Callable[[str | int], None],
    persist_state: Optional[Callable[[], None]] = None,
    now: Callable[[], float] = time.time,
) -> dict[str, int]:
    """Applique une seule fois chaque delta d'exécution d'un ordre ouvert.

    ``state['reconciled_orders']`` est persistant et mémorise, par ``order_id``,
    la quantité cumulée déjà appliquée à l'inventaire. Un redémarrage ou une
    nouvelle lecture du même ordre ne peut donc pas ajouter/retirer à nouveau
    les mêmes unités. Les statuts terminaux sans exécution sont également
    mémorisés.
    """
    ledger = state.setdefault("reconciled_orders", {})
    if not isinstance(ledger, dict):
        raise ValueError("reconciled_orders doit être un dictionnaire")

    summary = {"orders": 0, "deltas_applied": 0, "cancelled": 0, "cancel_failures": 0}
    normalized_buy = buy_side.upper()
    normalized_sell = sell_side.upper()

    for order in orders:
        missing = _REQUIRED_FIELDS.difference(order)
        if missing:
            raise ValueError(f"DTO ordre incomplet, champs manquants: {sorted(missing)}")

        order_id = str(order["order_id"])
        side = str(order["side"]).upper()
        if side not in (normalized_buy, normalized_sell):
            raise ValueError(f"Côté d'ordre invalide pour {order_id}: {order['side']!r}")

        executed_qty = float(order["executed_qty"])
        price = float(order["price"])
        if executed_qty < 0 or price < 0:
            raise ValueError(f"Quantité ou prix négatif pour l'ordre {order_id}")

        previous = ledger.get(order_id, {})
        already_applied = float(previous.get("executed_qty", 0.0))
        if executed_qty + _EPSILON < already_applied:
            raise ValueError(
                f"Régression d'exécution pour {order_id}: "
                f"{executed_qty} < {already_applied} déjà réconcilié"
            )

        delta = max(0.0, executed_qty - already_applied)
        if delta > _EPSILON:
            if side == normalized_buy:
                inv_mgr.add_buy_lot(
                    state,
                    qty=delta,
                    price=price,
                    source="open_order_reconcile",
                    reconciled=True,
                )
            else:
                available = inv_mgr.inventory_qty(state)
                if available + _EPSILON < delta:
                    raise RuntimeError(
                        f"Inventaire insuffisant pour réconcilier SELL {order_id}: "
                        f"disponible={available:.8f}, delta={delta:.8f}"
                    )
                consumed = inv_mgr.consume_fifo(state, delta)
                consumed_qty = sum(lot["qty"] for lot in consumed)
                if abs(consumed_qty - delta) > _EPSILON:
                    raise RuntimeError(
                        f"FIFO incomplet pour SELL {order_id}: "
                        f"consommé={consumed_qty:.8f}, attendu={delta:.8f}"
                    )
            summary["deltas_applied"] += 1

        # Ecrire aussi les ordres sans exécution : ils restent idempotents après
        # un redémarrage et documentent l'état terminal observé.
        ledger[order_id] = {
            "side": side,
            "executed_qty": executed_qty,
            "status": str(order["status"]),
            "reconciled_at": now(),
        }
        # Persister avant l'annulation : une panne entre l'application du delta
        # et le prochain démarrage ne peut pas conduire à un second passage.
        if persist_state is not None:
            persist_state()
        summary["orders"] += 1

        # La politique historique est conservée : les ordres ouverts sont annulés
        # après prise en compte de leur exécution cumulée.
        try:
            cancel_order(order["order_id"])
            summary["cancelled"] += 1
        except Exception:
            # Le ledger a déjà été mis à jour. L'annulation sera retentée au
            # prochain cycle sans jamais réappliquer le même delta.
            summary["cancel_failures"] += 1

    return summary
