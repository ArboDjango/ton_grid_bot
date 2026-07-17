"""
capital_transition_guard.py

Modele de donnees du CapitalTransitionGuard (RN-023), conforme aux
principes de RN-022 (Budget Strategique Vivant).

Etape 1 du plan de reconstruction : fondations du domaine.
Etape 2 du plan de reconstruction : validation pure des demandes de
transition (validate_transition_request).
Etape 3 du plan de reconstruction : resolution pure des valeurs de
transition contre un etat economique donne (resolve_transition_value).
Etape 4 du plan de reconstruction (partielle, perimetre restreint a la
journalisation logique) : CapitalTransitionJournal, un composant
d'enregistrement en memoire des CapitalTransitionJournalEntry.
Etape 6 du plan de reconstruction : CapitalTransitionGuard devient un
orchestrateur complet de submit_transition, deleguant chaque etape aux
composants deja construits (validation, resolution, application,
persistance via EconomicStateRepositoryProtocol, journalisation).

(La persistance elle-meme, EconomicStateRepository, vit dans le module
economic_state_repository.py, etape 5 du plan de reconstruction — non
importee ici afin que ce module de domaine reste independant de toute
infrastructure de fichiers.)

Ce module contient desormais la validation structurelle des
TransitionRequest, la resolution pure d'une TransitionValue en
AppliedDelta, une fonction pure d'application d'un delta a un etat
(apply_delta), un journal logique en memoire, un port de persistance
(EconomicStateRepositoryProtocol) et l'orchestrateur complet
CapitalTransitionGuard.submit_transition. Ne contiennent toujours
aucune logique metier propre au Guard :
  - aucun calcul economique n'est effectue par le Guard lui-meme (tout
    calcul est delegue a resolve_transition_value et apply_delta) ;
  - aucune decision de validation n'est prise par le Guard (deleguee a
    validate_transition_request) ;
  - aucune connaissance du MetaController, de bot_gateio.py ou de
    Gate.io.

get_current_state et get_history sont desormais implementees comme
de pures delegations, respectivement a self._repository.load(bot_id)
et self._journal.history_for(bot_id). Aucune logique metier n'y a ete
ajoutee : ce sont des points d'acces publics vers Repository et
Journal, rien de plus.

Il definit les structures de donnees, la validation pure, la
resolution pure, le journal logique, le port de persistance, et
desormais l'orchestrateur complet (y compris ses points d'acces en
lecture), tels que specifies par RN-023 et le Plan de reconstruction
associe.

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
from typing import Optional, Union, Mapping, Any, Protocol
import math
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
# VALIDATION PURE D'UNE DEMANDE DE TRANSITION (ETAPE 2)
# ============================================================
#
# Cette section ne resout aucune RelativeCorrection, ne persiste rien,
# ne journalise rien et ne modifie aucun etat economique. Elle verifie
# uniquement que la demande est structurellement coherente, avant
# qu'une quelconque decision d'application ne soit envisagee.

@dataclass(frozen=True)
class ValidationOutcome:
    """
    Resultat d'une validation structurelle pure d'une TransitionRequest.

    Ce type est distinct de TransitionStatus : une ValidationOutcome
    ne se prononce que sur la coherence formelle de la demande (est-
    elle bien formee ?), jamais sur la decision d'application (sera-
    t-elle acceptee, tronquee ou rejetee au sens du Guard ?). Cette
    derniere decision appartient aux etapes ulterieures du plan de
    reconstruction (resolution, contraintes locales).

    Attributes:
        is_valid: True si la demande est structurellement valide.
        reason: Motif du premier echec de validation rencontre.
            None si is_valid est True.
    """
    is_valid: bool
    reason: Optional[str] = None


# Origines compatibles avec chaque cause (RN-023 par.7). Cette table
# est une donnee de reference statique, pas un calcul ni une decision
# economique : elle ne fait qu'exprimer sous forme verifiable la regle
# deja enoncee par RN-023 (une cause n'est legitime que si son origine
# declaree correspond a l'emetteur autorise pour cette cause).
_AUTHORIZED_ORIGINS_BY_CAUSE: Mapping[TransitionCause, frozenset]= {
    TransitionCause.REALIZED_PROFIT: frozenset({TransitionOrigin.BOT}),
    TransitionCause.REALIZED_LOSS: frozenset({TransitionOrigin.BOT}),
    TransitionCause.META_CORRECTION: frozenset({TransitionOrigin.META_CONTROLLER}),
    TransitionCause.MANUAL_SYNC: frozenset({TransitionOrigin.OPERATOR}),
}

# Type de valeur structurellement attendu pour chaque cause. Un profit
# ou une perte realises, ainsi qu'une synchronisation exceptionnelle,
# portent un montant absolu ; une correction strategique porte une
# regle relative, non resolue a ce stade.
_EXPECTED_VALUE_TYPE_BY_CAUSE = {
    TransitionCause.REALIZED_PROFIT: AbsoluteAmount,
    TransitionCause.REALIZED_LOSS: AbsoluteAmount,
    TransitionCause.MANUAL_SYNC: AbsoluteAmount,
    TransitionCause.META_CORRECTION: RelativeCorrection,
}


def _is_finite_number(value: Any) -> bool:
    """Verifie qu'une valeur est un nombre fini (ni NaN, ni infini)."""
    if isinstance(value, bool):
        # bool est une sous-classe d'int en Python ; on l'exclut
        # explicitement pour eviter qu'un booleen ne soit accepte
        # silencieusement comme un montant.
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def validate_transition_request(request: TransitionRequest) -> ValidationOutcome:
    """
    Valide la coherence structurelle d'une TransitionRequest.

    Fonction pure : aucun effet de bord, aucune lecture ni ecriture de
    persistance, aucune resolution de RelativeCorrection, aucune
    journalisation. A entrees identiques, retourne toujours le meme
    resultat.

    Verifications effectuees, dans cet ordre (la premiere verification
    en echec determine le motif retourne) :
        1. bot_id est une chaine non vide (hors espaces).
        2. cause est bien une instance de TransitionCause.
        3. origin est bien une instance de TransitionOrigin.
        4. origin est autorisee pour la cause declaree (RN-023 par.7).
        5. value est du type structurellement attendu pour cette cause
           (AbsoluteAmount ou RelativeCorrection selon le cas).
        6. Le champ numerique porte par value est un nombre fini.
        7. Si cause == MANUAL_SYNC, justification doit etre une chaine
           non vide (hors espaces) — la synchronisation exceptionnelle
           doit toujours etre motivee (RN-022).

    Cette fonction ne determine jamais si la transition doit etre
    appliquee, tronquee ou rejetee par le Guard (TransitionStatus) :
    elle etablit seulement si la demande est exploitable par les
    etapes ulterieures.

    Args:
        request: La demande de transition a valider.

    Returns:
        ValidationOutcome(is_valid=True) si toutes les verifications
        passent, sinon ValidationOutcome(is_valid=False, reason=...)
        avec le motif du premier echec rencontre.
    """
    if not isinstance(request.bot_id, str) or request.bot_id.strip() == "":
        return ValidationOutcome(
            is_valid=False,
            reason="bot_id doit etre une chaine non vide.",
        )

    if not isinstance(request.cause, TransitionCause):
        return ValidationOutcome(
            is_valid=False,
            reason="cause doit etre une instance de TransitionCause.",
        )

    if not isinstance(request.origin, TransitionOrigin):
        return ValidationOutcome(
            is_valid=False,
            reason="origin doit etre une instance de TransitionOrigin.",
        )

    authorized_origins = _AUTHORIZED_ORIGINS_BY_CAUSE[request.cause]
    if request.origin not in authorized_origins:
        return ValidationOutcome(
            is_valid=False,
            reason=(
                f"origine '{request.origin.value}' non autorisee pour "
                f"la cause '{request.cause.value}'."
            ),
        )

    expected_value_type = _EXPECTED_VALUE_TYPE_BY_CAUSE[request.cause]
    if not isinstance(request.value, expected_value_type):
        return ValidationOutcome(
            is_valid=False,
            reason=(
                f"la cause '{request.cause.value}' requiert une valeur de "
                f"type {expected_value_type.__name__}."
            ),
        )

    if isinstance(request.value, AbsoluteAmount):
        numeric_value = request.value.amount
    else:
        numeric_value = request.value.fraction

    if not _is_finite_number(numeric_value):
        return ValidationOutcome(
            is_valid=False,
            reason="la valeur numerique de la demande doit etre un nombre fini.",
        )

    if request.cause is TransitionCause.MANUAL_SYNC:
        if (
            not isinstance(request.justification, str)
            or request.justification.strip() == ""
        ):
            return ValidationOutcome(
                is_valid=False,
                reason=(
                    "une synchronisation exceptionnelle (MANUAL_SYNC) doit "
                    "obligatoirement porter une justification non vide."
                ),
            )

    return ValidationOutcome(is_valid=True)


