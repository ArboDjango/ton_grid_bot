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
