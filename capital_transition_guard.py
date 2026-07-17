"""
capital_transition_guard.py

Modele de donnees du CapitalTransitionGuard (RN-023), conforme aux
principes de RN-022 (Budget Strategique Vivant).

Etape 1 du plan de reconstruction : fondations du domaine uniquement.

Ce module ne contient :
  - aucune validation ;
  - aucune persistance ;
  - aucune decision economique ;
  - aucun calcul ;
  - aucune journalisation effective.

Il definit uniquement les structures de donnees et le squelette de
l'API publique du composant, tels que specifies par RN-023 et le
Plan de reconstruction associe.

Rappel du principe fondamental (RN-023 par.2) :
    Le CapitalTransitionGuard n'est pas un controleur. Il ne pilote
    aucune strategie, ne poursuit aucune convergence, ne decide jamais
    d'une politique economique. Sa responsabilite est uniquement de
    garantir que toute transition economique appliquee au Bot respecte
    les invariants definis par RN-022.

Note sur le typage (evolution demandee apres relecture de l'etape 1) :
    Les concepts metier (valeur d'une demande, etat economique, valeur
    appliquee) sont representes par des types de domaine dedies plutot
    que par `Any`, afin de renforcer le typage sans introduire de
    logique. Ces types restent de simples conteneurs de donnees : ils
    ne portent aucune methode de calcul, de validation ou de
    conversion a ce stade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union, Mapping, Any
import time


# ============================================================
# ENUMERATIONS
# ============================================================

class TransitionCause(Enum):
    """
    Cause autorisee d'une transition economique (RN-023 par.6).

    Toute nouvelle cause devra etre definie par une Requirement Note
    dediee ; cette enumeration est donc volontairement fermee et ne
    doit pas etre etendue de maniere informelle lors de l'implementation.
    """
    REALIZED_PROFIT = "REALIZED_PROFIT"
    REALIZED_LOSS = "REALIZED_LOSS"
    META_CORRECTION = "META_CORRECTION"
    MANUAL_SYNC = "MANUAL_SYNC"


class TransitionOrigin(Enum):
    """
    Origine autorisee d'une demande de transition (RN-023 par.7).

    Chaque cause n'est compatible qu'avec une origine precise ; cette
    compatibilite sera verifiee a l'etape de validation (hors perimetre
    de cette etape 1).
    """
    BOT = "BOT"
    META_CONTROLLER = "META_CONTROLLER"
    OPERATOR = "OPERATOR"


class TransitionStatus(Enum):
    """
    Statut final d'une transition apres traitement par le Guard
    (RN-023 par.7, par.8).

    Les trois valeurs partagent la meme structure de resultat
    (TransitionResult), afin que l'appelant n'ait jamais a distinguer
    les cas par des types de retour differents.
    """
    ACCEPTED = "ACCEPTED"
    TRUNCATED = "TRUNCATED"
    REJECTED = "REJECTED"


# ============================================================
# TYPES DE DOMAINE - VALEUR D'UNE DEMANDE DE TRANSITION
# ============================================================

@dataclass(frozen=True)
class AbsoluteAmount:
    """
    Montant absolu, exprime dans l'unite de compte du bot (ex: USDC).

    Utilise pour les causes REALIZED_PROFIT, REALIZED_LOSS et
    MANUAL_SYNC, dont la valeur ne depend pas de l'etat courant au
    moment de l'application.

    Attributes:
        amount: Montant, positif ou negatif (une perte realisee est
            representee par un montant negatif).
    """
    amount: float


@dataclass(frozen=True)
class RelativeCorrection:
    """
    Regle de correction relative, exprimee en proportion de l'etat
    economique courant du bot.

    Utilisee pour la cause META_CORRECTION. Sa resolution en un montant
    concret (AppliedDelta) n'a pas lieu a cette etape : elle appartient
    a l'etape de resolution du plan de reconstruction, et doit
    s'effectuer contre l'etat lu au moment de l'application par le
    Guard, jamais contre l'etat qui prevalait a l'emission de la
    demande.

    Attributes:
        fraction: Proportion de correction souhaitee (ex: 0.05 pour
            +5%, -0.03 pour -3%). Aucune borne n'est imposee par ce
            type ; le bornage relevera des contraintes locales
            (etape ulterieure).
    """
    fraction: float


TransitionValue = Union[AbsoluteAmount, RelativeCorrection]


@dataclass(frozen=True)
class AppliedDelta:
    """
    Delta effectivement applique a l'etat economique d'un bot, tel que
    resolu par le Guard.

    Contrairement a TransitionValue (qui peut etre absolu ou relatif
    tel que demande), AppliedDelta est toujours un montant concret : la
    resolution d'une RelativeCorrection en AppliedDelta est le resultat
    d'un calcul qui n'a pas lieu a cette etape (aucune logique n'est
    implementee ici).

    Attributes:
        amount: Montant concret applique, positif ou negatif.
    """
    amount: float


# ============================================================
# TYPE DE DOMAINE - ETAT ECONOMIQUE
# ============================================================

@dataclass(frozen=True)
class EconomicState:
    """
    Etat economique d'un bot, tel que gere exclusivement par le
    CapitalTransitionGuard (RN-023 par.9).

    Aujourd'hui limite a allocated_capital ; RN-023 par.9 prevoit
    explicitement une extension future sans modification du contrat du
    Guard - cette classe est le point d'extension naturel (ajout de
    champs), plutot que le type Any utilise a titre provisoire dans la
    premiere version de ce squelette.

    Attributes:
        allocated_capital: Budget strategique vivant du bot (RN-022).
    """
    allocated_capital: float


# ============================================================
# DEMANDE DE TRANSITION
# ============================================================

@dataclass(frozen=True)
class TransitionRequest:
    """
    Demande de transition economique soumise au CapitalTransitionGuard
    (RN-023 par.6).

    Cette structure est volontairement neutre : elle ne prejuge pas de
    la validite de son contenu (aucune validation n'est effectuee a ce
    stade).

    Attributes:
        bot_id: Identifiant du bot concerne par la transition. Le Guard
            a une portee strictement locale a un bot.
        cause: Cause invoquee pour cette transition.
        origin: Origine declaree de l'emetteur de la demande.
        value: Valeur associee a la demande, typee selon qu'elle est
            absolue (AbsoluteAmount) ou relative (RelativeCorrection).
        requested_at: Horodatage d'emission de la demande par
            l'appelant (distinct de l'horodatage d'application, capture
            plus tard par le Guard).
        justification: Motif textuel. Optionnel pour la plupart des
            causes, mais destine a devenir obligatoire pour
            MANUAL_SYNC (regle de validation, hors perimetre de cette
            etape).
        metadata: Metadonnees additionnelles non structurees, reservees
            a un usage futur. Reste generique (Mapping[str, Any]) car
            il ne s'agit pas d'un concept de domaine a typer
            precisement a ce stade.
    """
    bot_id: str
    cause: TransitionCause
    origin: TransitionOrigin
    value: TransitionValue
    requested_at: float = field(default_factory=time.time)
    justification: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ============================================================
# RESULTAT D'UNE TRANSITION
# ============================================================

@dataclass(frozen=True)
class TransitionResult:
    """
    Resultat du traitement d'une TransitionRequest par le
    CapitalTransitionGuard (RN-023 par.7, par.8).

    Structure unique pour les trois statuts possibles (ACCEPTED,
    TRUNCATED, REJECTED), afin de garantir une interface stable pour
    l'appelant (Bot ou MetaController) et pour les tests d'architecture.

    Attributes:
        status: Statut final de la transition.
        requested_value: Valeur telle que demandee a l'origine.
        applied_value: Delta effectivement applique a l'etat
            economique, sous forme resolue (AppliedDelta). None si
            status == REJECTED.
        state_before: Etat economique du bot avant application.
            None si status == REJECTED.
        state_after: Etat economique du bot apres application.
            None si status == REJECTED.
        reason: Motif. None si status == ACCEPTED sans troncature ;
            renseigne obligatoirement pour TRUNCATED et REJECTED.
        applied_at: Horodatage de l'application par le Guard (distinct
            de TransitionRequest.requested_at).
    """
    status: TransitionStatus
    requested_value: TransitionValue
    applied_value: Optional[AppliedDelta]
    state_before: Optional[EconomicState]
    state_after: Optional[EconomicState]
    reason: Optional[str]
    applied_at: float = field(default_factory=time.time)


# ============================================================
# ENTREE DE JOURNAL
# ============================================================

@dataclass(frozen=True)
class CapitalTransitionJournalEntry:
    """
    Entree de journal correspondant au traitement d'une
    TransitionRequest (RN-023 par.10).

    Chaque appel au point d'entree du Guard doit produire exactement
    une entree de ce type, quel que soit le statut obtenu.

    La structure exacte de persistance (fichier JSONL, base, autre)
    est laissee a une etape ulterieure ; cette classe ne represente que
    la forme logique de l'entree, independamment de son stockage.

    Attributes:
        bot_id: Identifiant du bot concerne.
        cause: Cause de la transition.
        origin: Origine declaree de la demande.
        status: Statut final (ACCEPTED, TRUNCATED, REJECTED).
        requested_value: Valeur demandee a l'origine.
        applied_value: Delta effectivement applique (None si rejetee).
        state_before: Etat economique avant application.
        state_after: Etat economique apres application.
        reason: Motif de refus ou de troncature (None si acceptee sans
            troncature).
        requested_at: Horodatage d'emission de la demande.
        applied_at: Horodatage de traitement par le Guard.
    """
    bot_id: str
    cause: TransitionCause
    origin: TransitionOrigin
    status: TransitionStatus
    requested_value: TransitionValue
    applied_value: Optional[AppliedDelta]
    state_before: Optional[EconomicState]
    state_after: Optional[EconomicState]
    reason: Optional[str]
    requested_at: float
    applied_at: float


# ============================================================
# SQUELETTE DU CAPITALTRANSITIONGUARD
# ============================================================

class CapitalTransitionGuard:
    """
    Gardien exclusif des transitions de l'etat economique d'un Grid Bot
    (RN-023).

    Squelette d'etape 1 : aucune methode n'est implementee a ce stade.
    Aucune logique de validation, de resolution, de persistance ou de
    journalisation ne doit etre ajoutee avant les etapes suivantes du
    plan de reconstruction.

    Rappel des non-responsabilites (RN-023 par.5) : ce composant ne
    calcule jamais le GOI, ne produit jamais une correction
    strategique, ne calcule jamais un profit ou une perte, ne decide
    jamais de la politique economique, ne pilote jamais le
    portefeuille, ne connait jamais la strategie de trading, ne
    poursuit jamais une valeur cible, et ne conserve aucun etat de
    convergence entre deux cycles.
    """

    def submit_transition(self, request: TransitionRequest) -> TransitionResult:
        """
        Point d'entree unique de soumission d'une demande de transition
        (RN-023 par.3).

        Etape 1 : non implementee.
        """
        raise NotImplementedError(
            "submit_transition sera implementee aux etapes suivantes "
            "du plan de reconstruction (validation, resolution, "
            "persistance, journalisation, contraintes locales)."
        )

    def get_current_state(self, bot_id: str) -> EconomicState:
        """
        Lecture seule de l'etat economique courant d'un bot
        (ex: allocated_capital), sans effet de bord.

        Etape 1 : non implementee.
        """
        raise NotImplementedError(
            "get_current_state sera implementee a l'etape de persistance."
        )

    def get_history(self, bot_id: str) -> list[CapitalTransitionJournalEntry]:
        """
        Consultation de l'historique des transitions d'un bot, pour
        reconstruction et audit (RN-023 par.10, garantie G5).

        Etape 1 : non implementee.
        """
        raise NotImplementedError(
            "get_history sera implementee a l'etape de persistance/journalisation."
        )