# ============================================================
# RESOLUTION PURE D'UNE VALEUR DE TRANSITION (ETAPE 3)
# ============================================================
#
# Cette section ne persiste rien, ne journalise rien et n'ecrit aucun
# etat economique. Elle transforme uniquement une TransitionValue deja
# structurellement valide en un AppliedDelta concret, contre un
# EconomicState explicitement fourni par l'appelant.

def resolve_transition_value(
    value: TransitionValue,
    current_state: EconomicState,
) -> AppliedDelta:
    """
    Resout une TransitionValue en un AppliedDelta concret.

    Fonction pure : aucun effet de bord, aucune lecture ni ecriture de
    persistance, aucune journalisation. A entrees identiques, retourne
    toujours le meme resultat.

    Deux cas, selon le type de `value` :
      - AbsoluteAmount : deja un montant concret ; resolu tel quel,
        independamment de `current_state` (un profit realise, une
        perte realisee ou une synchronisation exceptionnelle ne
        dependent pas de l'etat courant pour etre resolus).
      - RelativeCorrection : regle relative, resolue en multipliant sa
        fraction par `current_state.allocated_capital`. Conformement a
        la clarification de RN-023, cette resolution s'effectue
        contre l'EconomicState fourni ici et uniquement celui-ci :
        c'est a l'appelant de garantir qu'il s'agit bien de l'etat lu
        au moment de l'application, et non d'un etat fige au moment de
        l'emission de la demande. Cette fonction ne lit aucun etat par
        elle-meme et ne met rien en cache.

    Cette fonction ne verifie pas la validite structurelle de `value`
    (role de validate_transition_request, etape 2) et ne decide pas si
    le delta resolu sera applique, tronque ou rejete (role des etapes
    ulterieures : contraintes locales, application par le Guard).

    Args:
        value: La valeur de transition a resoudre (AbsoluteAmount ou
            RelativeCorrection).
        current_state: L'etat economique contre lequel resoudre une
            eventuelle regle relative.

    Returns:
        AppliedDelta representant le montant concret resolu.

    Raises:
        TypeError: si `value` n'est ni un AbsoluteAmount ni un
            RelativeCorrection. Il ne s'agit pas d'un rejet metier
            (celui-ci est du ressort de la validation, etape 2) mais
            d'une erreur de programmation appelant cette fonction en
            dehors de son contrat.
    """
    if isinstance(value, AbsoluteAmount):
        return AppliedDelta(amount=value.amount)

    if isinstance(value, RelativeCorrection):
        resolved_amount = value.fraction * current_state.allocated_capital
        return AppliedDelta(amount=resolved_amount)

    raise TypeError(
        f"Type de TransitionValue non pris en charge : {type(value).__name__}. "
        "resolve_transition_value attend un AbsoluteAmount ou un "
        "RelativeCorrection."
    )


