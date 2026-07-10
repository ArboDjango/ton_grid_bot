"""
feature_engine.py

Module de calcul de métriques dérivées pour le Meta-Controller.
Couche Feature de l'architecture, purement fonctionnelle et sans état.

Toutes les fonctions sont déterministes, sans effets de bord,
sans logging, sans accès disque, sans réseau.

Chaque résultat inclut un champ `value` (la métrique principale),
un booléen `valid` et une chaîne `reason` pour expliciter
les éventuelles invalidités.

Les classes de résultat sont conçues pour être facilement étendues
avec de nouvelles métriques.
"""

# ------------------------------------------------------------------
# Beta 0.1
# Tant que le DerivativeEngine ne fournit pas l'ATR,
# on neutralise la feature VolatilityFit.
# ------------------------------------------------------------------

BETA_NEUTRAL_VOLATILITY = True

from dataclasses import dataclass
from typing import Optional


# =============================================================================
# Résultats (inchangés)
# =============================================================================


@dataclass
class HeadroomResult:
    """
    Résultat du calcul de headroom.

    Attributes:
        value: Métrique principale (headroom).
        headroom: Capacité résiduelle normalisée (0 = saturé, 1 = aucune allocation).
        saturated: Indique si le capital courant atteint ou dépasse l'idéal.
        ratio: Ratio capital_courant / capital_idéal.
        valid: True si le calcul est valide.
        reason: Explication de l'état de validité.
    """
    value: Optional[float]
    headroom: Optional[float]
    saturated: bool
    ratio: Optional[float]
    valid: bool
    reason: str


@dataclass
class TradeEfficiencyResult:
    """
    Résultat du calcul d'efficacité des trades.

    Attributes:
        value: Métrique principale (normalized_trn).
        trn: Taux de trade par unité de capital (trade_rate / capital).
        normalized_trn: Version normalisée entre 0 et 1 (via sigmoïde logistique).
        valid: True si le calcul est valide.
        reason: Explication de l'état de validité.
    """
    value: Optional[float]
    trn: Optional[float]
    normalized_trn: Optional[float]
    valid: bool
    reason: str


@dataclass
class VolatilityFitResult:
    """
    Résultat du calcul d'adéquation de la volatilité.

    Attributes:
        value: Métrique principale (fit).
        rpg: Ratio de volatilité par grille (ATR / (Gv / active_levels)).
        rpg_max: Seuil maximal pour rpg (ici fixé à 1.0).
        fit: Indice d'adéquation (min(rpg / rpg_max, 1)).
        valid: True si le calcul est valide.
        reason: Explication de l'état de validité.
    """
    value: Optional[float]
    rpg: Optional[float]
    rpg_max: Optional[float]
    fit: Optional[float]
    valid: bool
    reason: str


@dataclass
class RiskPenaltyResult:
    """
    Résultat du calcul de pénalité de risque.

    Attributes:
        value: Métrique principale (penalty).
        penalty: Pénalité brute = drawdown * (1 + |inventory_skew|).
        risk_score: Pénalité normalisée entre 0 et 1 (penalty / (1 + penalty)).
        valid: True si le calcul est valide.
        reason: Explication de l'état de validité.
    """
    value: Optional[float]
    penalty: Optional[float]
    risk_score: Optional[float]
    valid: bool
    reason: str


@dataclass
class InventoryBalanceResult:
    """
    Résultat du calcul de balance d'inventaire.

    Attributes:
        value: Métrique principale (balance_score).
        skew: Déséquilibre entre inventaire et cash : (inventory - cash) / (inventory + cash).
        balance_score: Score de balance = 1 - |skew| (1 = parfait équilibre, 0 = extrême déséquilibre).
        valid: True si le calcul est valide.
        reason: Explication de l'état de validité.
    """
    value: Optional[float]
    skew: Optional[float]
    balance_score: Optional[float]
    valid: bool
    reason: str


# =============================================================================
# Feature Engine (inchangé)
# =============================================================================


# =============================================================================
# NOUVEAUTÉ : Agrégation en FeatureSet
# =============================================================================


