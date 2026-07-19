"""
virtual_treasury_manager.py

Gestionnaire de trésorerie virtuel (VTM) — Version finale pour tests live.

Ce moteur calcule une allocation recommandée pour chaque stratégie active,
en se basant sur le GOI et le budget actuel de chaque stratégie, ainsi que
sur le solde USDT libre disponible sur le compte Spot.

Le moteur est purement analytique : il ne modifie aucun état, n'envoie aucun
ordre et ne consomme aucune ressource de trading. Il produit uniquement des
recommandations d'allocation.

Le capital total est recalculé dynamiquement à chaque exécution comme :
    capital_total = solde USDT libre + somme des valeurs économiques des inventaires

Les valeurs fournies dans StrategyState ne sont jamais des consignes de
pilotage (allocated_capital). Le solde USDT libre, partagé entre les bots, est
donc compté exactement une fois.

Le moteur est conçu pour fonctionner avec un nombre variable de stratégies.

Architecture du pipeline :
    GOI → (Diminishing Returns) → Budget cible → Contraintes → Lissage → Allocation

La version actuelle (VTM-v1) n'implémente pas encore les rendements marginaux
décroissants (USE_DIMINISHING_RETURNS = False), mais l'architecture est prête.

Le portefeuille cible représente la décision économique du moteur (après bornes).
Le portefeuille recommandé est une version lissée pour éviter les oscillations.
Le lissage est volontairement utilisé afin d'éviter les réallocations permanentes.

Le moteur retourne un VirtualTreasuryResult contenant un résumé global et la liste
des allocations. L'affichage est séparé dans une méthode statique dédiée.
"""

import math
from typing import Any
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum



# -------------------------------------------------------------------------
# Types et structures de données
# -------------------------------------------------------------------------

class AllocationAction(Enum):
    """Action de réallocation recommandée pour une stratégie."""
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    HOLD = "HOLD"


# (Modifications mineures à virtual_treasury_manager.py pour ajouter le champ decision)

# Dans virtual_treasury_manager.py, modifier StrategyState :

@dataclass(frozen=True)
class StrategyState:
    """
    État d'une stratégie active.

    Attributes:
        symbol: Symbole de la stratégie.
        current_budget: Valeur économique actuelle de l'inventaire de la
            stratégie (en USDT), hors cash partagé.
        goi: Grid Opportunity Index calculé pour cette stratégie (dans [0, 1]).
        decision: Décision stratégique (DecisionResult ou autre). Optionnel, peut être None.
    """
    symbol: str
    current_budget: float
    goi: float
    decision: Any = None  # Type Any pour éviter les imports circulaires

# Le reste du fichier virtual_treasury_manager.py reste inchangé.
# La méthode compute ne l'utilise pas encore, mais le champ est présent.


@dataclass(frozen=True)
class AllocationResult:
    """
    Résultat de l'allocation recommandée pour une stratégie.

    Attributes:
        symbol: Symbole de la stratégie.
        goi: GOI utilisé pour le calcul (éventuellement après rendements décroissants).
        current_budget: Budget actuel.
        current_allocation_pct: Pourcentage du capital total actuellement alloué.
        target_budget: Budget cible après application des garde-fous (avant lissage).
        target_allocation_pct: Pourcentage du capital total cible.
        recommended_budget: Budget recommandé après lissage.
        allocation_pct: Pourcentage du capital total recommandé.
        delta: Différence entre le budget recommandé et le budget actuel.
        action: Action de réallocation (INCREASE, DECREASE, HOLD).
        estimated_cycles: Nombre estimé de cycles pour atteindre la cible (arrondi).
    """
    symbol: str
    goi: float
    current_budget: float
    current_allocation_pct: float
    target_budget: float
    target_allocation_pct: float
    recommended_budget: float
    allocation_pct: float
    delta: float
    action: AllocationAction
    estimated_cycles: Optional[int]


