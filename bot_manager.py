"""
bot_manager.py

Gestionnaire centralisé des bots – Adaptateur unique entre le MetaController et les bots.

Responsabilités :
- Découvrir automatiquement les bots disponibles pour un exchange donné.
- Fournir des descripteurs (BotDescriptor) pour chaque bot.
- Modifier le budget (capital_usdc) de manière atomique avec vérification.
- Mettre à jour le fichier .service du bot avec le nouveau budget.
- Redémarrer le bot via systemd.
- Vérifier que le bot est actif (service + lock + budget rechargé).
- Journaliser chaque étape.

Toute la logique de bas niveau (fichiers, systemd, locks) est encapsulée ici.
Le TransferEngine ne manipule que des descripteurs et des appels à ce gestionnaire.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any
from process_synchronization import AtomicJsonStateStore, BotLock

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# DTO
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class BotDescriptor:
    symbol: str
    exchange: str
    base_asset: str
    state_file: Path
    service_name: str
    lock_file: Path
    service_file: Optional[Path] = None

    @property
    def pid(self) -> Optional[int]:
        if not self.lock_file.exists():
            return None
        try:
            content = self.lock_file.read_text().strip()
            return int(json.loads(content)["pid"])
        except (ValueError, OSError, KeyError, json.JSONDecodeError):
            return None

    @property
    def exists(self) -> bool:
        return self.state_file.exists()


# -------------------------------------------------------------------------
# Gestionnaire principal
# -------------------------------------------------------------------------

class BotManager:
    def __init__(
        self,
        exchange: str,
        state_dir: str = ".",
        lock_dir: Optional[str] = None,
        service_dir: str = "/etc/systemd/system",
        service_template: str = "bot_{base_asset}_{exchange}.service",
        state_file_template: str = "state_{exchange}_{symbol_lower}.json",
        lock_file_template: str = "lock_{symbol_lower}.pid",
        verify_retries: int = 5,
        verify_delay: float = 1.0,
    ):
        self.exchange = exchange.lower()
        self.state_dir = Path(state_dir)
        if lock_dir is None:
            lock_dir = state_dir
        self.lock_dir = Path(lock_dir)
        self.service_dir = Path(service_dir)
        self.service_template = service_template
        self.state_file_template = state_file_template
        self.lock_file_template = lock_file_template
        self.verify_retries = verify_retries
        self.verify_delay = verify_delay
        self.logger = logger.getChild(f"BotManager[{self.exchange}]")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info(
            f"BotManager initialisé : exchange={self.exchange}, "
            f"state_dir={self.state_dir}, lock_dir={self.lock_dir}, "
            f"service_dir={self.service_dir}"
        )

    # ---------------------------------------------------------------------
    # Utilitaires
    # ---------------------------------------------------------------------

    @staticmethod
    def _extract_base_asset(symbol: str) -> str:
        for suffix in ["USDT", "USDC", "BUSD", "DAI"]:
            if symbol.endswith(suffix):
                return symbol[:-len(suffix)]
        return symbol

    @staticmethod
    def _extract_pid_from_lock(content: str) -> Optional[int]:
        match = re.search(r'^(\d+)', content.strip())
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

    # ---------------------------------------------------------------------
    # Découverte
    # ---------------------------------------------------------------------

    def discover_bots(self) -> List[BotDescriptor]:
        pattern = f"state_{self.exchange}_*.json"
        descriptors = []
        for state_file in self.state_dir.glob(pattern):
            stem = state_file.stem
            prefix = f"state_{self.exchange}_"
            if stem.startswith(prefix):
                symbol = stem[len(prefix):].upper()
                descriptors.append(self._build_descriptor(symbol))
        self.logger.info(f"Découverte de {len(descriptors)} bots pour {self.exchange}")
        return descriptors

    def get_descriptor(self, symbol: str) -> Optional[BotDescriptor]:
        if not symbol:
            raise ValueError("symbol ne peut pas être vide")
        descriptor = self._build_descriptor(symbol)
        if descriptor.exists:
            self.logger.info(
                f"Bot {symbol} trouvé : state={descriptor.state_file.name}, "
                f"service={descriptor.service_name}"
            )
            return descriptor
        self.logger.warning(
            f"Bot {symbol} introuvable : fichier {descriptor.state_file} manquant"
        )
        return None

    def _build_descriptor(self, symbol: str) -> BotDescriptor:
        if not symbol:
            raise ValueError("symbol ne peut pas être vide")

        symbol_lower = symbol.lower()
        base_asset = self._extract_base_asset(symbol).lower()

        try:
            state_file = self.state_dir / self.state_file_template.format(
                exchange=self.exchange, symbol_lower=symbol_lower
            )
        except KeyError as e:
            raise KeyError(f"Template state_file_template '{self.state_file_template}' nécessite la clé {e}")

        try:
            service_name = self.service_template.format(
                base_asset=base_asset, exchange=self.exchange
            )
        except KeyError as e:
            raise KeyError(f"Template service_template '{self.service_template}' nécessite la clé {e}")

        try:
            lock_file = self.lock_dir / self.lock_file_template.format(
                symbol_lower=symbol_lower
            )
        except KeyError as e:
            raise KeyError(f"Template lock_file_template '{self.lock_file_template}' nécessite la clé {e}")

        service_file = self.service_dir / service_name

        return BotDescriptor(
            symbol=symbol,
            exchange=self.exchange,
            base_asset=base_asset,
            state_file=state_file,
            service_name=service_name,
            lock_file=lock_file,
            service_file=service_file,
        )

    # ---------------------------------------------------------------------
    # Écriture du fichier de contrôle (nouveau mécanisme)
    # ---------------------------------------------------------------------

    def _write_control_file(self, symbol: str, target: float) -> None:
        """
        Écrit le fichier de contrôle de manière atomique.
        """
        control_path = self.state_dir / f"control_{symbol.lower()}.json"
        data = {
            "symbol": symbol,
            "capital_target": round(target, 2),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.state_dir,
            prefix=".tmp_control_",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)

        try:
            os.replace(tmp_path, control_path)
            self.logger.info(f"📝 Fichier de contrôle écrit : {control_path} (target={target:.2f})")
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink()
            raise OSError(f"Échec de l'écriture atomique du fichier de contrôle : {e}")

    # ---------------------------------------------------------------------
    # Transaction (nouvelle version : publication de la cible)
    # ---------------------------------------------------------------------

    def apply_transaction(self, symbol: str, new_budget: float) -> Dict[str, Any]:
        """
        Publie la nouvelle allocation en écrivant le fichier de contrôle.
        (Anciennement : mettait à jour le service et redémarrait.)
        """
        self.logger.info(f"📤 Publication de la cible pour {symbol} : {new_budget:.2f} USDT")
        try:
            self._write_control_file(symbol, new_budget)
            return {
                "symbol": symbol,
                "success": True,
                "new_budget": new_budget,
                "old_budget": None,  # inconnu ici
                "steps": [{"step": "write_control_file", "status": "OK"}],
            }
        except Exception as e:
            self.logger.error(f"❌ Échec publication cible pour {symbol}: {e}")
            return {
                "symbol": symbol,
                "success": False,
                "new_budget": new_budget,
                "old_budget": None,
                "error": str(e),
                "steps": [{"step": "write_control_file", "status": "FAILED", "detail": str(e)}],
            }

    # ---------------------------------------------------------------------
    # Lecture du budget (inchangée)
    # ---------------------------------------------------------------------

    def get_budget(self, descriptor: BotDescriptor) -> float:
        data = AtomicJsonStateStore(descriptor.state_file).read()
        if data is None:
            raise OSError(f"State illisible : {descriptor.state_file}")
        budget = data.get("capital_usdc")
        if budget is None:
            raise KeyError(f"capital_usdc manquant dans {descriptor.state_file}")
        return float(budget)

    # ---------------------------------------------------------------------
    # Écriture atomique du budget (inchangée)
    # ---------------------------------------------------------------------

    def update_budget(self, descriptor: BotDescriptor, new_budget: float) -> None:
        self.logger.info(
            f"Mise à jour du budget de {descriptor.symbol} : {new_budget:.2f} USDT"
        )

        # Même lock que le bot : aucune mise à jour manuelle ne peut écraser
        # un state maintenu par une instance active.
        with BotLock(descriptor.lock_file, timeout=10.0, version="BotManager"):
            store = AtomicJsonStateStore(descriptor.state_file)
            data = store.read()
            if data is None:
                raise OSError(f"State illisible : {descriptor.state_file}")
            data["capital_usdc"] = new_budget
            store.write(data)

        self.logger.info(f"Budget mis à jour avec succès pour {descriptor.symbol}")

    # ---------------------------------------------------------------------
    # Méthodes systemd supprimées (redémarrage abandonné)
    # ---------------------------------------------------------------------
    # _update_service_file, _systemctl_daemon_reload, restart_bot ont été supprimées.
    # Elles ne sont plus utilisées.

# -------------------------------------------------------------------------
# Mock pour les tests (inchangé)
# -------------------------------------------------------------------------

class MockBotManager:
    def __init__(self):
        self.budgets: Dict[str, float] = {}
        self.restart_calls: List[str] = []
        self.running_states: Dict[str, bool] = {}
        self.descriptors: Dict[str, BotDescriptor] = {}

    def get_descriptor(self, symbol: str) -> Optional[BotDescriptor]:
        desc = BotDescriptor(
            symbol=symbol,
            exchange="test",
            base_asset=self._extract_base_asset(symbol),
            state_file=Path(f"/tmp/fake_{symbol}.json"),
            service_name=f"fake_{symbol}.service",
            lock_file=Path(f"/tmp/fake_{symbol}.pid"),
            service_file=Path(f"/tmp/fake_{symbol}.service"),
        )
        self.descriptors[symbol] = desc
        return desc

    @staticmethod
    def _extract_base_asset(symbol: str) -> str:
        for suffix in ["USDT", "USDC", "BUSD", "DAI"]:
            if symbol.endswith(suffix):
                return symbol[:-len(suffix)]
        return symbol

    def get_budget(self, descriptor: BotDescriptor) -> float:
        return self.budgets.get(descriptor.symbol, 0.0)

    def update_budget(self, descriptor: BotDescriptor, new_budget: float) -> None:
        self.budgets[descriptor.symbol] = round(new_budget, 2)

    def apply_transaction(self, symbol: str, new_budget: float) -> Dict[str, Any]:
        rounded = round(new_budget, 2)
        old_budget = self.budgets.get(symbol, 0.0)
        if abs(old_budget - rounded) < 0.01:
            return {"symbol": symbol, "success": True, "old_budget": old_budget, "new_budget": rounded}
        self.budgets[symbol] = rounded
        return {
            "symbol": symbol,
            "success": True,
            "old_budget": old_budget,
            "new_budget": rounded,
            "steps": [{"step": "write_control_file", "status": "OK"}],
        }
