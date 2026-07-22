#!/usr/bin/env python3
"""
audit_prejalon0_v2.py

Version corrigée après premier passage d'audit (les noms de fichiers
réels utilisent le symbole complet — egldusdt, filusdt, etc. — et
certains sont horodatés/rotés, pas un nom fixe par bot).

A exécuter depuis ~/ton_grid_bot :
    python3 audit_prejalon0_v2.py

Lecture seule stricte (Principe P0).
"""

import json
import os
import glob
import re
from datetime import datetime, timezone

SEUIL_COMPLETUDE_RN044 = 0.95
SEUIL_JOURS_CONTINUS_RN044 = 14
SEUIL_TROU_MAX_RN043A_SECONDES = 30 * 60

SYMBOLS = ["filusdt", "stxusdt", "rsrusdt", "egldusdt", "injusdt"]
SHORT_NAMES = {"filusdt": "fil", "stxusdt": "stx", "rsrusdt": "rsr", "egldusdt": "egld", "injusdt": "inj"}

LOG_TS_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")


def parse_log_timestamp(line):
    m = LOG_TS_PATTERN.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def scan_jsonl(path):
    records, errors = [], 0
    if not os.path.exists(path):
        return records, errors, False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                errors += 1
    return records, errors, True


def ts_to_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def analyze_timestamps(records, ts_key="ts"):
    timestamps = [r[ts_key] for r in records if r.get(ts_key) is not None]
    timestamps.sort()
    if not timestamps:
        return {"count": len(records), "first": None, "last": None, "depth_days": 0.0, "gaps": []}
    first_dt, last_dt = ts_to_dt(timestamps[0]), ts_to_dt(timestamps[-1])
    depth_days = (last_dt - first_dt).total_seconds() / 86400.0
    gaps = []
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        if gap > SEUIL_TROU_MAX_RN043A_SECONDES:
            gaps.append((ts_to_dt(timestamps[i-1]).isoformat(), ts_to_dt(timestamps[i]).isoformat(), round(gap/60, 1)))
    return {"count": len(records), "first": first_dt.isoformat(), "last": last_dt.isoformat(),
            "depth_days": round(depth_days, 2), "gaps": gaps}


def print_header(t):
    print("\n" + "=" * 78); print(t); print("=" * 78)


def main():
    print_header("PRÉ-JALON 0 v2 — AUDIT DES DONNÉES HISTORIQUES (noms réels)")

    # ------------------------------------------------------------
    # metrics_gateio_{symbol}_*.jsonl — fichiers horodatés/rotés
    # ------------------------------------------------------------
    print_header("Analyse : metrics_gateio_{symbol}_*.jsonl (fichiers rotés)")
    metrics_depths = {}
    for sym in SYMBOLS:
        files = sorted(glob.glob(f"metrics_gateio_{sym}_*.jsonl"))
        print(f"\n  [{sym}] {len(files)} fichier(s) trouvé(s) :")
        total_records = 0
        all_ts = []
        for f in files:
            records, errors, _ = scan_jsonl(f)
            total_records += len(records)
            for r in records:
                if r.get("ts") is not None:
                    all_ts.append(r["ts"])
            print(f"      {f} : {len(records)} enregistrements, {errors} erreurs")
        if all_ts:
            all_ts.sort()
            depth = (ts_to_dt(all_ts[-1]) - ts_to_dt(all_ts[0])).total_seconds() / 86400.0
            print(f"    → profondeur cumulée : {depth:.2f} jours ({ts_to_dt(all_ts[0]).isoformat()} -> {ts_to_dt(all_ts[-1]).isoformat()})")
            metrics_depths[sym] = depth
        else:
            print(f"    → aucun timestamp exploitable")
            metrics_depths[sym] = 0.0

    # ------------------------------------------------------------
    # journal_gateio_{symbol}.jsonl — décisions de capital
    # ------------------------------------------------------------
    print_header("Analyse : journal_gateio_{symbol}.jsonl")
    for sym in SYMBOLS:
        path = f"journal_gateio_{sym}.jsonl"
        records, errors, exists = scan_jsonl(path)
        if not exists:
            print(f"  [{sym}] ABSENT")
            continue
        ts_info = analyze_timestamps(records, ts_key="ts")
        print(f"  [{sym}] {ts_info['count']} transitions, profondeur {ts_info['depth_days']} jours "
              f"({ts_info['first']} -> {ts_info['last']})")

    # ------------------------------------------------------------
    # Nouvelles sources découvertes via `ls` — MetaController/portefeuille
    # ------------------------------------------------------------
    print_header("Analyse : sources MetaController/portefeuille (découvertes via ls)")
    for path in ["decision_journal_gateio.jsonl", "reallocation_history.jsonl",
                 "portfolio_history.jsonl", "portfolio_history_gateio.jsonl",
                 "portfolio_history_old.jsonl", "last_reallocation_gateio.json"]:
        if os.path.exists(path):
            if path.endswith(".jsonl"):
                records, errors, _ = scan_jsonl(path)
                ts_info = analyze_timestamps(records, ts_key="ts")
                print(f"  [{path}] {ts_info['count']} enregistrements, {errors} erreurs, "
                      f"profondeur {ts_info['depth_days']} jours ({ts_info['first']} -> {ts_info['last']})")
            else:
                print(f"  [{path}] présent (snapshot, pas d'historique)")
        else:
            print(f"  [{path}] ABSENT")

    # ------------------------------------------------------------
    # STOP-LOSS (PnL total) — extraction précise des bornes réelles
    # ------------------------------------------------------------
    print_header("Extraction précise des fenêtres STOP-LOSS (PnL total), par bot")
    contaminated_windows = {}
    for sym in SYMBOLS:
        short = SHORT_NAMES[sym]
        path = f"bot_gateio_{short}.log"
        if not os.path.exists(path):
            continue
        first_ts, last_ts, count = None, None, 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "STOP-LOSS (PnL total)" in line:
                    count += 1
                    dt = parse_log_timestamp(line)
                    if dt is not None:
                        if first_ts is None or dt < first_ts:
                            first_ts = dt
                        if last_ts is None or dt > last_ts:
                            last_ts = dt
        if count:
            print(f"  [{sym}] {count} occurrences, fenêtre réelle : "
                  f"{first_ts.isoformat() if first_ts else '???'} -> {last_ts.isoformat() if last_ts else '???'}")
            contaminated_windows[sym] = (first_ts, last_ts, count)
        else:
            print(f"  [{sym}] 0 occurrence — non contaminé par cet incident")

    print_header("CONCLUSION")
    max_depth = max(metrics_depths.values()) if metrics_depths else 0.0
    print(f"Profondeur max (metrics_gateio) : {max_depth:.2f} jours (seuil RN-044 : {SEUIL_JOURS_CONTINUS_RN044} j)")
    print(f"Semaines de walk-forward brutes : {max_depth/7:.1f}")
    print("\nFenêtres STOP-LOSS(PnL) réelles à exclure de tout entraînement/validation :")
    for sym, (first_ts, last_ts, count) in contaminated_windows.items():
        print(f"  - {sym} : {first_ts.isoformat() if first_ts else '???'} -> "
              f"{last_ts.isoformat() if last_ts else '???'} ({count} occurrences)")

    print("\n" + "=" * 78)
    print("FIN — Aucune donnée modifiée.")
    print("=" * 78)


if __name__ == "__main__":
    main()
