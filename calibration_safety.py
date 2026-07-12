"""Validation atomique des résultats de calibration ATR/K."""

from __future__ import annotations

import math
import time
from typing import Any, Callable


class CalibrationValidationError(ValueError):
    """Résultat de calibration inutilisable, sans modification de l'état."""


_REQUIRED_NUMBERS = ("atr_low", "atr_high", "k_min", "k_max", "adx_mean", "autocorr_1")
_MAX_NORMALIZED_ATR = 0.20
_MAX_K = 2.0


def validate_calibration_params(params: dict[str, Any]) -> dict[str, Any]:
    """Valide intégralement une calibration avant toute application.

    Les ATR du calibrateur sont normalisés par le prix : la limite de 20 %
    représente donc la contrainte ``spacing < 20 %``.
    """
    if not isinstance(params, dict):
        raise CalibrationValidationError("résultat de calibration non dictionnaire")

    validated = dict(params)
    for name in _REQUIRED_NUMBERS:
        if name not in params:
            raise CalibrationValidationError(f"paramètre manquant: {name}")
        try:
            value = float(params[name])
        except (TypeError, ValueError) as exc:
            raise CalibrationValidationError(f"paramètre non numérique: {name}") from exc
        if not math.isfinite(value):
            raise CalibrationValidationError(f"paramètre non fini: {name}")
        validated[name] = value

    if not (0 < validated["atr_low"] <= validated["atr_high"] < _MAX_NORMALIZED_ATR):
        raise CalibrationValidationError(
            f"ATR normalisés incohérents: 0 < atr_low <= atr_high < {_MAX_NORMALIZED_ATR} requis"
        )
    if not (0 < validated["k_min"] <= validated["k_max"] <= _MAX_K):
        raise CalibrationValidationError("coefficients K incohérents: 0 < k_min <= k_max <= 2 requis")
    if not (0 <= validated["adx_mean"] <= 100):
        raise CalibrationValidationError("ADX moyen hors bornes [0, 100]")
    if not (-1 <= validated["autocorr_1"] <= 1):
        raise CalibrationValidationError("autocorrélation hors bornes [-1, 1]")
    return validated


def run_calibration(
    enabled: bool,
    exchange: Any,
    symbol: str,
    calibrator: Callable[[Any, str], dict[str, Any]],
) -> tuple[dict[str, Any] | None, float]:
    """Exécute et valide une calibration, sans effet si elle est désactivée."""
    if not enabled:
        return None, 0.0
    if exchange is None or not callable(getattr(exchange, "get_klines", None)):
        raise CalibrationValidationError("exchange invalide: get_klines indisponible")
    if not isinstance(symbol, str) or not symbol.strip():
        raise CalibrationValidationError("symbole invalide")

    started = time.perf_counter()
    try:
        params = calibrator(exchange, symbol)
    except Exception as exc:
        raise CalibrationValidationError(
            f"échec récupération/calcul données marché pour {symbol}: {type(exc).__name__}: {exc}"
        ) from exc
    duration = time.perf_counter() - started
    return validate_calibration_params(params), duration


def apply_calibration_atomically(state: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Applique tous les paramètres ensemble, uniquement après validation totale."""
    validated = validate_calibration_params(params)
    updates = {
        "DENSITY_ATR_LOW": validated["atr_low"],
        "DENSITY_ATR_HIGH": validated["atr_high"],
        "DENSITY_K_MIN": validated["k_min"],
        "DENSITY_K_MAX": validated["k_max"],
        "CALIB_REF_ATR_LOW": validated["atr_low"],
        "CALIB_REF_ATR_HIGH": validated["atr_high"],
        "CALIB_REF_K_MIN": validated["k_min"],
    }
    state.update(updates)
    return validated
