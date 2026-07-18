"""
bot_capital_sync.py

Premiere integration reelle du CapitalTransitionGuard dans bot_gateio.py,
limitee strictement au mecanisme --sync-capital (RN-022 / RN-023).

Ce module fournit deux briques, deliberement separees de bot_gateio.py
(qui est un script executable au niveau module, non importable
proprement dans des tests) afin de rester unitairement testables :

  - StateDictEconomicRepository : un adaptateur satisfaisant
    EconomicStateRepositoryProtocol, qui lit/ecrit allocated_capital
    directement dans le dict `state` deja charge et persiste par
    bot_gateio.py (via sa propre fonction save_state), plutot que dans
    un fichier JSON separe (EconomicStateRepository). Ce choix est
    deliberer : la premiere integration ne doit introduire aucune
    deuxieme source de verite pour allocated_capital tant que le reste
    de bot_gateio.py (compute_capital_view, CapitalViewBuilder, logs,
    metriques) continue de lire ce champ directement depuis le meme
    state dict. Faire persister le Guard vers un fichier separe
    rendrait le state dict immediatement obsolete apres une
    synchronisation, cassant tous les autres flux du bot - exactement
    ce que cette etape s'interdit de faire.

  - build_manual_sync_request : fonction pure qui construit la
    TransitionRequest MANUAL_SYNC correspondant exactement au calcul
    deja effectue aujourd'hui par bot_gateio.py (nouveau capital arrondi
    a 2 decimales, delta signe par rapport a l'ancien), sans dupliquer
    ni modifier ce calcul.

  - merge_allocated_capital_from_disk : PATCH TRANSITOIRE (cf.
    TODO/RN a creer dans bot_gateio.py, fonction save_state()).
    Empeche le process bot d'ecraser silencieusement une correction
    externe d'allocated_capital (MANUAL_SYNC ou META_CORRECTION
    survenue depuis un autre process) en adoptant la valeur lue sur
    disque juste avant chaque sauvegarde. Ne resout pas la course
    critique dans l'absolu (pas de verrouillage inter-processus
    strict autour du cycle lecture-modification-ecriture), mais
    elimine le cas observe en production (ecrasement par une valeur
    figee en memoire depuis le demarrage du bot).

  - skip_merge_for_guard_self_write : BUGFIX DU PATCH TRANSITOIRE
    CI-DESSUS, decouvert en production le 18/07/2026. Le patch de
    fusion, applique sans distinction a chaque appel de save_state(),
    ecrasait aussi la propre ecriture du Guard : quand
    StateDictEconomicRepository.save() appelle save_state() pour
    persister une transition MANUAL_SYNC/REALIZED_PROFIT/REALIZED_LOSS
    deja calculee et deja appliquee a `state` en memoire, save_state()
    relisait le disque (encore l'ancienne valeur, puisque c'est
    precisement cette ecriture qui doit la remplacer) et l'adoptait a
    la place de la nouvelle valeur correcte - annulant silencieusement
    la transition que le Guard venait pourtant d'accepter. Cette
    fonction encapsule la regle de decision : la fusion ne doit
    s'appliquer QUE lorsque save_state() est appelee pour une raison
    independante du Guard (calibration, wallet_peak, etc.), jamais
    lorsqu'elle est appelee PAR le Guard pour persister sa propre
    ecriture deja legitime.

Aucune logique de validation, de resolution ou de decision n'est
ajoutee ici : ce module se contente de traduire le mecanisme existant
en une demande de transition conforme au contrat du Guard.
"""

from __future__ import annotations

from typing import Callable

from capital_transition_guard import (
    AbsoluteAmount,
    EconomicState,
    TransitionCause,
    TransitionOrigin,
    TransitionRequest,
)


class StateDictEconomicRepository:
    """
    Adaptateur EconomicStateRepositoryProtocol operant directement sur
    le state dict d'un bot deja charge en memoire, plutot que sur un
    fichier JSON dedie.

    Portee strictement locale a un seul bot_id (verifiee a chaque
    appel), conformement a la portee locale du Guard.
    """

    def __init__(self, state: dict, bot_id: str, save_fn: Callable[[dict], None]):
        """
        Args:
            state: Le dict d'etat du bot, deja charge par bot_gateio.py
                (load_state()). Reference partagee : toute modification
                effectuee ici est immediatement visible du reste du
                script, exactement comme l'ecriture directe qu'elle
                remplace.
            bot_id: Identifiant du bot pour lequel ce repository est
                valide (le symbole courant, ex: CURRENT_SYMBOL).
            save_fn: Fonction de persistance du state dict (save_state
                de bot_gateio.py), appelee apres chaque ecriture.
        """
        self._state = state
        self._bot_id = bot_id
        self._save_fn = save_fn

    def load(self, bot_id: str) -> EconomicState:
        """
        Lit allocated_capital directement depuis le state dict.

        Raises:
            KeyError: si bot_id ne correspond pas au bot pour lequel ce
                repository a ete construit.
        """
        if bot_id != self._bot_id:
            raise KeyError(
                f"StateDictEconomicRepository est lie au bot '{self._bot_id}', "
                f"appel recu pour '{bot_id}'."
            )
        return EconomicState(allocated_capital=self._state.get("allocated_capital", 0.0))

    def save(self, bot_id: str, state: EconomicState) -> None:
        """
        Ecrit allocated_capital dans le state dict, puis persiste via
        save_fn (identique au mecanisme de sauvegarde deja utilise par
        le reste de bot_gateio.py).

        Raises:
            KeyError: si bot_id ne correspond pas au bot pour lequel ce
                repository a ete construit.
        """
        if bot_id != self._bot_id:
            raise KeyError(
                f"StateDictEconomicRepository est lie au bot '{self._bot_id}', "
                f"appel recu pour '{bot_id}'."
            )
        self._state["allocated_capital"] = state.allocated_capital
        self._save_fn(self._state)


