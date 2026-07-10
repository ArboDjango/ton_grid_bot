"""
goi_engine.py

Moteur de calcul du Grid Opportunity Index (GOI).

Le GOI est une estimation de la capacité d'une stratégie à transformer
du capital supplémentaire en création future de valeur.

Le moteur consomme exclusivement les Features produites par feature_engine.py
et n'a aucune connaissance des couches inférieures (bots, exchange, wallet, etc.).

Architecture du pipeline :
    Features → Dimensions normalisées → Weighted Score → Normalisation → GOI

Le Weighted Score est le score linéaire issu de la combinaison pondérée des
quatre dimensions. Le GOI est le score final après normalisation (par défaut
une sigmoïde). Les deux sont conservés dans les composants pour assurer la
traçabilité et faciliter la calibration.

Le moteur est configurable via la constante NORMALIZATION_MODE, qui supporte :
    - "identity" : retourne le Weighted Score tel quel
    - "sigmoid"  : applique une sigmoïde (paramètres SIGMOID_ALPHA, SIGMOID_BETA)
    - "tanh"     : applique une tangente hyperbolique normalisée

Chaque dimension est isolée dans une fonction privée dédiée,
permettant un remplacement futur sans impacter le reste du moteur.

Toutes les méthodes sont statiques et pures.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, Optional
from feature_engine import FeatureSet

# Import des types produits par le Feature Engine
from feature_engine import (
    HeadroomResult,
    TradeEfficiencyResult,
    VolatilityFitResult,
    RiskPenaltyResult,
    InventoryBalanceResult,
)


@dataclass(frozen=True)
class GOIResult:
    """
    Résultat du calcul du Grid Opportunity Index.

    Attributes:
        value: Valeur du GOI final (après normalisation), dans [0, 1].
        confidence: Niveau de confiance dans le calcul. (Non utilisé dans RN-010a/b,
                    sera implémenté dans RN-010c.)
        valid: True si toutes les features nécessaires sont valides.
        reason: Explication de la validité ou de l'invalidité.
        components: Dictionnaire des dimensions intermédiaires pour le
                    diagnostic et la traçabilité. Contient notamment :
                    - opportunity, capacity, efficiency, safety
                    - balance_factor (diagnostic)
                    - weighted_score (score linéaire avant normalisation)
                    - goi (score final après normalisation, identique à value)
    """
    value: Optional[float]
    confidence: Optional[float]  # Toujours None dans RN-010a/b
    valid: bool
    reason: str
    components: Dict[str, float] = field(default_factory=dict)


class GOIEngine:
    """
    Moteur de calcul du GOI.

    Seule méthode publique : compute().
    Toute la logique métier est encapsulée dans des méthodes privées,
    une par dimension.
    """

    # -------------------------------------------------------------------------
    # Identifiant de version du modèle (pour traçabilité historique)
    # -------------------------------------------------------------------------

    GOI_MODEL_VERSION = "GOI-v2"

    # -------------------------------------------------------------------------
    # Poids de la combinaison linéaire (constants de classe)
    # -------------------------------------------------------------------------

    WEIGHT_OPPORTUNITY = 0.35
    WEIGHT_EFFICIENCY  = 0.30
    WEIGHT_CAPACITY    = 0.20
    WEIGHT_RISK        = 0.15  # Bien que nommé RISK, il est associé à la dimension Safety

    # -------------------------------------------------------------------------
    # Paramètres de normalisation
    # -------------------------------------------------------------------------

    # Mode de normalisation : "identity", "sigmoid", "tanh"
    NORMALIZATION_MODE = "sigmoid"

    # Paramètres de la sigmoïde (et de la tanh)
    SIGMOID_ALPHA = 8.0
    SIGMOID_BETA  = 0.55

    # -------------------------------------------------------------------------
    # Dimensions élémentaires
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_opportunity(volatility_fit: VolatilityFitResult) -> float:
        """
        Dimension Opportunité.
        Représente l'adéquation de la volatilité du marché à la grille de la stratégie.
        """
        # Le fit est déjà normalisé dans [0, 1] par le Feature Engine
        return max(0.0, min(1.0, volatility_fit.fit))

    @staticmethod
    def _compute_capacity(headroom: HeadroomResult) -> float:
        """
        Dimension Capacité.
        Représente la marge de manœuvre disponible avant d'atteindre le capital idéal.
        """
        # headroom est déjà dans [0, 1]
        return max(0.0, min(1.0, headroom.headroom))

    @staticmethod
    def _compute_efficiency(trade_efficiency: TradeEfficiencyResult) -> float:
        """
        Dimension Efficacité.
        Représente la capacité de la stratégie à générer des trades
        par unité de capital alloué.
        """
        # normalized_trn est déjà dans (0, 1)
        return max(0.0, min(1.0, trade_efficiency.normalized_trn))

    @staticmethod
    def _compute_safety(risk_penalty: RiskPenaltyResult) -> float:
        """
        Dimension Sécurité (anciennement Risque).
        Représente un score de sécurité où 0 = très risqué, 1 = très sûr.
        Plus le risque initial (risk_score) est élevé, plus ce score est faible.
        """
        # risk_score est dans [0, 1], on le retourne pour que
        # risque élevé -> score faible (safety faible)
        safety = 1.0 - risk_penalty.risk_score
        return max(0.0, min(1.0, safety))

    @staticmethod
    def _compute_balance_factor(inventory_balance: InventoryBalanceResult) -> float:
        """
        Dimension Équilibre (diagnostic).
        Bien que non incluse directement dans le calcul du Weighted Score,
        elle est exposée dans les composants pour le monitoring.
        """
        # balance_score est déjà dans [0, 1]
        return max(0.0, min(1.0, inventory_balance.balance_score))

    @staticmethod
    def _combine_weighted_sum(
        opportunity: float,
        efficiency: float,
        capacity: float,
        safety: float
    ) -> float:
        """
        Combine les quatre dimensions principales en une somme pondérée.

        Les poids sont définis comme constantes de classe pour faciliter
        la calibration ultérieure.

        Cette étape produit le Weighted Score, qui sera ensuite normalisé
        pour obtenir le GOI final.
        """
        return (
            GOIEngine.WEIGHT_OPPORTUNITY * opportunity +
            GOIEngine.WEIGHT_EFFICIENCY  * efficiency +
            GOIEngine.WEIGHT_CAPACITY    * capacity +
            GOIEngine.WEIGHT_RISK        * safety
        )

    @staticmethod
    def _normalize_score(weighted_score: float) -> float:
        """
        Normalise le Weighted Score en fonction du mode configuré.

        Modes supportés :
            - "identity" : retourne le score inchangé
            - "sigmoid"  : applique une sigmoïde paramétrée
            - "tanh"     : applique une tangente hyperbolique normalisée

        Args:
            weighted_score: Score pondéré dans [0, 1].

        Returns:
            Valeur normalisée (dans [0, 1] pour les modes sigmoid et tanh).

        Raises:
            ValueError: si le mode n'est pas reconnu.
        """
        mode = GOIEngine.NORMALIZATION_MODE

        if mode == "identity":
            return weighted_score

        elif mode == "sigmoid":
            alpha = GOIEngine.SIGMOID_ALPHA
            beta  = GOIEngine.SIGMOID_BETA
            return 1.0 / (1.0 + math.exp(-alpha * (weighted_score - beta)))

        elif mode == "tanh":
            alpha = GOIEngine.SIGMOID_ALPHA
            beta  = GOIEngine.SIGMOID_BETA
            # tanh en [-1,1] → mise à l'échelle [0,1]
            return 0.5 * (1.0 + math.tanh(alpha * (weighted_score - beta)))

        else:
            raise ValueError(
                f"Mode de normalisation inconnu : '{mode}'. "
                "Les modes supportés sont : 'identity', 'sigmoid', 'tanh'."
            )

    # -------------------------------------------------------------------------
    # Orchestration
    # -------------------------------------------------------------------------

    @staticmethod
    def compute(
        features: FeatureSet,
    ) -> GOIResult:
        """
        Calcule le GOI à partir des cinq métriques issues du Feature Engine.

        Args:
            features: Ensemble complet des features (headroom, trade_efficiency,
                      volatility_fit, risk_penalty, inventory_balance).

        Returns:
            GOIResult contenant la valeur du GOI final, la validité,
            la raison et les composantes intermédiaires.

        Raises:
            Aucune exception n'est levée. Les cas invalides sont signalés
            via le champ `valid` et la `reason`. La seule exception possible
            est levée par _normalize_score si le mode est invalide (mais ce
            cas est détecté en développement).
        """

        # Validation des entrées : on parcourt toutes les features
        # pour détecter la première invalide.
        feature_dict = {
            "headroom": features.headroom,
            "trade_efficiency": features.trade_efficiency,
            "volatility_fit": features.volatility_fit,
            "risk_penalty": features.risk_penalty,
            "inventory_balance": features.inventory_balance,
        }

        for name, feat in feature_dict.items():
            if not feat.valid:
                return GOIResult(
                    value=None,
                    confidence=None,
                    valid=False,
                    reason=f"{name} invalide : {feat.reason}",
                    components={},
                )

        # Calcul des dimensions
        opportunity = GOIEngine._compute_opportunity(features.volatility_fit)
        capacity = GOIEngine._compute_capacity(features.headroom)
        efficiency = GOIEngine._compute_efficiency(features.trade_efficiency)
        safety = GOIEngine._compute_safety(features.risk_penalty)
        balance_factor = GOIEngine._compute_balance_factor(features.inventory_balance)

        # Combinaison linéaire pondérée des quatre dimensions principales
        weighted_score = GOIEngine._combine_weighted_sum(
            opportunity,
            efficiency,
            capacity,
            safety
        )

        # Clamp de sécurité (la somme pondérée est déjà dans [0,1] si les poids somment à 1)
        weighted_score = max(0.0, min(1.0, weighted_score))

        # Normalisation → GOI final
        goi = GOIEngine._normalize_score(weighted_score)

        # Dictionnaire des composantes (incluant l'équilibre pour le suivi)
        components = {
            "opportunity": opportunity,
            "capacity": capacity,
            "efficiency": efficiency,
            "safety": safety,
            "balance_factor": balance_factor,
            "weighted_score": weighted_score,
            "goi": goi,
        }

        # Affichage debug du calcul intermédiaire
        print("----- GOI DEBUG -----")
        print(f"Opportunity    : {opportunity:.2f}")
        print(f"Capacity       : {capacity:.2f}")
        print(f"Efficiency     : {efficiency:.2f}")
        print(f"Safety         : {safety:.2f}")
        print(f"Weighted Score : {weighted_score:.2f}")
        print(f"Normalization  : {GOIEngine.NORMALIZATION_MODE}")
        print(f"GOI            : {goi:.2f}")

        return GOIResult(
            value=goi,
            confidence=None,  # Pas encore implémenté (RN-010c)
            valid=True,
            reason="GOI computed successfully.",
            components=components,
        )
