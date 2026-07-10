"""
shared_types.py

Types partagés entre les différents modules du MetaController.
"""

from enum import Enum


class RunMode(Enum):
    """Mode d'exécution du MetaController."""
    OBSERVE = "observe"
    SIMULATE = "simulate"  # alias pour DRY_RUN
    EXECUTE = "execute"

    @property
    def is_execution_mode(self) -> bool:
        return self in (RunMode.SIMULATE, RunMode.EXECUTE)