@dataclass(frozen=True)
class TreasurySummary:
    """
    Résumé global de la trésorerie virtuelle.

    Attributes:
        capital_total: Capital économique total (USDT libre + inventaires).
        free_usdt: Solde USDT libre sur le compte Spot.
        total_recommended: Somme des budgets recommandés.
        mean_goi: Moyenne des GOI (après rendements décroissants éventuels).
        max_goi: GOI maximal.
        min_goi: GOI minimal.
        number_of_strategies: Nombre de stratégies actives.
        model_version: Version du modèle utilisé (VTM-v1).
        mean_delta: Moyenne des deltas (en valeur absolue ? on va mettre la moyenne des deltas signés).
        max_delta: Delta maximum en valeur absolue.
        total_absolute_delta: Somme des valeurs absolues des deltas.
    """
    capital_total: float
    free_usdt: float
    total_recommended: float
    mean_goi: float
    max_goi: float
    min_goi: float
    number_of_strategies: int
    model_version: str = "VTM-v1"
    mean_delta: float = 0.0
    max_delta: float = 0.0
    total_absolute_delta: float = 0.0


@dataclass(frozen=True)
class VirtualTreasuryResult:
    """
    Résultat complet du VirtualTreasuryManager.

    Attributes:
        summary: Résumé global.
        allocations: Liste des résultats par stratégie.
    """
    summary: TreasurySummary
    allocations: List[AllocationResult]


# -------------------------------------------------------------------------
# Moteur principal
# -------------------------------------------------------------------------

