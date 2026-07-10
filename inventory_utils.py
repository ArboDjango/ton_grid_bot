# inventory_utils.py
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

INVENTORY_COST_TOLERANCE = 1e-8  # seuil de tolérance en USDC

def verify_and_resync_inventory_cost(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Vérifie que state['inventory_cost'] est bien égal à la somme des coûts
    des lots dans state['inventory_lots'].
    Si un écart supérieur à INVENTORY_COST_TOLERANCE est détecté :
      - avertissement dans les logs
      - resynchronisation automatique de inventory_cost
    Retourne le state modifié (ou inchangé).
    """
            
    inventory_lots: List[Dict[str, float]] = state.get("inventory_lots", [])
    
    corrected = False

    # Migration des anciens lots
    for lot in state.get("inventory_lots", []):
        if "source" not in lot:
            lot["source"] = "legacy"
            corrected = True

        if "reconciled" not in lot:
            lot["reconciled"] = False
            corrected = True
            
    if corrected:
        logger.info("🛠️ Migration des anciens inventory_lots effectuée")
    
    # Calcul du coût attendu
    expected_cost = 0.0
    for lot in inventory_lots:
        qty = float(lot.get("qty", 0.0))
        price = float(lot.get("buy_price", 0.0))
        expected_cost += qty * price

    current_cost = float(state.get("inventory_cost", 0.0))
    diff = abs(current_cost - expected_cost)
    
    corrected = False

    if diff > INVENTORY_COST_TOLERANCE:
        logger.warning(
            f"Inventory_cost incohérent : actuel={current_cost:.8f}, "
            f"recalculé={expected_cost:.8f}, écart={diff:.8f} → resynchronisation"
        )
        state["inventory_cost"] = expected_cost
        corrected = True
    
    elif diff > 0:
        logger.debug(
            f"Inventory_cost diff={diff:.12f} (sous la tolérance)"
        )
            
            
    return state, corrected
