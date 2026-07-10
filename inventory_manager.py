# inventory_manager.py
# Module centralisé de gestion de l'inventaire pour le bot Gate.io V106
# Source de vérité : inventory_lots
# Compatibilité maintenue avec total_base_qty (utilisé uniquement pour la compatibilité)

import time
import logging
from typing import Dict, Any, List, Optional, Tuple

INVENTORY_EPSILON = 1e-8

logger = logging.getLogger(__name__)

# ─── FONCTIONS D'ACCÈS ──────────────────────────────────────

def inventory_qty(state: Dict[str, Any]) -> float:
    """
    Retourne la quantité totale de tokens à partir des lots.
    C'est la source de vérité.
    """
    lots = state.get("inventory_lots", [])
    return sum(float(lot.get("qty", 0.0)) for lot in lots)

def inventory_cost(state: Dict[str, Any]) -> float:
    """
    Retourne le coût total des lots.
    Si inventory_cost est présent et à jour, on le retourne.
    Sinon, on le recalculé (fallback).
    """
    # Si le state a un inventory_cost fiable, on l'utilise
    cost = state.get("inventory_cost", 0.0)
    
    if "inventory_cost" in state:
        return float(state["inventory_cost"])

    # Fallback
    lots = state.get("inventory_lots", [])
    return sum(
        float(lot.get("qty", 0.0)) * float(lot.get("buy_price", 0.0))
        for lot in lots
    )

def inventory_value(state: Dict[str, Any], price: float) -> float:
    """Retourne la valeur marchande actuelle de l'inventaire."""
    return inventory_qty(state) * price
    
def inventory_unrealized_pnl(state, price):
    return inventory_value(state, price) - inventory_cost(state)

# ─── OPÉRATIONS SUR L'INVENTAIRE ──────────────────────────

"""
Fonction unique d'ajout d'un lot.

Toute création de lot dans le projet doit passer par cette fonction.
Elle garantit la cohérence entre :
- inventory_lots
- inventory_cost
- total_base_qty (compatibilité)
"""

def add_buy_lot(
    state: Dict[str, Any],
    qty: float,
    price: float,
    source: str = "buy",
    reconciled: bool = False,
):
    """
    Ajoute un lot d'achat et met à jour inventory_cost immédiatement.
    Met également à jour total_base_qty pour compatibilité.
    """
    if qty <= 0:
        return
    lot = {
        "qty": qty,
        "buy_price": price,
        "source": source,
        "timestamp": time.time(),
        "reconciled": reconciled,
    }
    state.setdefault("inventory_lots", []).append(lot)
    # Mise à jour du coût total
    current_cost = state.get("inventory_cost", 0.0)
    state["inventory_cost"] = current_cost + qty * price
    # Mise à jour de total_base_qty pour compatibilité (obsolète)
    state["total_base_qty"] = inventory_qty(state)

def consume_fifo(state: Dict[str, Any], qty: float) -> List[Dict[str, float]]:
    """
    Retire les lots selon la méthode FIFO.
    Met à jour inventory_cost en soustrayant le coût des lots consommés.
    Retourne la liste des lots consommés (avec qty et buy_price) pour le calcul du PnL.
    """
    if qty <= 0:
        return []
    remaining = qty
    consumed = []
    new_lots = []
    removed_cost = 0.0
    lots = state.get("inventory_lots", [])

    for lot in lots:
        if remaining <= 0:
            new_lots.append(lot)
            continue
        if lot["qty"] <= remaining:
            consumed.append({"qty": lot["qty"], "buy_price": lot["buy_price"]})
            removed_cost += lot["qty"] * lot["buy_price"]
            remaining -= lot["qty"]
        else:
            consumed.append({"qty": remaining, "buy_price": lot["buy_price"]})
            removed_cost += remaining * lot["buy_price"]
            lot["qty"] -= remaining
            new_lots.append(lot)
            remaining = 0

    if remaining > INVENTORY_EPSILON:
        logger.warning(f"⚠️ Quantité insuffisante dans l'inventaire : {remaining:.6f} tokens manquants")

    state["inventory_lots"] = new_lots
    # Mise à jour du coût total
    current_cost = state.get("inventory_cost", 0.0)
    state["inventory_cost"] = max(0.0, current_cost - removed_cost)
    # Mise à jour de total_base_qty pour compatibilité
    state["total_base_qty"] = inventory_qty(state)

    return consumed

# ─── RÉCONCILIATION ET INITIALISATION ──────────────────────

