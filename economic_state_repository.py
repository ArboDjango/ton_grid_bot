"""
economic_state_repository.py

EconomicStateRepository — étape 5 du plan de reconstruction du
CapitalTransitionGuard (RN-022 / RN-023).

Responsabilité strictement limitée : convertir un EconomicState vers
et depuis sa représentation JSON persistée sur disque, un bot à la
fois.

Ce module ne contient :
  - aucune validation (ni du contenu du JSON lu, ni de l'EconomicState
    écrit) ;
  - aucune décision (rien n'est accepté, tronqué ou rejeté ici) ;
  - aucune journalisation ;
  - aucune logique métier (aucun calcul, aucune résolution de
    RelativeCorrection, aucune interprétation économique) ;
  - aucune connaissance du MetaController (aucun import, aucune
    référence à TransitionCause, TransitionOrigin, GOI, correction
    stratégique, etc.) ;
  - aucune connaissance du Bot (aucune référence à l'inventaire, la
    grille, le PnL, la stratégie de trading — bot_id n'est utilisé que
    comme identifiant opaque de nommage de fichier).

Ce Repository ne comble volontairement aucune valeur manquante et ne
masque aucune erreur : un fichier absent, illisible, ou dont le
contenu ne correspond pas à la structure attendue d'EconomicState
lève une exception plutôt que d'être traité silencieusement (à la
différence du défaut identifié dans l'ancien CapitalTargetController,
qui ignorait silencieusement un fichier corrompu ou absent). Décider
de la conduite à tenir face à une telle erreur (créer un état par
défaut, alerter, arrêter le cycle, etc.) est une décision qui
n'appartient pas à ce composant.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Mapping

from capital_transition_guard import EconomicState


# ============================================================
# CONVERSION PURE JSON <-> ECONOMICSTATE
# ============================================================

def economic_state_to_dict(state: EconomicState) -> dict:
    """
    Convertit un EconomicState en dictionnaire sérialisable en JSON.

    Fonction pure : aucun effet de bord, aucune lecture ni écriture
    sur disque. Générique vis-à-vis des champs d'EconomicState (utilise
    dataclasses.asdict), afin qu'une future extension d'EconomicState
    (RN-023 §9) n'exige pas de modification de cette fonction.

    Args:
        state: L'état économique à convertir.

    Returns:
        Un dictionnaire dont les clés correspondent exactement aux
        champs d'EconomicState.
    """
    return dataclasses.asdict(state)


def economic_state_from_dict(data: Mapping) -> EconomicState:
    """
    Convertit un dictionnaire (typiquement issu d'un json.load) en
    EconomicState.

    Fonction pure : aucun effet de bord. Ne valide pas la plausibilité
    économique des valeurs reçues (par exemple un allocated_capital
    négatif ou non fini n'est pas rejeté ici — ce n'est pas le rôle de
    ce composant). Exige en revanche une correspondance stricte avec
    les champs d'EconomicState : un champ manquant ou un champ
    surnuméraire lève une TypeError, plutôt que d'être ignoré ou
    complété silencieusement.

    Args:
        data: Dictionnaire dont les clés doivent correspondre
            exactement aux champs d'EconomicState.

    Returns:
        L'EconomicState correspondant.

    Raises:
        TypeError: si `data` ne contient pas exactement les champs
            attendus par EconomicState (champ manquant ou en trop).
    """
    return EconomicState(**data)


# ============================================================
# REPOSITORY — LECTURE / ECRITURE SUR DISQUE
# ============================================================

class EconomicStateRepository:
    """
    Repository de persistance d'EconomicState, un bot a la fois.

    Convertit exclusivement entre JSON et EconomicState. Ne connaît ni
    le MetaController, ni le Bot, ni aucune notion de transition, de
    cause, d'origine ou de décision : bot_id n'est utilisé que comme
    identifiant opaque pour nommer le fichier de persistance associé.

    Le fichier de persistance est distinct du fichier d'état global du
    bot (state_{exchange}_{symbol}.json) : il s'agit d'un fichier dédié
    à ce Repository, dont le contenu se limite à la représentation
    d'EconomicState. Le rapprochement éventuel avec le fichier d'état
    plus large du bot est hors périmètre de ce composant.
    """

    def __init__(self, state_dir: str = ".") -> None:
        """
        Args:
            state_dir: Répertoire dans lequel les fichiers de
                persistance sont lus et écrits.
        """
        self._state_dir = Path(state_dir)

    def _path_for(self, bot_id: str) -> Path:
        """Chemin du fichier de persistance associé à un bot."""
        return self._state_dir / f"capital_state_{bot_id}.json"

    def load(self, bot_id: str) -> EconomicState:
        """
        Lit et convertit l'EconomicState persisté pour un bot.

        Args:
            bot_id: Identifiant opaque du bot concerné.

        Returns:
            L'EconomicState lu depuis le fichier de persistance.

        Raises:
            FileNotFoundError: si aucun fichier de persistance n'existe
                pour ce bot_id. Ce composant n'invente aucune valeur
                par défaut : décider de la conduite à tenir face à
                l'absence d'état persisté est une décision qui
                n'appartient pas au Repository.
            json.JSONDecodeError: si le fichier existe mais ne contient
                pas un JSON valide.
            TypeError: si le contenu JSON ne correspond pas exactement
                à la structure attendue d'EconomicState.
        """
        path = self._path_for(bot_id)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return economic_state_from_dict(data)

    def save(self, bot_id: str, state: EconomicState) -> None:
        """
        Persiste un EconomicState pour un bot.

        L'écriture est atomique (fichier temporaire puis
        os.replace()), conformément à la convention déjà en usage dans
        le projet pour la persistance d'état.

        Args:
            bot_id: Identifiant opaque du bot concerné.
            state: L'état économique à persister.
        """
        path = self._path_for(bot_id)
        data = economic_state_to_dict(state)

        tmp_path = path.with_name(path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
