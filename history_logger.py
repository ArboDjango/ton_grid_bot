#!/usr/bin/env python3
"""
History Logger intelligent (RN-018)
- Écriture conditionnelle basée sur des seuils relatifs par métrique
- Compare avec la dernière métrique enregistrée (last_written)
- Heartbeat toutes les 30 minutes
- Statistiques de réduction
- Méthode log_capital_view pour intégration facilitée
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from config import (
    DATA_DIR,
    METRICS_HEARTBEAT_SECONDS,
    METRICS_THRESHOLDS,
    METRICS_ABSOLUTE_MIN_CHANGE,
    METRICS_STATS_INTERVAL,
)

class HistoryLogger:
    def __init__(self):
        self._last_written = {}
        self._stats = {"written": 0, "ignored": 0, "heartbeat": 0}
        self._stats_last_display = time.time()

    def write(self, metrics: dict, symbol: str):
        if not metrics:
            return
        now = time.time()
        decision = self._should_write(metrics, symbol, now)
        if decision == "WRITE":
            self._write_metrics(metrics, symbol)
            self._last_written[symbol] = {'timestamp': now, 'metrics': metrics.copy()}
            self._stats["written"] += 1
        elif decision == "IGNORE":
            self._stats["ignored"] += 1
        elif decision == "HEARTBEAT":
            self._write_metrics(metrics, symbol)
            self._last_written[symbol] = {'timestamp': now, 'metrics': metrics.copy()}
            self._stats["written"] += 1
            self._stats["heartbeat"] += 1
        if (now - self._stats_last_display) >= METRICS_STATS_INTERVAL:
            self._display_stats()

    def log_capital_view(self, view: dict, symbol: str):
        """Enregistre une vue de capital (métriques)."""
        if not isinstance(view, dict):
            return
        self.write(view, symbol)

    def log_event(self, event_type: str, data: dict, symbol: str):
        """Journalisation des événements métier (toujours écrite)."""
        filename = Path(DATA_DIR) / f"journal_gateio_{symbol.lower()}.jsonl"
        filename.parent.mkdir(parents=True, exist_ok=True)
        if 'ts' not in data:
            data['ts'] = int(time.time())
        data['event'] = event_type
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data) + '\n')

    def _should_write(self, metrics, symbol, now):
        written = self._last_written.get(symbol)
        if written is None:
            return "WRITE"
        prev = written['metrics']
        last_time = written['timestamp']

        for metric, threshold in METRICS_THRESHOLDS.items():
            if metric in metrics and metric in prev:
                if self._significant_change(prev[metric], metrics[metric], metric, threshold):
                    return "WRITE"

        if ('adx_regime' in metrics and 'adx_regime' in prev and
            metrics['adx_regime'] != prev['adx_regime']):
            return "WRITE"

        if (now - last_time) >= METRICS_HEARTBEAT_SECONDS:
            return "HEARTBEAT"

        return "IGNORE"

    @staticmethod
    def _significant_change(old, new, metric, threshold):
        EPS = 1e-9
        if abs(old) < EPS:
            min_abs = METRICS_ABSOLUTE_MIN_CHANGE.get(metric, 1e-6)
            return abs(new) > min_abs
        return abs(new - old) / abs(old) >= threshold

    def _write_metrics(self, metrics, symbol):
        filename = Path(DATA_DIR) / f"metrics_gateio_{symbol.lower()}.jsonl"
        filename.parent.mkdir(parents=True, exist_ok=True)
        if 'ts' not in metrics:
            metrics['ts'] = int(time.time())
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(json.dumps(metrics) + '\n')

    def _display_stats(self):
        total = self._stats["written"] + self._stats["ignored"]
        reduction = 100 * (1 - self._stats["written"] / total) if total > 0 else 0.0
        print("\n" + "═" * 50)
        print("📊 HistoryLogger - Statistiques")
        print(f"   Écrites      : {self._stats['written']}")
        print(f"   Ignorées     : {self._stats['ignored']}")
        print(f"   Heartbeat    : {self._stats['heartbeat']}")
        print(f"   Réduction    : {reduction:.1f} %")
        print("═" * 50 + "\n")
        self._stats_last_display = time.time()
        # Optionnel : réinitialiser les compteurs
        # self._stats = {"written": 0, "ignored": 0, "heartbeat": 0}



class MetaControllerHistoryLogger:
    def __init__(self, base_dir: str = "history"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _write(self, filename: str, data: Dict[str, Any]) -> None:
        filepath = self.base_dir / filename
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def log_meta_controller(self, report: Dict[str, Any]) -> None:
        self._write("meta_controller_history.jsonl", report)

    def log_capital(self, report: Dict[str, Any]) -> None:
        self._write("capital_history.jsonl", report)

    def log_summary(self, report: Dict[str, Any]) -> None:
        self._write("summary_history.jsonl", report)

    def log_control_with_state(self, timestamp: str, decisions: Dict[str, float]) -> None:
        data = {
            "timestamp": timestamp,
            "decisions": decisions,
        }
        self._write("control_history.jsonl", data)



def build_reports(result) -> Dict[str, Dict[str, Any]]:
    from meta_controller import MetaControllerResult
    if not isinstance(result, MetaControllerResult):
        raise TypeError("result must be a MetaControllerResult instance")

    now = datetime.now(timezone.utc).isoformat()
    exchange = result.exchange

    evaluations = result.evaluations
    treasury = result.treasury_result
    exec_plan = result.execution_plan
    free_cash = result.free_usdt

    # ---- 1. Budget target (cible idéale) depuis le VirtualTreasury ----
    target_by_symbol = {}
    if treasury is not None:
        alloc = getattr(treasury, 'allocations', None)
        if alloc is not None:
            if isinstance(alloc, dict):
                target_by_symbol = alloc
            elif isinstance(alloc, list):
                for item in alloc:
                    if isinstance(item, dict) and 'symbol' in item and 'budget' in item:
                        target_by_symbol[item['symbol']] = item['budget']
                    elif hasattr(item, 'symbol') and hasattr(item, 'target_budget'):
                        target_by_symbol[item.symbol] = item.target_budget

    # ---- 2. Budget recommandé (final) depuis l'ExecutionPlan ----
    recommendations_by_symbol = {}
    if exec_plan:
        recs = getattr(exec_plan, 'recommendations', [])
        if isinstance(recs, list):
            for rec in recs:
                if hasattr(rec, 'symbol') and hasattr(rec, 'target_budget') and hasattr(rec, 'action'):
                    recommendations_by_symbol[rec.symbol] = rec

    # ---- 3. Construction de la liste des stratégies ----
    strategies_meta = []
    goi_values = []

    for ev in evaluations:
        symbol = ev.symbol
        goi = ev.goi_result.value if ev.goi_result and ev.goi_result.valid else None
        if goi is not None:
            goi_values.append(goi)

        obs = ev.observation or {}
        current_budget = obs.get("capital_usdc", 0.0)

        target_budget = target_by_symbol.get(symbol, current_budget)

        rec = recommendations_by_symbol.get(symbol)
        if rec:
            recommended_budget = rec.target_budget
            action = rec.action.value if hasattr(rec.action, 'value') else str(rec.action)
        else:
            recommended_budget = current_budget
            action = "HOLD"

        strategies_meta.append({
            "symbol": symbol,
            "goi": goi,
            "budget_current": current_budget,
            "budget_target": target_budget,
            "budget_recommended": recommended_budget,
            "action": action,
        })

    portfolio_value = sum(s["budget_current"] for s in strategies_meta) + free_cash

    # ---- Rapport meta_controller ----
    meta_report = {
        "timestamp": now,
        "exchange": exchange,
        "portfolio_value": portfolio_value,
        "cash_free": free_cash,
        "strategies": strategies_meta,
    }

    # ---- Rapport capital ----
    capital_report = {
        "timestamp": now,
        "portfolio": portfolio_value,
        "cash": free_cash,
    }
    for s in strategies_meta:
        capital_report[s["symbol"]] = s["budget_target"]

    # ---- Rapport summary ----
    goi_mean = sum(goi_values) / len(goi_values) if goi_values else 0.0
    goi_max = max(goi_values) if goi_values else 0.0
    goi_min = min(goi_values) if goi_values else 0.0

    operations = sum(1 for s in strategies_meta if s["action"] != "HOLD")
    execution_required = exec_plan.execution_required if exec_plan and hasattr(exec_plan, 'execution_required') else False

    summary_report = {
        "timestamp": now,
        "portfolio": portfolio_value,
        "cash_free": free_cash,
        "goi_mean": goi_mean,
        "goi_max": goi_max,
        "goi_min": goi_min,
        "execution_required": execution_required,
        "operations": operations,
    }

    return {
        "meta": meta_report,
        "capital": capital_report,
        "summary": summary_report,
    }
