import json
import os
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging
from dataclasses import asdict

logger = logging.getLogger(__name__)


class HistoryLogger:
    def __init__(self, base_dir: str = "history"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.meta_file = self.base_dir / "meta_controller_history.jsonl"
        self.capital_file = self.base_dir / "capital_history.jsonl"
        self.control_file = self.base_dir / "control_history.jsonl"
        self.summary_file = self.base_dir / "summary_history.jsonl"

    def _append_jsonl(self, filepath: Path, data: Dict[str, Any]) -> None:
        try:
            with filepath.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"⚠️ Échec d'écriture dans {filepath.name}: {e}")

    def log_meta_controller(self, report: Dict[str, Any]) -> None:
        if not report:
            return
        if "timestamp" not in report:
            report = {**report, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self._append_jsonl(self.meta_file, report)

    def log_capital(self, report: Dict[str, Any]) -> None:
        if not report:
            return
        if "timestamp" not in report:
            report = {**report, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        entry = {"timestamp": report["timestamp"]}
        for key in ["portfolio", "cash"]:
            if key in report:
                entry[key] = report[key]
        for k, v in report.items():
            if k not in ("timestamp", "portfolio", "cash"):
                entry[k] = v
        self._append_jsonl(self.capital_file, entry)

    def log_capital_view(self, view) -> None:
        """
        Sérialise directement une CapitalView dans capital_history.jsonl.
        Aucun recalcul ni transformation n'est effectué.
        La dataclass est convertie en dict via asdict().
        """
        if not view:
            return
        try:
            entry = asdict(view)
            # S'assurer que le timestamp est présent (il l'est déjà)
            self._append_jsonl(self.capital_file, entry)
        except Exception as e:
            logger.warning(f"⚠️ Échec d'écriture de CapitalView dans history: {e}")

    def log_control(self, report: Dict[str, Any]) -> None:
        if not report:
            return
        timestamp = report.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        decisions = report.get("decisions", {})
        for symbol, change in decisions.items():
            old_target = change.get("old_target")
            new_target = change.get("new_target")
            if old_target is None or new_target is None:
                continue
            if abs(new_target - old_target) < 0.01:
                continue
            entry = {
                "timestamp": timestamp,
                "symbol": symbol,
                "old_target": round(old_target, 2),
                "new_target": round(new_target, 2),
                "delta": round(new_target - old_target, 2),
                "action": "INCREASE" if new_target > old_target else "DECREASE",
            }
            self._append_jsonl(self.control_file, entry)

    def log_summary(self, report: Dict[str, Any]) -> None:
        if not report:
            return
        if "timestamp" not in report:
            report = {**report, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self._append_jsonl(self.summary_file, report)


def build_reports(result) -> Dict[str, Any]:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # --- Meta ---
    treasury = result.treasury_result
    strategies = []
    if treasury and treasury.allocations:
        for alloc in treasury.allocations:
            strategies.append({
                "symbol": alloc.symbol,
                "goi": round(alloc.goi, 3),
                "budget_current": round(alloc.current_budget, 2),
                "budget_target": round(alloc.target_budget, 2),
                "budget_recommended": round(alloc.recommended_budget, 2),
                "action": alloc.action.value,
            })

    meta_report = {
        "timestamp": timestamp,
        "exchange": result.exchange,
        "portfolio_value": round(result.free_usdt + sum(s.get("budget_current", 0) for s in strategies), 2),
        "cash_free": round(result.free_usdt, 2),
        "strategies": strategies,
    }

    # --- Capital ---
    capital_entry = {
        "timestamp": timestamp,
        "portfolio": meta_report["portfolio_value"],
        "cash": result.free_usdt,
    }
    for s in strategies:
        capital_entry[s["symbol"]] = s["budget_current"]

    # --- Summary ---
    if treasury and treasury.allocations:
        goi_values = [a.goi for a in treasury.allocations]
        summary_report = {
            "timestamp": timestamp,
            "portfolio": meta_report["portfolio_value"],
            "cash_free": result.free_usdt,
            "goi_mean": round(sum(goi_values) / len(goi_values), 3),
            "goi_max": round(max(goi_values), 3),
            "goi_min": round(min(goi_values), 3),
            "execution_required": result.execution_plan.execution_required if result.execution_plan else False,
            "operations": len([a for a in treasury.allocations if abs(a.delta) > 0.01]),
        }
    else:
        summary_report = {
            "timestamp": timestamp,
            "portfolio": meta_report["portfolio_value"],
            "cash_free": result.free_usdt,
            "goi_mean": 0.0,
            "goi_max": 0.0,
            "goi_min": 0.0,
            "execution_required": False,
            "operations": 0,
        }

    return {
        "meta": meta_report,
        "capital": capital_entry,
        "summary": summary_report,
    }


class MetaControllerHistoryLogger(HistoryLogger):
    def __init__(self, base_dir: str = "history"):
        super().__init__(base_dir)
        self._last_targets: Dict[str, float] = {}

    def log_control_with_state(self, timestamp: str, decisions: Dict[str, float]) -> None:
        entries = []
        for symbol, new_target in decisions.items():
            old_target = self._last_targets.get(symbol)
            if old_target is not None and abs(new_target - old_target) > 0.01:
                entries.append({
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "old_target": round(old_target, 2),
                    "new_target": round(new_target, 2),
                    "delta": round(new_target - old_target, 2),
                    "action": "INCREASE" if new_target > old_target else "DECREASE",
                })
            self._last_targets[symbol] = new_target

        for entry in entries:
            self._append_jsonl(self.control_file, entry)