class VirtualTreasuryManager:
    """
    Gestionnaire de trésorerie virtuel.

    Calcule des allocations recommandées à partir des GOI et des budgets actuels.

    Constantes de classe :
        MIN_BUDGET: Budget minimal (USDT) en dessous duquel une stratégie
                    ne peut pas descendre.
        MAX_BUDGET_PCT: Pourcentage maximum du capital total qu'une stratégie
                        peut recevoir.
        SMOOTHING_FACTOR: Facteur de lissage (entre 0 et 1) pour converger
                          progressivement vers la cible.
        MIN_DELTA_ACTION: Seuil (USDT) pour déterminer l'action de réallocation.
                          En dessous de ce seuil, on considère que le budget
                          recommandé est égal au budget actuel (pas de mouvement).
        EPSILON: Tolérance numérique pour les comparaisons de flottants.
        USE_DIMINISHING_RETURNS: Active/désactive la pénalisation des stratégies
                                 déjà fortement financées (préparé pour futur).
    """

    MIN_BUDGET = 50.0
    MAX_BUDGET_PCT = 0.40
    SMOOTHING_FACTOR = 0.20
    MIN_DELTA_ACTION = 10.0
    EPSILON = 1e-9
    USE_DIMINISHING_RETURNS = False  # Réservé pour future évolution

    @staticmethod
    def compute(
        strategies: List[StrategyState],
        free_usdt: float,
    ) -> VirtualTreasuryResult:
        """
        Calcule les allocations recommandées pour un ensemble de stratégies.

        Args:
            strategies: Liste des états des stratégies actives.
            free_usdt: Solde USDT disponible sur le compte Spot.

        Returns:
            VirtualTreasuryResult contenant le résumé et les allocations.

        Raises:
            ValueError: Si les données d'entrée sont invalides (GOI négatif,
                        budget négatif, etc.) ou si la liste est vide.
        """
        # --- Validation des entrées ---
        if not strategies:
            raise ValueError("Aucune stratégie fournie.")

        for s in strategies:
            if s.current_budget < 0:
                raise ValueError(f"Budget négatif pour {s.symbol}: {s.current_budget}")
            if not (0.0 <= s.goi <= 1.0 + VirtualTreasuryManager.EPSILON):
                raise ValueError(f"GOI hors de [0,1] pour {s.symbol}: {s.goi}")

        # Calcul du capital total
        total_current_budget = sum(s.current_budget for s in strategies)
        capital_total = total_current_budget + free_usdt
        if capital_total <= VirtualTreasuryManager.EPSILON:
            raise ValueError("Capital total nul ou négatif.")

        # --- Étape 1 : Application éventuelle des rendements marginaux décroissants ---
        gois = []
        for s in strategies:
            if VirtualTreasuryManager.USE_DIMINISHING_RETURNS:
                goi_adj = VirtualTreasuryManager._apply_diminishing_returns(
                    s.goi, s.current_budget, capital_total
                )
            else:
                goi_adj = s.goi
            gois.append(goi_adj)

        goi_total = sum(gois)
        if goi_total <= VirtualTreasuryManager.EPSILON:
            raise ValueError("GOI total nul ou négatif.")

        # --- Étape 2 : Calcul des budgets cibles bruts (proportionnels) ---
        target_budgets: Dict[str, float] = {}
        for idx, s in enumerate(strategies):
            target = (gois[idx] / goi_total) * capital_total
            target_budgets[s.symbol] = target

        # --- Étape 3 : Application des bornes par algorithme itératif ---
        # On passe les GOI ajustés pour la redistribution
        goi_dict = {s.symbol: g for s, g in zip(strategies, gois)}
        min_budget = VirtualTreasuryManager.MIN_BUDGET
        max_budget = VirtualTreasuryManager.MAX_BUDGET_PCT * capital_total

        clamped = VirtualTreasuryManager._apply_bounds_iterative(
            target_budgets, goi_dict, min_budget, max_budget, capital_total, strategies
        )

        # --- Étape 4 : Lissage avec seuil d'action ---
        # BUGFIX (18/07/2026) : le seuil d'action (deadband) ci-dessous
        # peut briser la conservation du capital total — certaines
        # stratégies sont mises en HOLD (recommended = current) tandis
        # que d'autres appliquent pleinement le lissage, sans qu'aucune
        # étape ne revérifie que la somme retombe sur capital_total.
        # C'est ce qui provoquait l'AssertionError observée en
        # production (remaining_cash < -EPSILON) une fois plusieurs
        # stratégies concernées simultanément par ce seuil.
        raw_recommended_map: Dict[str, float] = {}
        for s in strategies:
            current = s.current_budget
            target = clamped[s.symbol]
            raw_recommended = current + VirtualTreasuryManager.SMOOTHING_FACTOR * (target - current)
            raw_delta = raw_recommended - current
            if abs(raw_delta) < VirtualTreasuryManager.MIN_DELTA_ACTION:
                raw_recommended_map[s.symbol] = current
            else:
                raw_recommended_map[s.symbol] = raw_recommended

        # Réconciliation : on réutilise le même mécanisme itératif que
        # pour les bornes (déjà corrigé ci-dessus) afin de garantir que
        # la somme des budgets recommandés, après application du seuil
        # d'action, retombe exactement sur capital_total — ou échoue
        # explicitement si c'est structurellement impossible.
        reconciled = VirtualTreasuryManager._apply_bounds_iterative(
            raw_recommended_map, goi_dict, min_budget, max_budget, capital_total, strategies
        )

        allocations = []
        deltas = []
        for idx, s in enumerate(strategies):
            current = s.current_budget
            target = clamped[s.symbol]
            recommended = reconciled[s.symbol]
            delta = recommended - current

            if abs(delta) < VirtualTreasuryManager.MIN_DELTA_ACTION:
                action = AllocationAction.HOLD
            elif delta > 0:
                action = AllocationAction.INCREASE
            else:
                action = AllocationAction.DECREASE

            # Calcul des pourcentages
            current_pct = current / capital_total if capital_total > 0 else 0.0
            target_pct = target / capital_total if capital_total > 0 else 0.0
            recommended_pct = recommended / capital_total if capital_total > 0 else 0.0

            # Estimation du nombre de cycles pour atteindre la cible
            if abs(target - current) > VirtualTreasuryManager.EPSILON:
                # On estime le nombre de cycles pour que la distance actuelle se réduise à 5% de la distance initiale
                # Avec un facteur de lissage S, la distance après n cycles est (1-S)^n * distance_initiale
                # On veut (1-S)^n <= 0.05
                # n >= log(0.05) / log(1-S)
                S = VirtualTreasuryManager.SMOOTHING_FACTOR
                if S < 1.0:
                    n = math.ceil(math.log(0.05) / math.log(1.0 - S))
                else:
                    n = 1
                # On vérifie que ça a du sens
                if n > 0:
                    estimated_cycles = n
                else:
                    estimated_cycles = 0
            else:
                estimated_cycles = 0

            allocations.append(AllocationResult(
                symbol=s.symbol,
                goi=gois[idx],
                current_budget=current,
                current_allocation_pct=current_pct,
                target_budget=target,
                target_allocation_pct=target_pct,
                recommended_budget=recommended,
                allocation_pct=recommended_pct,
                delta=delta,
                action=action,
                estimated_cycles=estimated_cycles,
            ))
            deltas.append(delta)
        
        # Les budgets recommandés représentent uniquement le capital investi.
        # Le reste demeure en trésorerie libre.
        sum_recommended = sum(a.recommended_budget for a in allocations)
        remaining_cash = capital_total - sum_recommended

        assert remaining_cash >= -VirtualTreasuryManager.EPSILON

        assert abs(
            sum_recommended + remaining_cash - capital_total
        ) < VirtualTreasuryManager.EPSILON * capital_total
               
        
        # --- Construction du résumé ---
        mean_goi = sum(a.goi for a in allocations) / len(allocations)
        max_goi = max(a.goi for a in allocations)
        min_goi = min(a.goi for a in allocations)

        mean_delta = sum(a.delta for a in allocations) / len(allocations)
        max_delta = max(abs(a.delta) for a in allocations)
        total_absolute_delta = sum(abs(a.delta) for a in allocations)
        
        
        summary = TreasurySummary(
            capital_total=capital_total,
            free_usdt=free_usdt,
            total_recommended=sum_recommended,
            mean_goi=mean_goi,
            max_goi=max_goi,
            min_goi=min_goi,
            number_of_strategies=len(strategies),
            model_version="VTM-v1",
            mean_delta=mean_delta,
            max_delta=max_delta,
            total_absolute_delta=total_absolute_delta,
        )

        return VirtualTreasuryResult(summary=summary, allocations=allocations)

    # -------------------------------------------------------------------------
    # Méthodes privées
    # -------------------------------------------------------------------------

    @staticmethod
    def _apply_bounds_iterative(
        target_budgets: Dict[str, float],
        goi_dict: Dict[str, float],
        min_budget: float,
        max_budget: float,
        capital_total: float,
        strategies: List[StrategyState],
    ) -> Dict[str, float]:
        """
        Applique les bornes min/max de manière itérative en redistribuant
        proportionnellement aux GOI des stratégies libres.

        Principe :
            1. Clamper tous les budgets aux bornes.
            2. Calculer l'écart entre la somme des budgets clampés et capital_total.
            3. Redistribuer l'écart uniquement sur les stratégies qui ne sont pas
               aux bornes (libres), proportionnellement à leurs GOI ; si aucune
               stratégie n'est libre, redistribuer également sur toutes, puis
               reclamper et recalculer l'écart au prochain tour.
            4. Répéter jusqu'à convergence (écart nul dans la tolérance).

        BUGFIX (18/07/2026) : l'ancienne implémentation sortait de la
        boucle immédiatement dès qu'aucune stratégie n'était libre
        (`break`), puis tentait un unique ajustement final suivi d'un
        reclamp — sans jamais revérifier que ce reclamp ne rompait pas
        à nouveau la conservation du capital total. Ce défaut ne se
        manifestait pas tant que le système restait loin de ses
        bornes ; il devenait visible dès que plusieurs stratégies
        s'en approchaient simultanément (capital total réduit,
        free_usdt faible), provoquant l'échec de l'assertion en aval
        dans compute(). Cette version boucle réellement jusqu'à
        convergence, et signale explicitement une infaisabilité
        plutôt que de retourner un résultat incohérent.

        Raises:
            ValueError: si aucune répartition respectant à la fois les
                bornes [min_budget, max_budget] et la conservation du
                capital total n'a pu être trouvée après max_iter
                itérations (cas dégénéré, par exemple si la somme des
                planchers min_budget dépasse capital_total).
        """
        budgets = target_budgets.copy()
        symbols = [s.symbol for s in strategies]
        tolerance = VirtualTreasuryManager.EPSILON * max(1.0, capital_total)

        max_iter = 100
        for _ in range(max_iter):
            # Clamper
            for sym in symbols:
                budgets[sym] = max(min_budget, min(max_budget, budgets[sym]))

            current_sum = sum(budgets[sym] for sym in symbols)
            diff = capital_total - current_sum

            if abs(diff) < tolerance:
                return budgets

            # Identifier les libres (non bloqués)
            free_symbols = [
                sym for sym in symbols
                if min_budget < budgets[sym] < max_budget
            ]

            if not free_symbols:
                # Aucune stratégie libre : redistribuer sur toutes,
                # puis reclamper et réévaluer l'écart au tour suivant
                # (ne jamais sortir de la boucle sans reconvergence).
                for sym in symbols:
                    budgets[sym] += diff / len(symbols)
                continue

            # Redistribution proportionnelle aux GOI des libres
            total_free_goi = sum(goi_dict[sym] for sym in free_symbols)
            if total_free_goi <= VirtualTreasuryManager.EPSILON:
                # Si les GOI des libres sont nuls, on distribue équitablement
                for sym in free_symbols:
                    budgets[sym] += diff / len(free_symbols)
            else:
                for sym in free_symbols:
                    ratio = goi_dict[sym] / total_free_goi
                    budgets[sym] += diff * ratio

        # max_iter épuisées sans convergence : la contrainte est
        # infaisable (ex: somme des planchers min_budget > capital_total).
        # On ne retourne jamais un résultat qui violerait la conservation
        # du capital total en silence.
        for sym in symbols:
            budgets[sym] = max(min_budget, min(max_budget, budgets[sym]))
        final_sum = sum(budgets[sym] for sym in symbols)
        if abs(final_sum - capital_total) > tolerance:
            raise ValueError(
                "Impossible de répartir capital_total="
                f"{capital_total:.2f} entre {len(symbols)} stratégies en "
                f"respectant les bornes [{min_budget:.2f}, {max_budget:.2f}] "
                f"(somme obtenue={final_sum:.2f}, écart={final_sum - capital_total:+.2f}). "
                "Vérifier que la somme des planchers min_budget ne dépasse "
                "pas capital_total, ou ajuster les bornes."
            )
        return budgets

    @staticmethod
    def _apply_diminishing_returns(goi: float, current_budget: float, capital_total: float) -> float:
        """
        Applique une pénalité de rendements marginaux décroissants.

        Actuellement non implémentée (USE_DIMINISHING_RETURNS = False).
        Placeholder pour future extension.
        """
        return goi

    # -------------------------------------------------------------------------
    # Affichage (séparé du calcul, sans couleurs)
    # -------------------------------------------------------------------------

    @staticmethod
    def print_report(result: VirtualTreasuryResult) -> None:
        """
        Affiche un rapport détaillé des allocations recommandées.

        Args:
            result: Résultat du calcul du VirtualTreasuryManager.
        """
        summary = result.summary
        allocations = result.allocations

        print("\n" + "=" * 72)
        print("VIRTUAL TREASURY — ALLOCATION RECOMMENDATION")
        print("=" * 72)
        print(f"Modèle                 : {summary.model_version}")
        print(f"Capital total          : {summary.capital_total:>12.2f} USDT")
        print(f"USDT libre             : {summary.free_usdt:>12.2f} USDT")
        print(f"Nombre de stratégies   : {summary.number_of_strategies:>12}")
        print(f"GOI moyen              : {summary.mean_goi:>12.3f}")
        print(f"GOI max                : {summary.max_goi:>12.3f}")
        print(f"GOI min                : {summary.min_goi:>12.3f}")
        print(f"Delta moyen            : {summary.mean_delta:>12.2f}")
        print(f"Delta max (abs)        : {summary.max_delta:>12.2f}")
        print(f"Delta total (abs)      : {summary.total_absolute_delta:>12.2f}")
        print("-" * 72)

        for a in allocations:
            print(f"{a.symbol}")
            print(f"  GOI               : {a.goi:>10.3f}")
            print(f"  Allocation %      : {a.allocation_pct * 100:>10.1f}%  (cible: {a.target_allocation_pct * 100:>10.1f}%, actuelle: {a.current_allocation_pct * 100:>10.1f}%)")
            print(f"  Budget actuel     : {a.current_budget:>10.2f}")
            print(f"  Budget cible      : {a.target_budget:>10.2f}")
            print(f"  Budget recommandé : {a.recommended_budget:>10.2f}")
            delta_sign = "+" if a.delta >= 0 else ""
            action_str = a.action.value
            cycles = f" (~{a.estimated_cycles} cycles)" if a.estimated_cycles is not None and a.estimated_cycles > 0 else ""
            print(f"  Delta             : {delta_sign}{a.delta:>10.2f}  [{action_str}]{cycles}")
            print("-" * 72)

        # Section SIMULATION
        print("\n" + "=" * 72)
        print("SIMULATION  (projection si les recommandations étaient appliquées)")
        print("=" * 72)
        
        total_delta = sum(a.delta for a in allocations)
        
        remaining_cash = max(0.0, summary.free_usdt - total_delta)
        
        print(f"\nUSDT libre restant : {remaining_cash:>10.2f} USDT")
        print("=" * 72)