@dataclass(frozen=True)
class FeatureSet:
    """
    Ensemble complet des métriques dérivées pour une stratégie.

    Contient les cinq résultats produits par le Feature Engine.
    """
    headroom: HeadroomResult
    trade_efficiency: TradeEfficiencyResult
    volatility_fit: VolatilityFitResult
    risk_penalty: RiskPenaltyResult
    inventory_balance: InventoryBalanceResult



class FeatureEngine:
    """
    Moteur de calcul des métriques dérivées.

    Toutes les méthodes sont statiques et pures.
    """

    @staticmethod
    def compute_headroom(capital_current: float, capital_ideal: float) -> HeadroomResult:
        """
        Calcule le headroom, la saturation et le ratio courant/idéal.

        Args:
            capital_current: Capital actuellement alloué (>= 0).
            capital_ideal: Capital idéal souhaité (> 0).

        Returns:
            HeadroomResult avec les métriques calculées.
        """
        if capital_ideal <= 0:
            return HeadroomResult(
                value=None,
                headroom=None,
                saturated=False,
                ratio=None,
                valid=False,
                reason="capital_ideal must be strictly positive"
            )
        if capital_current < 0:
            return HeadroomResult(
                value=None,
                headroom=None,
                saturated=False,
                ratio=None,
                valid=False,
                reason="capital_current cannot be negative"
            )

        ratio = capital_current / capital_ideal
        saturated = capital_current >= capital_ideal
        headroom = max(0.0, 1.0 - ratio)

        return HeadroomResult(
            value=headroom,
            headroom=headroom,
            saturated=saturated,
            ratio=ratio,
            valid=True,
            reason="OK"
        )

    @staticmethod
    def compute_trade_efficiency(trade_rate: float, capital: float) -> TradeEfficiencyResult:
        """
        Calcule le taux de trade normalisé par unité de capital.

        Args:
            trade_rate: Nombre de trades (ou fréquence) sur une période.
            capital: Capital alloué (> 0).

        Returns:
            TradeEfficiencyResult avec trn et normalized_trn.
        """
        if capital <= 0:
            return TradeEfficiencyResult(
                value=None,
                trn=None,
                normalized_trn=None,
                valid=False,
                reason="capital must be strictly positive"
            )
        if trade_rate < 0:
            return TradeEfficiencyResult(
                value=None,
                trn=None,
                normalized_trn=None,
                valid=False,
                reason="trade_rate cannot be negative"
            )

        trn = trade_rate / capital
        normalized_trn = trn / (1.0 + trn)

        return TradeEfficiencyResult(
            value=normalized_trn,
            trn=trn,
            normalized_trn=normalized_trn,
            valid=True,
            reason="OK"
        )

    @staticmethod
    def compute_volatility_fit(atr: float, gv: float, active_levels: int) -> VolatilityFitResult:
        """
        Évalue l'adéquation de la volatilité par rapport à la grille de trading.

        Args:
            atr: Average True Range (volatilité absolue), doit être >= 0.
            gv: Capital total alloué à la stratégie (Grid Value), doit être > 0.
            active_levels: Nombre de niveaux de grille actifs, doit être > 0.

        Returns:
            VolatilityFitResult avec rpg, rpg_max (fixé à 1.0) et fit.
        """
        if gv <= 0:
            return VolatilityFitResult(
                value=None,
                rpg=None,
                rpg_max=None,
                fit=None,
                valid=False,
                reason="gv must be strictly positive"
            )
        if active_levels <= 0:
            return VolatilityFitResult(
                value=None,
                rpg=None,
                rpg_max=None,
                fit=None,
                valid=False,
                reason="active_levels must be positive"
            )
        if atr < 0:
            return VolatilityFitResult(
                value=None,
                rpg=None,
                rpg_max=None,
                fit=None,
                valid=False,
                reason="atr cannot be negative"
            )

        grid_spacing = gv / active_levels
        rpg = atr / grid_spacing
        rpg_max = 1.0
        fit = min(rpg / rpg_max, 1.0) if rpg_max > 0 else 1.0

        return VolatilityFitResult(
            value=fit,
            rpg=rpg,
            rpg_max=rpg_max,
            fit=fit,
            valid=True,
            reason="OK"
        )

    @staticmethod
    def compute_risk_penalty(drawdown: float, inventory_skew: float) -> RiskPenaltyResult:
        """
        Calcule une pénalité de risque basée sur le drawdown et le skew d'inventaire.

        Args:
            drawdown: Perte maximale relative (0..1).
            inventory_skew: Déséquilibre d'inventaire (peut être négatif).

        Returns:
            RiskPenaltyResult avec penalty brute et risk_score normalisé.
        """
        if not (0.0 <= drawdown <= 1.0):
            return RiskPenaltyResult(
                value=None,
                penalty=None,
                risk_score=None,
                valid=False,
                reason="drawdown must be in [0, 1]"
            )
        penalty = drawdown * (1.0 + abs(inventory_skew))
        risk_score = penalty / (1.0 + penalty)

        return RiskPenaltyResult(
            value=penalty,
            penalty=penalty,
            risk_score=risk_score,
            valid=True,
            reason="OK"
        )

    @staticmethod
    def compute_inventory_balance(inventory_value: float, cash_value: float) -> InventoryBalanceResult:
        """
        Calcule l'équilibre entre la valeur de l'inventaire et le cash.

        Args:
            inventory_value: Valeur totale de l'inventaire (>= 0).
            cash_value: Montant de cash disponible (>= 0).

        Returns:
            InventoryBalanceResult avec skew et balance_score.
        """
        if inventory_value < 0 or cash_value < 0:
            return InventoryBalanceResult(
                value=None,
                skew=None,
                balance_score=None,
                valid=False,
                reason="inventory_value and cash_value must be non-negative"
            )
        total = inventory_value + cash_value
        if total == 0:
            return InventoryBalanceResult(
                value=None,
                skew=None,
                balance_score=None,
                valid=False,
                reason="total value (inventory + cash) is zero"
            )

        skew = (inventory_value - cash_value) / total
        balance_score = 1.0 - abs(skew)

        return InventoryBalanceResult(
            value=balance_score,
            skew=skew,
            balance_score=balance_score,
            valid=True,
            reason="OK"
        )

    @staticmethod
    def compute_all(observation: dict) -> FeatureSet:
        """
        Calcule toutes les features d'une stratégie.
        """

        diagnostics = observation["diagnostics"]
        trading = observation["trading"]

        capital = observation["capital_usdc"]
        wallet = observation["wallet"]

        inventory_value = observation["inventory_value"]
        cash_value = max(0.0, wallet - inventory_value)

        # ------------------------------------------------------------
        # Beta 0.1
        # En attendant que le DerivativeEngine fournisse un ATR,
        # on neutralise complètement cette feature.
        # ------------------------------------------------------------

        if BETA_NEUTRAL_VOLATILITY:

            volatility_fit = VolatilityFitResult(
                value=1.0,
                rpg=1.0,
                rpg_max=1.0,
                fit=1.0,
                valid=True,
                reason="Beta 0.1 - Neutral volatility",
            )

        else:

            volatility_fit = FeatureEngine.compute_volatility_fit(
                atr=0.0,      # TODO : remplacé par le DerivativeEngine
                gv=diagnostics["gv"],
                active_levels=diagnostics["nb_levels"],
            )

        return FeatureSet(

            headroom=FeatureEngine.compute_headroom(
                capital,
                wallet,
            ),

            trade_efficiency=FeatureEngine.compute_trade_efficiency(
                trade_rate=trading["total_trades"],
                capital=capital,
            ),

            volatility_fit=volatility_fit,

            risk_penalty=FeatureEngine.compute_risk_penalty(
                drawdown=observation["drawdown_pct"],
                inventory_skew=0.0,      # TODO : calcul réel plus tard
            ),

            inventory_balance=FeatureEngine.compute_inventory_balance(
                inventory_value=inventory_value,
                cash_value=cash_value,
            ),
        )