def apply_delta(state: EconomicState, delta: AppliedDelta) -> EconomicState:
    """
    Applique un AppliedDelta deja resolu a un EconomicState, produisant
    le nouvel etat qui en resulte.

    Fonction pure : ne modifie ni `state` ni `delta` (tous deux
    immuables), ne valide rien, ne decide rien. Elle exprime
    uniquement, sous forme d'un nouvel EconomicState, la consequence
    mecanique de l'application d'un delta deja resolu — jamais le
    calcul de ce delta lui-meme (role de resolve_transition_value) ni
    la decision de savoir s'il doit etre applique (role des etapes de
    validation et de contraintes locales).

    Cette fonction existe pour que CapitalTransitionGuard n'ait lui-
    meme aucune arithmetique a effectuer : l'orchestrateur se contente
    d'appeler cette fonction, sans jamais manipuler `allocated_capital`
    directement.

    Args:
        state: L'etat economique courant.
        delta: Le delta deja resolu a appliquer.

    Returns:
        Un nouvel EconomicState reflétant l'application du delta.
    """
    return EconomicState(allocated_capital=state.allocated_capital + delta.amount)


# ============================================================
# PORT DE PERSISTANCE (INVERSION DE DEPENDANCE)
# ============================================================
#
# Ce Protocol decrit uniquement l'interface attendue par
# CapitalTransitionGuard pour la persistance d'EconomicState. Il ne
# connait aucune implementation concrete : le module de domaine reste
# ainsi independant de toute infrastructure (aucun import de json,
# pathlib, os, ou de EconomicStateRepository dans ce fichier). C'est
# EconomicStateRepository (economic_state_repository.py) qui satisfait
# structurellement ce Protocol, sans qu'aucun des deux modules n'ait
# besoin d'importer l'autre au niveau du domaine.

