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
        max_adjust_per_cycle: float = 0.02,
        min_ratio: float = 0.5,
        max_ratio: float = 2.0,
    ):
        self.symbol = symbol.lower()
        self.state_dir = Path(state_dir)
        self.control_path = self.state_dir / f"control_{self.symbol}.json"

        self.check_interval = check_interval
        self.max_adjust_per_cycle = max_adjust_per_cycle
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio

        # État en mémoire
        self.current_target: Optional[float] = None
        self.last_read_time: float = 0.0
        self.capital_ratio: float = 1.0

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

        # Relecture périodique du fichier
        if now - self.last_read_time >= self.check_interval:
            self.last_read_time = now
            target = self._read_control_file()
            if target is not None and target > 0:
                if self.current_target != target:
                    logger.info("🎯 Nouvelle cible de capital reçue : %.2f", target)
                self.current_target = target

        # Si pas de cible, on ne touche pas au ratio
        if self.current_target is None or current_capital <= 0:
            return

        # Ratio désiré
        desired = self.current_target / current_capital
        desired = max(self.min_ratio, min(self.max_ratio, desired))

        # Ajustement progressif avec pas max
        diff = desired - self.capital_ratio
        step = max(-self.max_adjust_per_cycle, min(self.max_adjust_per_cycle, diff))
        self.capital_ratio += step

        # Reclamp
        self.capital_ratio = max(self.min_ratio, min(self.max_ratio, self.capital_ratio))

        # Les ajustements réguliers ne sont utiles qu'en diagnostic : aucun
        # message INFO périodique ne doit masquer les événements de démarrage.
        if abs(step) > 1e-12:
            logger.debug(
                "CapitalTarget ratio ajusté : %.3f -> %.3f (cible=%.2f, actuel=%.2f)",
                self.capital_ratio - step, self.capital_ratio,
                self.current_target, current_capital,
            )

    def get_ratio(self) -> float:
        """Retourne le ratio d'ajustement actuel."""
        return self.capital_ratio
