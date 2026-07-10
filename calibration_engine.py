"""
calibration_engine.py

Moteur de calibration du GOI.

Ce moteur est indépendant du Meta-Controller et ne participe pas aux décisions
en temps réel. Il évalue a posteriori la qualité des paramètres du GOI en
comparant les scores GOI calculés sur des observations historiques avec les
performances futures réellement observées.

Le but est de fournir des métriques objectives (corrélation, etc.) qui permettront
de choisir les meilleurs paramètres pour le GOI (poids, alpha, beta, mode de
normalisation) sans intervention humaine.

Le moteur est totalement analytique : il ne modifie aucun état, n'envoie aucun
ordre et ne consomme aucune ressource de trading.
"""

import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from goi_engine import GOIEngine, GOIConfig, GOIResult
from feature_engine import FeatureSet


@dataclass(frozen=True)
class Observation:
    """
    Une observation historique utilisée pour la calibration.

    Attributes:
        timestamp: Horodatage de l'observation (peut être un entier ou une chaîne).
        symbol: Symbole de l'actif (ex: "BTC/USDT").
        features: FeatureSet associé à cette observation.
        future_performance: Performance future observée (ex: rendement sur 24h, Sharpe, etc.).
                            Cette valeur doit être quantitative (float).
    """
    timestamp: Any
    symbol: str
    features: FeatureSet
    future_performance: float


@dataclass(frozen=True)
class CalibrationResult:
    """
    Résultat d'une évaluation de calibration.

    Attributes:
        model_version: Version du modèle GOI utilisé.
        normalization_mode: Mode de normalisation.
        weights: Dictionnaire des poids utilisés.
        sigmoid_alpha: Paramètre alpha.
        sigmoid_beta: Paramètre beta.
        samples: Nombre d'observations traitées.
        correlation: Corrélation de Pearson entre GOI et performance future.
        spearman_correlation: Corrélation de Spearman (réservé pour future extension).
        kendall_tau: Kendall Tau (réservé).
        precision_at_k: Precision@K (réservé).
        ndcg: NDCG (réservé).
        hit_ratio: Hit ratio (réservé).
        information_coefficient: IC (réservé).
        rank_ic: Rank IC (réservé).
        mse: Mean Squared Error (réservé).
        stats_by_symbol: Statistiques par actif (réservé).
        score: Score global de qualité (actuellement la corrélation, mais pourra être
               remplacé par une métrique composite plus tard).
        details: Dictionnaire extensible pour stocker des métriques supplémentaires.
    """
    model_version: str
    normalization_mode: str
    weights: Dict[str, float]
    sigmoid_alpha: float
    sigmoid_beta: float
    samples: int
    correlation: Optional[float] = None
    spearman_correlation: Optional[float] = None
    kendall_tau: Optional[float] = None
    precision_at_k: Optional[float] = None
    ndcg: Optional[float] = None
    hit_ratio: Optional[float] = None
    information_coefficient: Optional[float] = None
    rank_ic: Optional[float] = None
    mse: Optional[float] = None
    stats_by_symbol: Optional[Dict[str, Any]] = None
    score: Optional[float] = None
    details: Dict[str, float] = field(default_factory=dict)


class CalibrationEngine:
    """
    Moteur de calibration du GOI.

    Permet d'évaluer la qualité prédictive d'une configuration GOI donnée sur
    un ensemble d'observations historiques.

    Utilisation typique :
        engine = CalibrationEngine()
        config = GOIConfig(weight_opportunity=0.40, ...)
        result = engine.evaluate(observations, config)
        print(result.correlation)
    """

    @staticmethod
    def evaluate(
        observations: List[Observation],
        config: GOIConfig,
    ) -> CalibrationResult:
        """
        Évalue la qualité d'une configuration GOI sur des observations historiques.

        Args:
            observations: Liste d'observations historiques, chacune contenant
                          un FeatureSet et la performance future observée.
            config: Configuration GOI à évaluer.

        Returns:
            CalibrationResult contenant les métriques de qualité (corrélation, etc.).
        """
        if not observations:
            return CalibrationResult(
                model_version=config.model_version,
                normalization_mode=config.normalization_mode,
                weights={
                    "opportunity": config.weight_opportunity,
                    "efficiency": config.weight_efficiency,
                    "capacity": config.weight_capacity,
                    "risk": config.weight_risk,
                },
                sigmoid_alpha=config.sigmoid_alpha,
                sigmoid_beta=config.sigmoid_beta,
                samples=0,
                correlation=None,
                score=None,
                details={"error": "No observations provided"},
            )

        # Calculer le GOI pour chaque observation en utilisant la configuration
        goi_scores = []
        performances = []
        for obs in observations:
            result: GOIResult = GOIEngine.compute(obs.features, config=config)
            if result.valid and result.value is not None:
                goi_scores.append(result.value)
                performances.append(obs.future_performance)

        n = len(goi_scores)
        if n == 0:
            return CalibrationResult(
                model_version=config.model_version,
                normalization_mode=config.normalization_mode,
                weights={
                    "opportunity": config.weight_opportunity,
                    "efficiency": config.weight_efficiency,
                    "capacity": config.weight_capacity,
                    "risk": config.weight_risk,
                },
                sigmoid_alpha=config.sigmoid_alpha,
                sigmoid_beta=config.sigmoid_beta,
                samples=0,
                correlation=None,
                score=None,
                details={"error": "No valid GOI scores could be computed"},
            )

        # Calculer la corrélation de Pearson
        correlation = CalibrationEngine._pearson_correlation(goi_scores, performances)

        # Pour l'instant, le score global est la corrélation (peut être étendu)
        score = correlation

        return CalibrationResult(
            model_version=config.model_version,
            normalization_mode=config.normalization_mode,
            weights={
                "opportunity": config.weight_opportunity,
                "efficiency": config.weight_efficiency,
                "capacity": config.weight_capacity,
                "risk": config.weight_risk,
            },
            sigmoid_alpha=config.sigmoid_alpha,
            sigmoid_beta=config.sigmoid_beta,
            samples=n,
            correlation=correlation,
            score=score,
            details={
                "mean_goi": sum(goi_scores) / n,
                "mean_performance": sum(performances) / n,
            },
        )

    @staticmethod
    def _pearson_correlation(x: List[float], y: List[float]) -> Optional[float]:
        """
        Calcule le coefficient de corrélation de Pearson entre deux listes de nombres.

        Retourne None si les données sont insuffisantes ou si la variance est nulle.
        """
        n = len(x)
        if n != len(y) or n < 2:
            return None

        mean_x = sum(x) / n
        mean_y = sum(y) / n

        diff_x = [v - mean_x for v in x]
        diff_y = [v - mean_y for v in y]

        numerator = sum(dx * dy for dx, dy in zip(diff_x, diff_y))
        denom_x = math.sqrt(sum(dx * dx for dx in diff_x))
        denom_y = math.sqrt(sum(dy * dy for dy in diff_y))

        if denom_x == 0 or denom_y == 0:
            return None

        return numerator / (denom_x * denom_y)

    # -------------------------------------------------------------------------
    # Futures extensions : méthodes pour la recherche de grille, optimisation,
    # métriques avancées (Spearman, Kendall, etc.)
    # -------------------------------------------------------------------------

    # ... (à compléter ultérieurement)
