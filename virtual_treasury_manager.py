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
        remaining_cash: Capital non alloué à ce cycle (RN-029).
            Toujours ≥ 0 : représente le capital que le lissage n'a
            volontairement pas encore déployé (convergence progressive
            sur plusieurs cycles), jamais un manque à combler. Ne doit
            jamais être interprété comme une anomalie.
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
    remaining_cash: float = 0.0
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


class TreasuryReconciliationError(ValueError):
    """
    Levée par VirtualTreasuryManager._reconcile_deltas() lorsqu'aucune
    solution ne permet de satisfaire simultanément les invariants I1-I3
    de RN-028 (conservation du signe, conservation du capital, respect
    des bornes) — cas d'infaisabilité structurelle (ex : toutes les
    stratégies concernées par un même sens de correction sont déjà
    saturées à leur borne, et le résidu à absorber dépasse ce qu'elles
    peuvent encore encaisser).

    Conformément à RN-028 (I4), cette exception est explicite plutôt
    que de laisser _reconcile_deltas() retourner silencieusement un
    résultat qui violerait l'un de ces invariants.

    Attributes:
        residual: Écart de capital non résolu au moment de l'échec
            (capital_total - somme des budgets après la tentative de
            réconciliation).
        saturation: Diagnostic par stratégie. Pour chaque symbole
            concerné, un dict {"delta": ..., "lo": ..., "hi": ...}
            indiquant le delta atteint et l'intervalle qui le contraint
            (cf. contrat de _reconcile_deltas()).
    """

    def __init__(self, message: str, residual: float, saturation: Dict[str, Dict[str, float]]):
        super().__init__(message)
        self.residual = residual
        self.saturation = saturation


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
        # RN-027/RN-028, étape 5 de l'implémentation : le pipeline
        # travaille désormais dans l'espace des deltas pour la
        # réconciliation, jamais dans l'espace des budgets absolus —
        # c'est précisément ce changement d'espace qui permet de
        # garantir la conservation du signe (I1), impossible à assurer
        # structurellement quand on redistribue des budgets (cf.
        # démonstration de l'incident et RN-027).
        decided_deltas: Dict[str, float] = {}
        actions: Dict[str, AllocationAction] = {}
        for s in strategies:
            current = s.current_budget
            target = clamped[s.symbol]
            raw_recommended = current + VirtualTreasuryManager.SMOOTHING_FACTOR * (target - current)
            raw_delta = raw_recommended - current
            actions[s.symbol] = VirtualTreasuryManager._classify_delta_sign(raw_delta)
            if abs(raw_delta) < VirtualTreasuryManager.MIN_DELTA_ACTION:
                decided_deltas[s.symbol] = 0.0
            else:
                decided_deltas[s.symbol] = raw_delta

        current_budgets = {s.symbol: s.current_budget for s in strategies}

        reconciled_deltas = VirtualTreasuryManager._reconcile_deltas(
            decided_deltas, current_budgets, min_budget, max_budget, capital_total, goi_dict
        )

        allocations = []
        deltas = []
        for idx, s in enumerate(strategies):
            current = s.current_budget
            target = clamped[s.symbol]
            delta = reconciled_deltas[s.symbol]
            recommended = current + delta

            # RN-027/RN-028, étape 2 : action n'est plus recalculé ici.
            # C'est précisément ce recalcul (à partir du budget final,
            # après réconciliation) qui permettait à une réconciliation
            # de changer le signe d'une décision déjà prise à l'étape
            # Deadband — cf. démonstration de l'incident FIL/STX.
            action = actions[s.symbol]

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
            remaining_cash=remaining_cash,
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
    def _reconcile_deltas(
        decided_deltas: Dict[str, float],
        current_budgets: Dict[str, float],
        min_budget: float,
        max_budget: float,
        capital_total: float,
        goi_dict: Dict[str, float],
    ) -> Dict[str, float]:
        """
        Réconcilie des deltas déjà décidés (étape Deadband) pour que
        leur somme ne dépasse jamais le capital total disponible, sans
        jamais changer leur signe, les neutraliser, ni les amplifier
        (RN-027/RN-028/RN-029).

        Invariants garantis (contrat complet) :
            I1  — sign(résultat[sym]) == sign(decided_deltas[sym])
                  pour tout sym où decided_deltas[sym] ≠ 0.
            I2  — Σ(current_budgets[sym] + résultat[sym]) ≤ capital_total.
                  Un dépassement (Σ décidé > capital_total) est corrigé
                  par réduction ; un capital non alloué (Σ décidé <
                  capital_total) est légitime et n'est jamais comblé
                  (RN-029).
            I3  — min_budget ≤ current_budgets[sym] + résultat[sym] ≤ max_budget.
            I8 (RN-029, NON-AMPLIFICATION) — |résultat[sym]| ≤
                  |decided_deltas[sym]| pour tout sym, sans exception.
                  La réconciliation ne peut jamais rendre une décision
                  plus importante que ce que le lissage a décidé — dans
                  AUCUNE des deux directions (ni combler un capital non
                  alloué en l'augmentant, ni "sur-corriger" un
                  dépassement en l'amplifiant au-delà du strict
                  nécessaire). Cet invariant est plus fort que I1 : il
                  ne suffit pas de préserver le signe, il faut préserver
                  (ou réduire) la magnitude.

        Conséquence directe de I8 : une décision DECREASE n'est JAMAIS
        modifiée par la réconciliation. La rendre plus négative
        violerait I8 (amplification) ; la rendre moins négative
        aggraverait mécaniquement tout dépassement à résorber (ça
        n'aide jamais). Il n'existe donc aucune direction légitime dans
        laquelle toucher une DECREASE serait à la fois utile et
        conforme à I8 — la réconciliation ne considère plus les
        stratégies DECREASE comme un espace de correction. Seules les
        stratégies INCREASE peuvent être réduites (jamais en dessous de
        MIN_DELTA_ACTION, jamais au-delà de leur propre valeur décidée).

        La réduction parmi les stratégies INCREASE est répartie
        proportionnellement à leur capacité disponible
        (decided_delta - MIN_DELTA_ACTION), PAS au GOI : le GOI a déjà
        influencé le calcul du lissage en amont (Target Budgets,
        Smoothing) ; le réintroduire ici referait de la réconciliation
        un second moteur de décision économique, contrairement à sa
        nature de simple contrôle de faisabilité mécanique (RN-029).

        Ce contrat est volontairement indépendant de tout algorithme
        particulier (cf. spécification RN-027/RN-028 v2, §3) : il
        décrit uniquement ce que le résultat doit satisfaire, jamais
        comment y parvenir.

        Args:
            decided_deltas: Deltas déjà décidés à l'étape Deadband
                (peut contenir des zéros pour les stratégies en HOLD).
            current_budgets: Budgets actuels par stratégie.
            min_budget: Borne minimale globale (identique à celle de
                l'étape Bounds).
            max_budget: Borne maximale globale (identique à celle de
                l'étape Bounds).
            capital_total: Capital total à ne jamais dépasser.
            goi_dict: Conservé pour la stabilité de la signature (et la
                compatibilité des appels existants), mais n'est plus
                consulté par l'algorithme de réduction (RN-029) — voir
                ci-dessus. Uniquement validé (non négatif) comme
                précondition, jamais utilisé pour pondérer quoi que ce
                soit.

        Returns:
            Dict[str, float] : les deltas corrigés (reconciled_deltas)
            — jamais des budgets absolus. Leur somme peut être
            strictement inférieure à ce que capital_total permettrait
            (RN-029) ; c'est un résultat valide, pas une erreur.

        Raises:
            ValueError: si une précondition n'est pas respectée
                (incohérence des clés, bornes invalides, GOI négatif,
                capital_total non positif, budget actuel hors bornes).
            TreasuryReconciliationError: si un dépassement (Σ >
                capital_total) ne peut être résorbé par la seule
                réduction des stratégies INCREASE sans les neutraliser
                — cas d'infaisabilité structurelle. Ne concerne jamais
                le cas du capital non alloué (RN-029), qui n'est pas
                une infaisabilité.
        """
        # --- Préconditions ---
        symbols = set(current_budgets.keys())
        if set(decided_deltas.keys()) != symbols or set(goi_dict.keys()) != symbols:
            raise ValueError(
                "_reconcile_deltas : decided_deltas, current_budgets et "
                "goi_dict doivent porter exactement les mêmes clés "
                f"(current_budgets={sorted(current_budgets.keys())}, "
                f"decided_deltas={sorted(decided_deltas.keys())}, "
                f"goi_dict={sorted(goi_dict.keys())})."
            )

        if capital_total <= 0:
            raise ValueError(
                f"_reconcile_deltas : capital_total doit être strictement "
                f"positif, reçu {capital_total}."
            )

        if not (0 <= min_budget <= max_budget):
            raise ValueError(
                f"_reconcile_deltas : bornes invalides "
                f"(min_budget={min_budget}, max_budget={max_budget}), "
                f"attendu 0 ≤ min_budget ≤ max_budget."
            )

        for sym in symbols:
            budget = current_budgets[sym]
            if not (min_budget - VirtualTreasuryManager.EPSILON <= budget
                    <= max_budget + VirtualTreasuryManager.EPSILON):
                raise ValueError(
                    f"_reconcile_deltas : current_budgets['{sym}']={budget} "
                    f"hors des bornes [{min_budget}, {max_budget}] "
                    f"(devrait déjà être garanti par l'étape Bounds)."
                )
            if goi_dict[sym] < 0:
                raise ValueError(
                    f"_reconcile_deltas : goi_dict['{sym}']={goi_dict[sym]} "
                    f"ne peut pas être négatif."
                )

        # --- Algorithme de réconciliation (Famille C, révisée RN-029) ---
        # CHOIX EXPLICITE, documenté ici. Ce choix n'engage pas le
        # contrat ci-dessus : toute autre famille satisfaisant les
        # mêmes préconditions et postconditions (I1-I8) peut la
        # remplacer sans changer la signature ni le comportement
        # observable de cette fonction.
        #
        # Bornes par stratégie (I8 : jamais au-delà de la décision) :
        #   - INCREASE (d > 0) : δ ∈ [MIN_DELTA_ACTION, d]
        #       (réductible vers son plancher, jamais amplifié au-delà de d)
        #   - DECREASE (d < 0) : δ = d, verrouillé (jamais modifié — voir
        #       docstring : aucune direction n'est à la fois utile et
        #       conforme à I8 pour une DECREASE)
        #   - HOLD (d == 0) : δ = 0, verrouillé (Q1)
        tolerance = VirtualTreasuryManager.EPSILON * max(1.0, capital_total)

        lo: Dict[str, float] = {}
        hi: Dict[str, float] = {}
        capacity: Dict[str, float] = {}
        for sym in symbols:
            d = decided_deltas[sym]
            current = current_budgets[sym]

            # La décision elle-même doit respecter les bornes
            # budgétaires. Ce n'était auparavant vérifié qu'indirectement
            # via lo/hi dérivés de min_budget/max_budget ; ce n'est plus
            # le cas pour DECREASE/HOLD, désormais gelées (I8) — donc
            # plus aucun mécanisme interne ne pourrait sinon corriger une
            # décision déjà hors bornes. Doit être vérifié explicitement,
            # pour tout signe de décision.
            projected = current + d
            if not (min_budget - tolerance <= projected <= max_budget + tolerance):
                raise TreasuryReconciliationError(
                    f"Réconciliation infaisable : la décision pour '{sym}' "
                    f"(decided_delta={d:+.2f}) produirait un budget de "
                    f"{projected:.2f}, hors des bornes [{min_budget:.2f}, "
                    f"{max_budget:.2f}]. Cette décision ne peut pas être "
                    f"ajustée par la réconciliation sans l'amplifier ou la "
                    f"neutraliser (I8) — l'incohérence doit être corrigée "
                    f"en amont (étape Bounds/Smoothing).",
                    residual=float("nan"),
                    saturation={sym: {"delta": d, "lo": min_budget - current, "hi": max_budget - current}},
                )

            if d > 0:
                lo[sym] = VirtualTreasuryManager.MIN_DELTA_ACTION
                hi[sym] = d
                capacity[sym] = d - VirtualTreasuryManager.MIN_DELTA_ACTION
            else:
                # DECREASE ou HOLD : verrouillé, aucune marge de
                # réconciliation (I8).
                lo[sym] = d
                hi[sym] = d
                capacity[sym] = 0.0

            if lo[sym] > hi[sym] + tolerance:
                raise TreasuryReconciliationError(
                    f"Réconciliation infaisable : la stratégie '{sym}' "
                    f"(decided_delta={d:+.2f}) ne dispose pas de la marge "
                    f"minimale requise (MIN_DELTA_ACTION="
                    f"{VirtualTreasuryManager.MIN_DELTA_ACTION:.2f}) sans "
                    f"être amplifiée au-delà de sa propre décision.",
                    residual=float("nan"),
                    saturation={sym: {"delta": d, "lo": lo[sym], "hi": hi[sym]}},
                )

        reconciled: Dict[str, float] = dict(decided_deltas)
        target_diff = capital_total - sum(current_budgets.values())

        # Premier clamp aux bornes propres de chaque décision.
        for sym in symbols:
            reconciled[sym] = max(lo[sym], min(hi[sym], reconciled[sym]))

        initial_residual = target_diff - sum(reconciled.values())

        # RN-029 : le capital non alloué (résidu positif, la somme des
        # décisions déjà prises est inférieure au capital disponible)
        # est un état légitime — c'est le fonctionnement normal d'un
        # lissage qui ne bouge volontairement qu'une fraction de
        # l'écart brut par cycle (SMOOTHING_FACTOR). La réconciliation
        # ne doit jamais combler cet espace en amplifiant des décisions
        # existantes (I8) : seul un dépassement (résidu négatif) doit
        # être corrigé. Le reliquat éventuel est retourné tel quel à
        # l'appelant, qui le reporte en remaining_cash.
        if initial_residual >= -tolerance:
            return reconciled

        max_iter = 100
        for _ in range(max_iter):
            for sym in symbols:
                reconciled[sym] = max(lo[sym], min(hi[sym], reconciled[sym]))

            residual = target_diff - sum(reconciled.values())
            if residual >= -tolerance:
                # Dépassement résorbé (ou capital non alloué apparu en
                # cours de route, ce qui reste légitime — RN-029) :
                # on ne cherche jamais à revenir exactement à l'égalité.
                return reconciled

            # Uniquement les stratégies INCREASE encore réductibles
            # (I8 : jamais en dessous de MIN_DELTA_ACTION). Les
            # DECREASE/HOLD ont capacity=0 et lo==hi==reconciled dès le
            # départ : elles ne peuvent structurellement jamais entrer
            # dans ce groupe.
            group = [
                sym for sym in symbols
                if decided_deltas[sym] > 0 and reconciled[sym] > lo[sym] + tolerance
            ]

            if not group:
                break

            # Répartition mécanique, proportionnelle à la capacité
            # disponible de chacun — jamais au GOI (RN-029) : le GOI a
            # déjà influencé la décision en amont ; la réconciliation
            # ne réintroduit aucune logique économique.
            total_capacity = sum(capacity[sym] for sym in group)
            if total_capacity <= tolerance:
                for sym in group:
                    reconciled[sym] += residual / len(group)
            else:
                for sym in group:
                    reconciled[sym] += residual * (capacity[sym] / total_capacity)

        # Non convergé après max_iter (ou plus aucune stratégie
        # INCREASE disponible pour absorber le dépassement) :
        # infaisabilité structurelle, échec explicite (I4) plutôt qu'un
        # résultat silencieusement incohérent.
        for sym in symbols:
            reconciled[sym] = max(lo[sym], min(hi[sym], reconciled[sym]))
        final_residual = target_diff - sum(reconciled.values())
        if final_residual < -tolerance:
            saturation = {
                sym: {"delta": reconciled[sym], "lo": lo[sym], "hi": hi[sym]}
                for sym in symbols
            }
            raise TreasuryReconciliationError(
                f"Réconciliation infaisable : dépassement non résorbé = "
                f"{-final_residual:.4f} après {max_iter} itérations. "
                f"Les stratégies INCREASE ne disposent pas d'assez de "
                f"capacité résiduelle pour absorber le dépassement sans "
                f"amplifier une décision DECREASE (interdit par I8) ; "
                f"revoir les bornes ou le capital disponible.",
                residual=final_residual,
                saturation=saturation,
            )

        return reconciled

    @staticmethod
    def _classify_delta_sign(delta: float) -> AllocationAction:
        """
        Traduit un delta numérique en décision qualitative
        (RN-027/RN-028, étape 1 de l'implémentation).

        Fonction pure, sans effet de bord, déterministe. Centralise la
        seule règle de classification utilisée dans tout le pipeline,
        pour qu'elle ne soit jamais recalculée différemment à deux
        endroits (c'est cette duplication implicite — un calcul de
        "action" à l'étape du lissage, un autre recalcul silencieux à
        partir du budget final — qui permettait à la réconciliation de
        changer le signe d'une décision déjà prise, cf. RN-027).

        Args:
            delta: Écart signé entre un budget cible et le budget
                actuel (ou toute grandeur de même nature).

        Returns:
            AllocationAction.HOLD si |delta| < MIN_DELTA_ACTION (zone
            morte), sinon AllocationAction.INCREASE si delta > 0,
            AllocationAction.DECREASE si delta < 0.
        """
        if abs(delta) < VirtualTreasuryManager.MIN_DELTA_ACTION:
            return AllocationAction.HOLD
        return AllocationAction.INCREASE if delta > 0 else AllocationAction.DECREASE

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
        print(f"Capital non alloué     : {summary.remaining_cash:>12.2f} USDT  (RN-029 : reporté aux cycles suivants, pas une anomalie)")
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