class EconomicStateRepositoryProtocol(Protocol):
    """
    Port de persistance attendu par CapitalTransitionGuard.

    Toute implementation fournissant ces deux methodes avec cette
    signature peut etre injectee dans le Guard, qu'elle vive dans ce
    projet ou ailleurs.
    """

    def load(self, bot_id: str) -> EconomicState:
        """Lit l'EconomicState courant d'un bot."""
        ...

    def save(self, bot_id: str, state: EconomicState) -> None:
        """Persiste un EconomicState pour un bot."""
        ...


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
# JOURNAL LOGIQUE (ETAPE 4, PERIMETRE RESTREINT)
# ============================================================
#
# Ce composant ne persiste rien sur disque ou support externe (role
# reserve au futur Repository). Il ne lit ni ne modifie aucun etat
# economique. Il ne construit aucune CapitalTransitionJournalEntry :
# il se contente de recevoir des entrees deja entierement formees et
# de les rendre consultables par bot, dans leur ordre d'enregistrement.

class CapitalTransitionJournal:
    """
    Journal logique, en memoire, des CapitalTransitionJournalEntry.

    Responsabilite unique : enregistrer des entrees deja construites
    et permettre leur consultation ulterieure par bot (RN-023 par.10,
    garantie G5 - auditabilite). Ce composant :

      - ne construit aucune entree lui-meme (role des etapes de
        validation, resolution et decision d'application, qui
        produisent une CapitalTransitionJournalEntry complete avant
        de la soumettre a ce journal) ;
      - ne valide pas le contenu des entrees recues (role de
        validate_transition_request, etape 2) ;
      - ne decide rien (aucune notion d'acceptation, de troncature ou
        de rejet n'est evaluee ici : le journal enregistre indifferemment
        les trois statuts) ;
      - ne lit ni ne modifie aucun EconomicState ;
      - ne persiste rien en dehors de la memoire du processus courant.
        L'ecriture vers un support durable (fichier JSONL, base, etc.)
        est le role d'un futur Repository, explicitement hors
        perimetre de cette etape.

    Ce composant conserve un etat interne (les entrees accumulees),
    ce qui ne contredit pas la garantie de statelessness du
    CapitalTransitionGuard (RN-023 par.11, G4) : cette derniere porte
    sur l'absence de memoire de decision ou de convergence, pas sur
    l'absence d'un historique append-only, que RN-023 exige au
    contraire explicitement (par.10).
    """

    def __init__(self) -> None:
        self._entries_by_bot: dict[str, list[CapitalTransitionJournalEntry]] = {}

    def record(self, entry: CapitalTransitionJournalEntry) -> None:
        """
        Enregistre une entree de journal deja construite.

        Ne modifie ni ne lit aucun EconomicState. N'evalue ni ne
        transforme le contenu de l'entree : elle est stockee telle
        quelle, a la suite des entrees deja enregistrees pour le meme
        bot.

        Args:
            entry: L'entree deja entierement construite a enregistrer.

        Raises:
            TypeError: si `entry` n'est pas une instance de
                CapitalTransitionJournalEntry. Il ne s'agit pas d'un
                rejet metier mais d'une erreur de programmation
                appelant cette methode en dehors de son contrat.
        """
        if not isinstance(entry, CapitalTransitionJournalEntry):
            raise TypeError(
                "CapitalTransitionJournal.record attend une instance de "
                f"CapitalTransitionJournalEntry, recu : {type(entry).__name__}."
            )
        self._entries_by_bot.setdefault(entry.bot_id, []).append(entry)

    def history_for(self, bot_id: str) -> list[CapitalTransitionJournalEntry]:
        """
        Retourne l'historique des entrees enregistrees pour un bot,
        dans leur ordre d'enregistrement.

        Retourne une copie defensive : toute modification de la liste
        retournee n'affecte pas l'etat interne du journal.

        Args:
            bot_id: Identifiant du bot dont on souhaite l'historique.

        Returns:
            Liste des CapitalTransitionJournalEntry enregistrees pour
            ce bot, dans l'ordre d'enregistrement. Liste vide si aucune
            entree n'a ete enregistree pour ce bot.
        """
        return list(self._entries_by_bot.get(bot_id, []))


