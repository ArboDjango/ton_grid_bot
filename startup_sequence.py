"""État déterministe et observable de l'initialisation du bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class StartupSequence:
    started_at: float = field(default_factory=time.monotonic)
    configuration_loaded: bool = False
    exchange_connected: bool = False
    calibration_done: bool = False
    state_loaded: bool = False
    reconciliation_done: bool = False
    grid_initialized: bool = False
    websocket_connected: bool = False
    capital_target_active: bool = False

    @property
    def ready(self) -> bool:
        return all((
            self.configuration_loaded,
            self.exchange_connected,
            self.calibration_done,
            self.state_loaded,
            self.reconciliation_done,
            self.grid_initialized,
            self.websocket_connected,
            self.capital_target_active,
        ))

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_at

    def ready_report(self) -> str:
        if not self.ready:
            raise RuntimeError("BOT READY demandé avant que toutes les dépendances soient satisfaites")
        return (
            "\n=================================================\n"
            "✅ BOT READY\n"
            "=================================================\n"
            "Exchange connecté\n"
            "State chargé\n"
            "Calibration OK\n"
            "Réconciliation OK\n"
            "Grille initialisée\n"
            "WebSocket connecté\n"
            f"Temps d'initialisation : {self.duration:.1f} s\n"
            "================================================="
        )
