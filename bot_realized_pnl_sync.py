"""
bot_realized_pnl_sync.py

Migration du PnL realise (bot_gateio.py) vers le CapitalTransitionGuard
(RN-022 / RN-023), TransitionTypes REALIZED_PROFIT et REALIZED_LOSS.

Ce module ne contient que deux fonctions pures, symetriques : la
construction de la TransitionRequest correspondant a un profit ou a
une perte deja calcules par le bot. Aucun calcul de PnL n'a lieu ici -
le montant transmis est exactement celui deja calcule par
bot_gateio.py (pnl_trade, net de frais, issu de la consommation FIFO),
inchange.

La decision de savoir si un montant constitue un profit ou une perte
n'est pas prise ici : c'est l'appelant (bot_gateio.py) qui decide,
avant meme d'appeler l'une ou l'autre de ces fonctions, de la branche
a emprunter (pnl_trade > 0 -> profit, pnl_trade < 0 -> perte). Ces
fonctions se contentent de traduire un montant deja qualifie en une
TransitionRequest structurellement valide.

Repository reutilise : StateDictEconomicRepository (bot_capital_sync.py),
deja construit pour operer sur le meme state dict en memoire que le
reste de bot_gateio.py - aucun nouveau mecanisme de persistance n'est
introduit par cette etape.
"""

from __future__ import annotations

from capital_transition_guard import (
    AbsoluteAmount,
    TransitionCause,
    TransitionOrigin,
    TransitionRequest,
)


def build_realized_profit_request(
    bot_id: str,
    amount: float,
    justification: str = "",
) -> TransitionRequest:
    """
    Construit la TransitionRequest REALIZED_PROFIT correspondant a un
    profit deja calcule par le bot.

    Fonction pure : ne calcule rien, ne valide pas le signe de
    `amount` (cette decision appartient a l'appelant), se contente de
    transporter le montant deja determine dans une TransitionRequest
    structurellement conforme.

    Args:
        bot_id: Identifiant du bot concerne (symbole).
        amount: Montant du profit realise, exactement tel que calcule
            par bot_gateio.py (pnl_trade, net de frais).
        justification: Motif optionnel (non obligatoire pour
            REALIZED_PROFIT, a la difference de MANUAL_SYNC).

    Returns:
        La TransitionRequest prete a etre soumise au
        CapitalTransitionGuard.
    """
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.REALIZED_PROFIT,
        origin=TransitionOrigin.BOT,
        value=AbsoluteAmount(amount=amount),
        justification=justification,
    )


def build_realized_loss_request(
    bot_id: str,
    amount: float,
    justification: str = "",
) -> TransitionRequest:
    """
    Construit la TransitionRequest REALIZED_LOSS correspondant a une
    perte deja calculee par le bot.

    Fonction pure, symetrique a build_realized_profit_request : ne
    calcule rien, ne valide pas le signe de `amount` (cette decision
    appartient a l'appelant), se contente de transporter le montant
    deja determine dans une TransitionRequest structurellement
    conforme.

    Args:
        bot_id: Identifiant du bot concerne (symbole).
        amount: Montant de la perte realisee, exactement tel que
            calcule par bot_gateio.py (pnl_trade, net de frais).
            Habituellement negatif, mais cette fonction ne l'impose
            pas : la decision de qualifier ce montant de perte
            appartient a l'appelant.
        justification: Motif optionnel (non obligatoire pour
            REALIZED_LOSS, a la difference de MANUAL_SYNC).

    Returns:
        La TransitionRequest prete a etre soumise au
        CapitalTransitionGuard.
    """
    return TransitionRequest(
        bot_id=bot_id,
        cause=TransitionCause.REALIZED_LOSS,
        origin=TransitionOrigin.BOT,
        value=AbsoluteAmount(amount=amount),
        justification=justification,
    )