# ============================================================
# SQUELETTE DU CAPITALTRANSITIONGUARD
# ============================================================

# ============================================================
# CAPITALTRANSITIONGUARD — ORCHESTRATEUR (ETAPE 6)
# ============================================================

class CapitalTransitionGuard:
    """
    Gardien exclusif des transitions de l'etat economique d'un Grid Bot
    (RN-023).

    Ce composant est un pur orchestrateur : il ne contient aucune
    logique metier propre. Chaque etape du flux (validation,
    resolution, application, persistance, journalisation) est deleguee
    a un composant deja construit et deja teste independamment
    (validate_transition_request, resolve_transition_value,
    apply_delta, EconomicStateRepositoryProtocol,
    CapitalTransitionJournal). Le Guard se contente d'appeler ces
    composants dans le bon ordre et de transporter leurs resultats.

    Flux de submit_transition, en cas de demande structurellement
    valide :

        TransitionRequest
              |
              v
        validate_transition_request  (deja teste independamment)
              |
              v
        repository.load              (lecture de l'etat courant)
              |
              v
        resolve_transition_value     (deja teste independamment)
              |
              v
        apply_delta                  (deja teste independamment)
              |
              v
        journal.record               (ecrit AVANT la persistance de
              |                       l'etat — cf. note sur l'ordre
              |                       d'ecriture ci-dessous)
              v
        repository.save
              |
              v
        TransitionResult

    Si la demande est structurellement invalide, seule la validation
    et la journalisation ont lieu : ni le Repository ni le Resolver ne
    sont sollicites (RN-023 : en cas d'echec, aucune modification
    economique n'est appliquee).

    Note sur l'ordre d'ecriture (journal avant etat) :
        Le journal est ecrit avant que le nouvel etat ne soit persiste.
        Si repository.save() echoue apres que journal.record() a
        reussi, l'entree de journal refletera une transition qui n'a
        finalement pas ete persistee. Ce risque residuel est connu et
        assume : il correspond a la decision d'implementation prise
        lors de la revue de faisabilite de RN-023 (le journal fait foi
        en cas d'incoherence ; une reconciliation au demarrage,
        comparant la derniere entree de journal a l'etat persiste,
        reste a implementer dans une etape ulterieure dediee a la
        persistance complete). Ce composant ne masque jamais cette
        situation : si repository.save() leve une exception, elle
        remonte telle quelle a l'appelant.

    Gestion des erreurs :
        Le Guard ne capture jamais d'exception. Une erreur levee par
        repository.load(), resolve_transition_value() ou
        repository.save() remonte sans etre transformee ni masquee.
        Seule la validation structurelle (validate_transition_request)
        produit un resultat REJECTED "normal" (un cas prevu par
        RN-023, pas une defaillance) ; toute autre erreur signale une
        situation exceptionnelle (bug, panne d'infrastructure) qui ne
        doit jamais etre deguisee en resultat metier.

    Rappel des non-responsabilites (RN-023 par.5), inchangees par
    cette etape : ce composant ne calcule jamais le GOI, ne produit
    jamais une correction strategique, ne decide jamais de la
    politique economique, ne pilote jamais le portefeuille, ne connait
    jamais la strategie de trading, le MetaController, bot_gateio.py ou
    Gate.io, et ne poursuit jamais une valeur cible.

    Les contraintes locales (troncature d'une correction, planchers
    economiques) restent hors perimetre de cette etape : seuls les
    statuts ACCEPTED (transition structurellement valide) et REJECTED
    (echec de validation) peuvent etre produits ici. TRUNCATED sera
    introduit lors de l'etape dediee aux contraintes locales.
    """

    def __init__(
        self,
        repository: EconomicStateRepositoryProtocol,
        journal: CapitalTransitionJournal,
    ) -> None:
        """
        Args:
            repository: Port de persistance d'EconomicState (voir
                EconomicStateRepositoryProtocol). Toute implementation
                satisfaisant structurellement ce protocole convient
                (ex: EconomicStateRepository).
            journal: Journal logique dans lequel chaque transition
                traitee est enregistree.
        """
        self._repository = repository
        self._journal = journal

    def submit_transition(self, request: TransitionRequest) -> TransitionResult:
        """
        Point d'entree unique de soumission d'une demande de transition
        (RN-023 par.3).

        Orchestration pure : voir le flux decrit dans la docstring de
        la classe. Aucune decision economique, aucune validation,
        aucune resolution et aucune ecriture de fichier n'ont lieu dans
        cette methode elle-meme — toutes sont deleguees aux composants
        injectes ou importes.

        Args:
            request: La demande de transition a traiter.

        Returns:
            Le TransitionResult decrivant l'issue du traitement
            (ACCEPTED ou REJECTED a ce stade du plan de reconstruction).

        Raises:
            Toute exception levee par repository.load(),
            resolve_transition_value() ou repository.save() est
            propagee telle quelle, sans etre capturee ni transformee.
        """
        validation = validate_transition_request(request)
        if not validation.is_valid:
            return self._reject(request, validation.reason)

        current_state = self._repository.load(request.bot_id)
        applied_delta = resolve_transition_value(request.value, current_state)
        new_state = apply_delta(current_state, applied_delta)

        result = TransitionResult(
            status=TransitionStatus.ACCEPTED,
            requested_value=request.value,
            applied_value=applied_delta,
            state_before=current_state,
            state_after=new_state,
            reason=None,
        )

        # Le journal est ecrit avant la persistance de l'etat (cf. note
        # d'ordre d'ecriture dans la docstring de la classe).
        self._journal.record(self._build_journal_entry(request, result))
        self._repository.save(request.bot_id, new_state)

        return result

    def get_current_state(self, bot_id: str) -> EconomicState:
        """
        Lecture seule de l'etat economique courant d'un bot
        (ex: allocated_capital), sans effet de bord.

        Delegation pure a self._repository.load(bot_id). Aucune
        logique metier, aucune validation, aucun calcul, aucune
        modification d'etat : le Guard se contente de transmettre
        l'appel et de retourner le resultat tel quel.

        Args:
            bot_id: Identifiant du bot dont on souhaite l'etat courant.

        Returns:
            L'EconomicState tel que retourne par le Repository.

        Raises:
            Toute exception levee par self._repository.load() est
            propagee telle quelle, sans etre capturee ni transformee
            (ex: FileNotFoundError si aucun etat n'est persiste pour ce
            bot).
        """
        return self._repository.load(bot_id)

    def get_history(self, bot_id: str) -> list[CapitalTransitionJournalEntry]:
        """
        Consultation de l'historique des transitions d'un bot, pour
        reconstruction et audit (RN-023 par.10, garantie G5).

        Delegation pure a self._journal.history_for(bot_id). Aucune
        logique metier, aucun filtrage, aucune transformation : le
        Guard se contente de transmettre l'appel et de retourner le
        resultat tel quel.

        Args:
            bot_id: Identifiant du bot dont on souhaite l'historique.

        Returns:
            La liste des CapitalTransitionJournalEntry telle que
            retournee par le Journal (deja une copie defensive,
            garantie par CapitalTransitionJournal.history_for).
        """
        return self._journal.history_for(bot_id)

    def _reject(self, request: TransitionRequest, reason: str) -> TransitionResult:
        """
        Construit et journalise un TransitionResult REJECTED, sans
        toucher au Repository (RN-023 : un echec de validation
        n'entraine aucune modification economique).

        Methode privee d'orchestration : ne decide de rien elle-meme,
        se contente d'assembler le resultat REJECTED a partir du motif
        deja produit par validate_transition_request, et de le
        journaliser.
        """
        result = TransitionResult(
            status=TransitionStatus.REJECTED,
            requested_value=request.value,
            applied_value=None,
            state_before=None,
            state_after=None,
            reason=reason,
        )
        self._journal.record(self._build_journal_entry(request, result))
        return result

    @staticmethod
    def _build_journal_entry(
        request: TransitionRequest,
        result: TransitionResult,
    ) -> CapitalTransitionJournalEntry:
        """
        Assemble une CapitalTransitionJournalEntry a partir de la
        demande d'origine et du resultat obtenu.

        Methode privee, purement mecanique : ne decide de rien, ne
        calcule rien, se contente de recopier les champs deja produits
        par la demande et par le resultat.
        """
        return CapitalTransitionJournalEntry(
            bot_id=request.bot_id,
            cause=request.cause,
            origin=request.origin,
            status=result.status,
            requested_value=result.requested_value,
            applied_value=result.applied_value,
            state_before=result.state_before,
            state_after=result.state_after,
            reason=result.reason,
            requested_at=request.requested_at,
            applied_at=result.applied_at,
        )
