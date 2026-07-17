"""
meta_capital_sync.py

Migration des corrections de capital du MetaController vers le
CapitalTransitionGuard (RN-022 / RN-023), TransitionType META_CORRECTION.

Ce module fournit les briques utilisees par BotManager.apply_transaction()
pour remplacer l'ancienne publication d'un fichier de controle
(control_{symbol}.json, lu par CapitalTargetController) par une
transition soumise directement au Guard, appliquee sur le fichier
d'etat reel du bot (allocated_capital).

  - BotStateFileEconomicRepository : adaptateur satisfaisant
    EconomicStateRepositoryProtocol, qui lit/ecrit allocated_capital
    directement dans le fichier d'etat reel du bot
    (state_{exchange}_{symbol}.json), via AtomicJsonStateStore - la
    meme classe que celle utilisee par bot_gateio.py lui-meme pour son
    propre state (STATE_STORE), garantissant le meme verrouillage
    fichier inter-processus (fcntl.flock) entre le MetaController et le
    bot. Seul le champ allocated_capital est modifie ; tous les autres
    champs du fichier (inventaire, grille, PnL, etc.) sont relus et
    reecrits tels quels, jamais touches.

  - build_meta_correction_request : fonction pure qui convertit une
    cible absolue (new_budget, telle que produite aujourd'hui par
    VirtualTreasuryManager -> ExecutionPlanner -> ExecutionOperation)
    en une regle relative (RelativeCorrection), seule forme de valeur
    autorisee pour META_CORRECTION (RN-023). La conversion est
    necessaire car le Guard ne poursuit jamais une valeur absolue :
    seule une correction relative au capital courant est soumise, a
    resoudre par le Guard contre l'etat lu au moment de l'application.

Limite connue et assumee : si le capital actuellement alloue est nul
ou negatif, aucune fraction relative ne peut exprimer une correction
vers une cible non nulle (une fraction, quelle qu'elle soit, appliquee
a zero reste zero). build_meta_correction_request leve alors une
ValueError explicite plutot que de produire silencieusement une
fraction infinie ou nulle a tort.

Risque architectural a signaler, hors perimetre de ce module : une
fois ce mecanisme en place, BotManager.apply_transaction() n'ecrit
plus le fichier control_{symbol}.json. CapitalTargetController (execute
a l'interieur de bot_gateio.py) continuera de lire ce fichier, qui ne
sera donc plus mis a jour par de nouvelles corrections du
MetaController : son capital_ratio restera fige a sa derniere valeur
connue. allocated_capital, lui, sera bien mis a jour directement (donc
capital_for_grid = min(wallet_real, allocated_capital) reflete la
correction), mais il continuera d'etre multiplie par ce ratio devenu
non pertinent. Corriger ce point necessite de toucher a bot_gateio.py
(hors perimetre explicite de cette etape, qui interdit de modifier
d'autres flux du bot) et devra faire l'objet d'une etape dediee.
"""

from __future__ import annotations

from pathlib import Path

from capital_transition_guard import (
    EconomicState,
    RelativeCorrection,
    TransitionCause,
    TransitionOrigin,
    TransitionRequest,
)
from process_synchronization import AtomicJsonStateStore


class BotStateFileEconomicRepository:
    """
    Adaptateur EconomicStateRepositoryProtocol operant directement sur
    le fichier d'etat reel d'un bot, en ne touchant qu'au champ
    allocated_capital.

    Portee strictement locale a un seul bot_id (verifiee a chaque
    appel). Reutilise AtomicJsonStateStore (deja utilise par
    bot_gateio.py pour son propre state), ce qui garantit le meme
    verrouillage fichier inter-processus entre le MetaController et le
    bot, sans introduire de nouveau mecanisme de verrouillage.
    """

    def __init__(self, state_file: Path, bot_id: str):
        """
        Args:
            state_file: Chemin du fichier d'etat reel du bot
                (ex: descriptor.state_file, fourni par BotManager).
            bot_id: Identifiant du bot pour lequel ce repository est
                valide (le symbole).
        """
        self._store = AtomicJsonStateStore(state_file)
        self._bot_id = bot_id

    def load(self, bot_id: str) -> EconomicState:
        """
        Lit allocated_capital depuis le fichier d'etat reel du bot.

        Ne touche a aucun autre champ du fichier : la lecture complete
        est effectuee (via AtomicJsonStateStore.read()), mais seul
        allocated_capital est extrait et retourne.

        Raises:
            KeyError: si bot_id ne correspond pas au bot pour lequel ce
                repository a ete construit, ou si allocated_capital est
                absent du fichier.
            OSError: si le fichier est illisible.
        """
        if bot_id != self._bot_id:
            raise KeyError(
                f"BotStateFileEconomicRepository est lie au bot '{self._bot_id}', "
                f"appel recu pour '{bot_id}'."
            )
        data = self._store.read()
        if data is None:
            raise OSError(f"State illisible : {self._store.path}")
        allocated = data.get("allocated_capital")
        if allocated is None:
            raise KeyError(f"allocated_capital manquant dans {self._store.path}")
        return EconomicState(allocated_capital=float(allocated))

    def save(self, bot_id: str, state: EconomicState) -> None:
        """
        Ecrit allocated_capital dans le fichier d'etat reel du bot.

        Relit d'abord le fichier complet, ne modifie que le champ
        allocated_capital, puis reecrit le fichier complet - tous les
        autres champs (inventaire, grille, PnL, wallet_peak, etc.)
        restent strictement inchanges.

        Raises:
            KeyError: si bot_id ne correspond pas au bot pour lequel ce
                repository a ete construit.
            OSError: si le fichier est illisible.
        """
        if bot_id != self._bot_id:
            raise KeyError(
                f"BotStateFileEconomicRepository est lie au bot '{self._bot_id}', "
                f"appel recu pour '{bot_id}'."
            )
        data = self._store.read()
        if data is None:
            raise OSError(f"State illisible : {self._store.path}")
        data["allocated_capital"] = state.allocated_capital
        self._store.write(data)


def build_meta_correction_request(
    bot_id: str,
    current_allocated: float,
    new_budget: float,
    justification: str,
) -> TransitionRequest:
    """
    Construit la TransitionRequest META_CORRECTION correspondant a une
    cible absolue calculee en amont (VirtualTreasuryManager ->
    ExecutionPlanner), convertie en correction relative.

    Le Guard n'accepte jamais de valeur absolue pour META_CORRECTION
    (RN-022/RN-023) : la cible est donc traduite en une fraction du
    capital actuellement alloue, resolue par le Guard contre l'etat lu
    au moment de l'application (jamais contre current_allocated tel
    que lu ici, qui ne sert qu'a calculer la fraction).

    Args:
        bot_id: Identifiant du bot concerne (symbole).
        current_allocated: Capital actuellement alloue, lu juste avant
            construction de la requete.
        new_budget: Cible absolue calculee par
            VirtualTreasuryManager/ExecutionPlanner.
        justification: Motif de la correction.

    Returns:
        La TransitionRequest prete a etre soumise au
        CapitalTransitionGuard.

    Raises:
        ValueError: si current_allocated <= 0, cas dans lequel aucune
            fraction relative ne peut exprimer une correction vers une
            cible non nulle.
    """
    if current_allocated <= 0:
        raise ValueError(
            "Impossible d'exprimer une correction relative : "
            f"current_allocated={current_allocated} <= 0."
        )
    fraction = (new_budget - current_allocated) / current_allocated
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.META_CORRECTION,
        origin=TransitionOrigin.META_CONTROLLER,
        value=RelativeCorrection(fraction=fraction),
        justification=justification,
    )