def ensure_initial_inventory(
    state: Dict[str, Any],
    real_balance: float,
    acquisition_price: float,
    source: str = "snapshot_t0",
) -> bool:
    """
    Si inventory_lots est vide mais que le solde réel est positif,
    crée automatiquement un lot initial avec source="snapshot_t0".
    Retourne True si un lot a été créé, False sinon.
    """
    if state.get("inventory_lots"):
        return False
    if real_balance <= 0:
        return False

    if acquisition_price <= 0:
        logger.warning(
            "⚠️ Impossible de créer le lot initial : acquisition_price invalide."
        )
        return False

    lot = {
        "qty": real_balance,
        "buy_price": acquisition_price,
        "source": source,
        "reconciled": True,
        "timestamp": time.time()
    }
    state["inventory_lots"] = [lot]
    state["inventory_cost"] = real_balance * acquisition_price
    state["total_base_qty"] = real_balance
    logger.info(
        f"🌟 Lot initial créé : "
        f"qty={real_balance:.6f}, "
        f"price={acquisition_price:.4f}, "
        f"source={source}"
    )
    return True

def reconcile(
    state: Dict[str, Any],
    real_balance: float,
    acquisition_price: float,
    source: str = "reconcile",
) -> bool:
    """
    Réconcilie inventory_lots avec le solde réel de l'exchange.
    Utilise la même priorité de prix que ensure_initial_inventory.
    Retourne True si une réconciliation a eu lieu, False sinon.
    """
    current_qty = inventory_qty(state)
    delta = real_balance - current_qty

    if abs(delta) < INVENTORY_EPSILON:
        logger.debug("Inventaire cohérent : pas de réconciliation nécessaire")
        return False

    logger.warning(f"⚠️ Réconciliation : delta={delta:.6f} (réel={real_balance:.6f}, lots={current_qty:.6f})")

    if delta > 0:
        add_buy_lot(
            state,
            qty=delta,
            price=acquisition_price,
            source=source,
            reconciled=True,
        )

        logger.info(
            f"➕ Réconciliation : ajout de {delta:.6f} tokens "
            f"à {acquisition_price:.4f}"
        )
    
    else:
        consumed = consume_fifo(
            state,
            qty=-delta,
        )

        removed_qty = sum(lot["qty"] for lot in consumed)
        removed_cost = sum(
            lot["qty"] * lot["buy_price"]
            for lot in consumed
        )

        logger.info(
            f"✂️ Réconciliation : retrait "
            f"{removed_qty:.6f} tokens "
            f"(coût={removed_cost:.4f})"
        )
    
    state["total_base_qty"] = inventory_qty(state)
    return True

# ─── VÉRIFICATIONS ──────────────────────────────────────────

def verify_invariant(state: Dict[str, Any], real_balance: float,
                     qty_precision: float = INVENTORY_EPSILON) -> bool:
    """
    Vérifie que la quantité totale des lots est égale au solde réel,
    avec une tolérance basée sur la précision de quantité de l'exchange.
    """
    qty = inventory_qty(state)
    return abs(qty - real_balance) <= qty_precision

def verify_inventory_cost(
    state: Dict[str, Any],
    tolerance: float = INVENTORY_EPSILON,
) -> Tuple[bool, float, float]:
    """
    Vérifie que state["inventory_cost"] est cohérent avec la somme des coûts des lots.

    Retourne :
        (is_valid, computed_cost, diff)

    où :
        - is_valid : booléen indiquant si inventory_cost est cohérent.
        - computed_cost : coût recalculé à partir des inventory_lots.
        - diff : écart absolu entre la valeur stockée et la valeur recalculée.

    Cette fonction est destinée au chargement initial ou au débogage.
    """
    lots = state.get("inventory_lots", [])
    computed_cost = sum(float(lot.get("qty", 0.0)) * float(lot.get("buy_price", 0.0)) for lot in lots)
    current_cost = state.get("inventory_cost", 0.0)
    diff = abs(current_cost - computed_cost)
    if diff > tolerance:
        logger.warning(f"⚠️ inventory_cost incohérent : actuel={current_cost:.8f}, recalculé={computed_cost:.8f}, écart={diff:.8f}")
        return False, computed_cost, diff
    return True, computed_cost, diff

def resync_inventory_cost(state: Dict[str, Any]) -> None:
    """
    Resynchronise inventory_cost avec la somme des coûts des lots.
    Utilisé uniquement en cas de détection d'incohérence (ex: chargement, debug).
    """
    lots = state.get("inventory_lots", [])
    computed_cost = sum(float(lot.get("qty", 0.0)) * float(lot.get("buy_price", 0.0)) for lot in lots)
    state["inventory_cost"] = computed_cost
    logger.debug(f"Inventory_cost resynchronisé à {computed_cost:.8f}")

# ─── UTILITAIRES ────────────────────────────────────────────

def get_lots(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Retourne la liste des lots."""
    return state.get("inventory_lots", [])

def get_consumed_lots(lots_consumed: List[Dict[str, float]]) -> float:
    """Retourne la quantité totale consommée à partir de la liste retournée par consume_fifo."""
    return sum(lot["qty"] for lot in lots_consumed)
