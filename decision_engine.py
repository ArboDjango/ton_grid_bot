"""
decision_engine.py

Moteur de décision du Meta-Controller.

Ce module est responsable de la transformation des opportunités détectées
(GOI) en une demande de capital pour une stratégie donnée.

Il agit au niveau individuel (stratégie par stratégie) et ne connaît pas
les contraintes globales du portefeuille (cash disponible, autres stratégies).

Toutes les constantes sont regroupées dans une classe de configuration
pour faciliter les évolutions futures.

Le moteur est pur, déterministe, ne lève aucune exception et signale
les cas invalides via les champs `valid` et `reason`.
"""

from dataclasses import dataclass, field
from typing import Optional

# Import des types utilisés (HeadroomResult du feature_engine)
from feature_engine import HeadroomResult


@dataclass(frozen=True)
class DecisionConfig:
    """
    Configuration du moteur de décision.

    Attributes:
        goi_gain: Facteur d'amplification de l'opportunité (GOI) sur le capital.
        headroom_gain: Facteur d'amplification du headroom (réserve disponible).
        confidence_weight: Poids de la confiance dans la prise de décision.
        min_desired_factor: Facteur minimal pour le capital désiré (par rapport au wallet).
        max_desired_factor: Facteur maximal pour le capital désiré (par rapport au wallet).
        default_confidence: Valeur de confiance par défaut si non fournie.
    """
    goi_gain: float = 1.0
    headroom_gain: float = 1.0
    confidence_weight: float = 1.0
    min_desired_factor: float = 0.5
    max_desired_factor: float = 2.0
    default_confidence: float = 0.5


@dataclass(frozen=True)
class DecisionInput:
    """
    Entrées nécessaires au calcul de la décision.

    Attributes:
        current_capital: Capital actuellement alloué à la stratégie (>= 0).
        wallet: Capital total disponible pour cette stratégie (>= 0).
        goi: Grid Opportunity Index, dans [0, 1].
        headroom: Résultat du calcul du headroom (optionnel).
        confidence: Niveau de confiance dans les mesures (0..1, optionnel).
    """
    current_capital: float
    wallet: float
    goi: float
    headroom: Optional[HeadroomResult] = None
    confidence: Optional[float] = None


@dataclass(frozen=True)
class DecisionResult:
    """
    Résultat de la décision d'allocation.

    Attributes:
        desired_capital: Capital idéal demandé.
        delta: Variation par rapport au capital actuel (desired - current).
        reason: Explication de la décision ou de l'invalidité.
        confidence: Confiance dans la décision (0..1).
        valid: True si la décision est fondée sur des données valides.
    """
    desired_capital: Optional[float]
    delta: Optional[float]
    reason: str
    confidence: float
    valid: bool


class DecisionEngine:
    """
    Moteur de décision pour le capital idéal d'une stratégie.

    Seule méthode publique : compute().
    """

    @staticmethod
    def compute(
        decision_input: DecisionInput,
        config: Optional[DecisionConfig] = None,
    ) -> DecisionResult:
        """
        Calcule le capital désiré pour une stratégie.

        La formule utilisée (V1) est :

            desired = wallet + goi_gain * GOI * headroom * wallet * confidence

        avec un clamp optionnel sur [wallet * min_factor, wallet * max_factor].

        Args:
            decision_input: Entrées de la décision.
            config: Configuration du moteur (utilise les valeurs par défaut si None).

        Returns:
            DecisionResult contenant le capital désiré, le delta,
            la confiance, la validité et la raison.
        """
        # Configuration par défaut
        if config is None:
            config = DecisionConfig()

        # Validation des entrées
        if decision_input.current_capital < 0:
            return DecisionResult(
                desired_capital=None,
                delta=None,
                reason="current_capital cannot be negative",
                confidence=0.0,
                valid=False,
            )
        if decision_input.wallet <= 0:
            return DecisionResult(
                desired_capital=None,
                delta=None,
                reason="wallet must be strictly positive",
                confidence=0.0,
                valid=False,
            )
        if not (0.0 <= decision_input.goi <= 1.0):
            return DecisionResult(
                desired_capital=None,
                delta=None,
                reason="goi must be in [0, 1]",
                confidence=0.0,
                valid=False,
            )

        # Confiance
        confidence = decision_input.confidence
        if confidence is None:
            confidence = config.default_confidence
        if not (0.0 <= confidence <= 1.0):
            return DecisionResult(
                desired_capital=None,
                delta=None,
                reason="confidence must be in [0, 1]",
                confidence=0.0,
                valid=False,
            )

        # Headroom
        headroom = decision_input.headroom
        headroom_value = 1.0  # par défaut, si absent
        if headroom is not None:
            if not headroom.valid:
                return DecisionResult(
                    desired_capital=None,
                    delta=None,
                    reason=f"headroom invalide : {headroom.reason}",
                    confidence=0.0,
                    valid=False,
                )
            headroom_value = headroom.headroom
        else:
            # Si headroom absent, on considère qu'il n'y a pas de limitation
            headroom_value = 1.0

        # Formule de base
        # desired = wallet + goi_gain * GOI * headroom * wallet * confidence_weight * confidence
        additional = (
            config.goi_gain
            * config.headroom_gain
            * decision_input.goi
            * headroom_value
            * decision_input.wallet
            * config.confidence_weight
            * confidence
        )
        desired = decision_input.wallet + additional

        # Application des bornes (optionnelles)
        min_desired = decision_input.wallet * config.min_desired_factor
        max_desired = decision_input.wallet * config.max_desired_factor
        if desired < min_desired:
            desired = min_desired
        if desired > max_desired:
            desired = max_desired

        # Calcul du delta
        delta = desired - decision_input.current_capital

        return DecisionResult(
            desired_capital=desired,
            delta=delta,
            reason="OK",
            confidence=confidence,
            valid=True,
        )
