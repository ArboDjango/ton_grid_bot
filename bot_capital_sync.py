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