def build_manual_sync_request(
    bot_id: str,
    old_allocated: float,
    new_allocated: float,
    justification: str,
) -> TransitionRequest:
    """
    Construit la TransitionRequest MANUAL_SYNC correspondant au
    mecanisme --sync-capital existant.

    Reproduit exactement le calcul actuel de bot_gateio.py : le nouveau
    capital est arrondi a 2 decimales, et la transition porte le delta
    signe entre l'ancien et le nouveau montant (jamais une valeur
    absolue cible, conformement a RN-022/RN-023). Applique par le
    Guard contre l'etat courant (old_allocated, lu par le Repository au
    moment de l'application), cela reproduit exactement
    round(new_allocated, 2) comme resultat final.

    Args:
        bot_id: Identifiant du bot concerne.
        old_allocated: Valeur actuelle d'allocated_capital avant
            synchronisation.
        new_allocated: Nouveau capital calcule (non arrondi) a partir
            du wallet reel, du PnL realise et du PnL latent.
        justification: Motif de la synchronisation (obligatoire pour
            MANUAL_SYNC, cf. RN-022).

    Returns:
        La TransitionRequest prete a etre soumise au
        CapitalTransitionGuard.
    """
    rounded_target = round(new_allocated, 2)
    delta = rounded_target - old_allocated
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.MANUAL_SYNC,
        origin=TransitionOrigin.OPERATOR,
        value=AbsoluteAmount(amount=delta),
        justification=justification,
    )


def merge_allocated_capital_from_disk(
    state: dict,
    on_disk_state: "dict | None",
) -> None:
    """
    PATCH TRANSITOIRE — adopte allocated_capital lu sur disque dans
    `state`, si present, juste avant une sauvegarde.

    Contexte (cf. TODO/RN a creer, cite dans bot_gateio.py :
    save_state()) : allocated_capital peut desormais etre modifie par
    un processus tiers (CapitalTransitionGuard sollicite par le
    MetaController via META_CORRECTION, ou par --sync-capital dans un
    autre run de ce meme bot) pendant que ce process garde sa propre
    copie en memoire, chargee une seule fois au demarrage. Sans cette
    fusion, une sauvegarde ulterieure (calibration, wallet_peak,
    trade...) ecraserait silencieusement cette correction externe avec
    l'ancienne valeur memorisee.

    Cette fonction ne resout pas la course critique dans l'absolu (le
    MetaController pourrait encore ecrire entre la lecture ici et
    l'ecriture qui suit dans save_state(), en l'absence de
    verrouillage inter-processus strict autour de ce cycle) : elle
    elimine le cas dominant observe en production (ecrasement par une
    valeur figee en memoire depuis le demarrage du bot).

    Fonction pure sur le plan du calcul (mutation de `state` en place,
    a l'image des autres mutations deja effectuees par save_state()
    historiquement) : ne lit ni n'ecrit elle-meme sur disque, se
    contente de fusionner deux dictionnaires deja fournis.

    Args:
        state: Le state dict du bot, sur le point d'etre sauvegarde.
            Mute en place si allocated_capital est present dans
            on_disk_state.
        on_disk_state: Le contenu actuellement persiste sur disque
            (typiquement STATE_STORE.read()), ou None si illisible ou
            absent.
    """
    if on_disk_state is not None and "allocated_capital" in on_disk_state:
        state["allocated_capital"] = on_disk_state["allocated_capital"]


def apply_disk_merge_unless_guard_write(
    state: dict,
    on_disk_state: "dict | None",
    is_guard_write: bool,
) -> None:
    """
    Applique merge_allocated_capital_from_disk(), sauf lorsque cette
    sauvegarde est elle-meme l'ecriture legitime du
    CapitalTransitionGuard (bugfix du 18/07/2026, cf. docstring de
    module).

    Cette fonction encapsule la seule ligne de decision necessaire :
    quand `is_guard_write` est vrai, save_state() est appelee par
    StateDictEconomicRepository.save() pour persister une transition
    deja calculee et deja appliquee a `state` — la fusion ne doit
    surtout pas s'executer, sinon elle ecraserait cette ecriture
    legitime avec l'ancienne valeur encore presente sur disque (c'est
    precisement cette ecriture qui doit la remplacer). Dans tous les
    autres cas (calibration, wallet_peak, achats, ventes...),
    `is_guard_write` est faux et la fusion s'applique normalement,
    conformement au patch transitoire.

    Args:
        state: Le state dict du bot, sur le point d'etre sauvegarde.
        on_disk_state: Le contenu actuellement persiste sur disque, ou
            None si illisible ou absent.
        is_guard_write: True si cette sauvegarde est declenchee par
            StateDictEconomicRepository.save() (donc par le Guard
            lui-meme) ; False pour tout autre appelant de save_state().
    """
    if is_guard_write:
        return
    merge_allocated_capital_from_disk(state, on_disk_state)
