#!/usr/bin/env python3
"""
Calibration des paramètres DENSITY_ATR et DENSITY_K pour un token donné.
Usage : python calibrate_atr.py RSRUSDC
        ou en module : from calibrate_atr import calibrate
"""
import sys
import numpy as np
import pandas as pd
import ta
from binance.client import Client
from dotenv import load_dotenv
import os

load_dotenv()

client = Client(
    os.getenv("BINANCE_API_KEY"),
    os.getenv("BINANCE_API_SECRET")
)

def calibrate(symbol: str, limit: int = 500) -> dict:
    """
    Calibre les paramètres de grille pour un symbole donné.
    
    Args:
        symbol: Paire Binance (ex: "INJUSDC")
        limit: Nombre de klines 15min à récupérer (défaut 500)
    
    Returns:
        dict: {
            "atr_low": float,
            "atr_high": float,
            "k_min": float,
            "k_max": float,
            "adx_mean": float,
            "autocorr_1": float,
            "comment": str
        }
    """
    # Téléchargement des klines 15min
    klines = client.get_klines(
        symbol=symbol,
        interval=Client.KLINE_INTERVAL_15MINUTE,
        limit=limit
    )
    df = pd.DataFrame(klines, columns=[
        'time','open','high','low','close','volume',
        'ct','qav','trades','tbb','tbq','i'
    ])
    for col in ['high','low','close']:
        df[col] = df[col].astype(float)
    
    # Calcul ATR normalisé
    atr_series = ta.volatility.average_true_range(
        df['high'], df['low'], df['close'], window=14
    )
    atr_norm = atr_series / df['close']
    atr_norm = atr_norm.dropna()
    
    p10 = atr_norm.quantile(0.10)
    p90 = atr_norm.quantile(0.90)
    
    # Analyse de la tendance
    adx_series = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    adx_mean = adx_series.dropna().mean()
    
    returns = df['close'].pct_change().dropna()
    autocorr_1 = returns.autocorr(lag=1)
    
    # Détermination de k_min (selon votre logique)
    if autocorr_1 < -0.05 and adx_mean < 25:
        k_min = 0.50
        comment = "Mean-reverting fort → grille très dense aux bornes"
    elif autocorr_1 < 0.0 and adx_mean < 30:
        k_min = 0.60
        comment = "Légèrement mean-reverting → densité modérée aux bornes"
    elif adx_mean < 35:
        k_min = 0.70
        comment = "Semi-tendanciel → densité faible aux bornes"
    else:
        k_min = 0.80
        comment = "Très tendanciel → grille quasi uniforme"
    
    return {
        "atr_low": round(p10, 4),
        "atr_high": round(p90, 4),
        "k_min": k_min,
        "k_max": 1.0,
        "adx_mean": round(adx_mean, 1),
        "autocorr_1": round(autocorr_1, 3),
        "comment": comment
    }

# ─────────────────────────────────────────────────────────────
# Partie main : si le script est exécuté directement
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python calibrate_atr.py SYMBOLE")
        sys.exit(1)
    symbol = sys.argv[1].upper()
    params = calibrate(symbol)
    
    print(f"\n{'='*60}")
    print(f"  CALIBRATION ATR — {symbol}")
    print(f"{'='*60}")
    print(f"\n  Distribution ATR normalisé 15min :")
    print(f"  p10    : {params['atr_low']:.4f}")
    print(f"  p90    : {params['atr_high']:.4f}")
    print(f"\n  Analyse comportement prix :")
    print(f"  ADX moyen        : {params['adx_mean']:.1f}")
    print(f"  Autocorrélation lag-1 : {params['autocorr_1']:+.3f}")
    print(f"\n  Recommandation DENSITY_K_MIN = {params['k_min']}")
    print(f"  Justification : {params['comment']}")
    print(f"\n  Ligne ExecStart recommandée :")
    print(f"  ExecStart=.../bot.py {symbol} <BUDGET> "
          f"{params['atr_low']} {params['atr_high']} {params['k_min']} {params['k_max']}")
    print(f"{'='*60}\n")
