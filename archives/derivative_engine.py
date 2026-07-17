#!/usr/bin/env python3
"""
derivative_engine.py
===============================================================================

Generic Derivative Engine
=========================

This module estimates time derivatives from timestamped observations.

It is completely domain-agnostic.

The engine does NOT know anything about:

    - trading
    - bots
    - Portfolio Manager
    - wallets
    - pnl
    - alpha
    - GOI

It only estimates:

        Δ value
rate = ----------
        Δ time

Design principles
-----------------

✓ Stateless
✓ Deterministic
✓ Pure computations
✓ No disk access
✓ No logging
✓ No side effects

===============================================================================
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


# ---------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Sample:
    """
    One timestamped observation.
    """

    timestamp: datetime
    value: float


@dataclass(frozen=True)
class RateResult:
    """
    Result of a derivative estimation.
    """

    rate_per_day: float

    delta_value: float

    duration_hours: float

    samples: int


# ---------------------------------------------------------------------
# Derivative Engine
# ---------------------------------------------------------------------

class DerivativeEngine:
    """
    Estimate first-order derivatives from timestamped samples.
    """

    @staticmethod
    def estimate_rate(
        samples: Sequence[Sample],
        window: timedelta,
    ) -> RateResult | None:
        """
        Estimate the average rate over the requested window.

        Parameters
        ----------
        samples
            Timestamped observations.

        window
            Requested observation window.

        Returns
        -------
        RateResult

        or

        None
            if estimation is impossible.
        """

        if len(samples) < 2:
            return None

        samples = sorted(samples, key=lambda s: s.timestamp)

        latest = samples[-1]

        target = latest.timestamp - window

        reference = DerivativeEngine._find_reference_sample(
            samples,
            target,
        )

        if reference is None:
            return None

        delta_value = latest.value - reference.value

        duration_hours = (
            latest.timestamp - reference.timestamp
        ).total_seconds() / 3600.0

        if duration_hours <= 0:
            return None

        rate_per_day = delta_value / duration_hours * 24.0

        return RateResult(
            rate_per_day=rate_per_day,
            delta_value=delta_value,
            duration_hours=duration_hours,
            samples=len(samples),
        )

    # -----------------------------------------------------------------

    @staticmethod
    def _find_reference_sample(
        samples: Sequence[Sample],
        target: datetime,
    ) -> Sample | None:
        """
        Return the latest sample whose timestamp is <= target.
        """

        timestamps = [s.timestamp for s in samples]

        idx = bisect_right(timestamps, target) - 1

        if idx < 0:
            return None

        return samples[idx]
