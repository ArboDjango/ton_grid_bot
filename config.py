# ============================================================
# Chemins
# ============================================================
DATA_DIR = "./data"          # Répertoire des données (métriques, journaux)

# ========== Smart History Logger (RN-018) ==========
METRICS_HEARTBEAT_SECONDS = 1800
METRICS_THRESHOLDS = {
    "capital_ratio": 0.02,
    "capital_target": 0.01,
    "Gv": 0.05,
    "stress": 0.03,
    "drawdown": 0.05,
}
METRICS_ABSOLUTE_MIN_CHANGE = {
    "capital_ratio": 1e-4,
    "capital_target": 0.05,
    "Gv": 0.05,
    "stress": 0.01,
    "drawdown": 0.01,
}
METRICS_STATS_INTERVAL = 86400
