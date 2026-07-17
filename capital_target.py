"""
capital_target.py

Gestion du fichier de contrôle envoyé par le MetaController.
Responsabilité : lire capital_target, calculer un ratio d'ajustement
et le faire converger progressivement vers la cible.
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class CapitalTargetController:
    """
    Contrôleur de la cible de capital.

    Lit le fichier control_{symbol}.json à intervalle régulier,
    maintient un ratio d'ajustement (capital_ratio) qui tend vers
    capital_target / capital_for_grid, avec un pas maximal configurable.
    """

    def __init__(
        self,
        symbol: str,
        state_dir: str = ".",
        check_interval: float = 30.0,
        deadband: float = 0.05,          # nouveau paramètre
        min_ratio: float = 0.5,
        max_ratio: float = 2.0,
    ):
        self.symbol = symbol.lower()
        self.state_dir = Path(state_dir)
        self.control_path = self.state_dir / f"control_{self.symbol}.json"
        self.check_interval = check_interval
        self.deadband = deadband          # stockage
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio

        self.current_target: Optional[float] = None
        self.last_read_time: float = 0.0
        self.capital_ratio: float = 1.0
        self.state: str = "HOLD"          # nouvel attribut

    def _read_control_file(self) -> Optional[float]:
        """Lit le fichier de contrôle et retourne capital_target, ou None."""
        if not self.control_path.exists():
            return None
        try:
            with self.control_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            target = data.get("capital_target")
            if target is not None:
                return float(target)
        except (json.JSONDecodeError, ValueError, OSError):
            # Fichier corrompu ou illisible : on ignore
            pass
        return None

    def update(self, current_capital: float) -> None:
        """
        Met à jour le ratio d'ajustement.

        Doit être appelée à chaque cycle (ou avant chaque ordre).
        La lecture du fichier est limitée par check_interval.
        """
        now = time.time()
        if now - self.last_read_time >= self.check_interval:
            self.last_read_time = now
            target = self._read_control_file()
            if target is not None and target > 0:
                self.current_target = target

        if self.current_target is None or self.current_target <= 0 or current_capital <= 0:
            self.capital_ratio = 1.0
            self.state = "HOLD"
            return

        target = self.current_target
        ratio = target / current_capital
        self.capital_ratio = max(self.min_ratio, min(self.max_ratio, ratio))

        # Calcul de l'état (zone morte)
        error_pct = (current_capital - target) / target
        if abs(error_pct) <= self.deadband:
            self.state = "HOLD"
        elif error_pct > 0:
            self.state = "DECREASE"
        else:
            self.state = "INCREASE"

    def get_ratio(self) -> float:
        """Retourne le ratio d'ajustement actuel."""
        return self.capital_ratio
