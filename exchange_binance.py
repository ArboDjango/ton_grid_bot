"""
exchange_binance.py

Implémentation Binance de l'interface ExchangeBase.
Supporte le spot Binance via python-binance.

Version : 2.0
Changements v2.0 :
    - Implémente ExchangeBase (exchange_base.py)
    - Classe ExchangeBinance avec toutes les méthodes normalisées
    - Compatibilité ascendante : get_client(), get_real_balances(),
      invalidate_balance_cache() conservées comme wrappers module-level
"""

from __future__ import annotations
import json
import logging
import math
import os
import time

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from exchange_base import ExchangeBase, OrderResult, SymbolInfo

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# IMPLÉMENTATION BINANCE
# ══════════════════════════════════════════════════════════════════

class ExchangeBinance(ExchangeBase):
    """
    Implémentation Binance Spot de ExchangeBase.

    Usage :
        exchange = ExchangeBinance()
        price = exchange.get_ticker_price("INJUSDC")

    Les credentials sont lus depuis les variables d'environnement :
        BINANCE_API_KEY, BINANCE_API_SECRET
    """
    
    NAME = "Binance"
    DEFAULT_QUOTE = "USDC"
    
    # ── Constantes Binance (override des défauts de ExchangeBase) ─
    SIDE_BUY  = Client.SIDE_BUY    # "BUY"
    SIDE_SELL = Client.SIDE_SELL   # "SELL"

    ORDER_TYPE_LIMIT  = Client.ORDER_TYPE_LIMIT    # "LIMIT"
    ORDER_TYPE_MARKET = Client.ORDER_TYPE_MARKET   # "MARKET"
    TIME_IN_FORCE_GTC = Client.TIME_IN_FORCE_GTC   # "GTC"

    KLINE_3M  = Client.KLINE_INTERVAL_3MINUTE      # "3m"
    KLINE_15M = Client.KLINE_INTERVAL_15MINUTE     # "15m"

    STATUS_NEW              = "NEW"
    STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
    STATUS_FILLED           = "FILLED"
    STATUS_CANCELED         = "CANCELED"
    STATUS_REJECTED         = "REJECTED"
    STATUS_EXPIRED          = "EXPIRED"

    _BALANCE_CACHE_TTL = 5.0  # secondes

    # ── Init ─────────────────────────────────────────────────────

    def __init__(self) -> None:
        api_key    = os.getenv("BINANCE_API_KEY")
        api_secret = os.getenv("BINANCE_API_SECRET")
        self._client = Client(api_key, api_secret)
        if hasattr(self._client, "session"):
            self._client.session.request_timeout = 15

        self._balance_cache: dict = {
            "quote":     0.0,
            "base":      0.0,
            "timestamp": 0.0,
        }

    # ── Prix ─────────────────────────────────────────────────────

    def get_ticker_price(self, symbol: str) -> float | None:
        try:
            ticker = self._client.get_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except Exception:
            logger.exception(f"❌ Erreur prix {symbol}")
            return None

    # ── Symbole ──────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        info = self._client.get_symbol_info(symbol)
        if info is None:
            raise ValueError(f"Symbole {symbol} introuvable sur Binance")

        price_decimals = 4
        qty_decimals   = 2
        min_qty        = 0.0
        min_notional   = 0.0

        for f in info["filters"]:
            ft = f["filterType"]
            if ft == "PRICE_FILTER":
                tick = float(f["tickSize"])
                if tick > 0:
                    price_decimals = int(round(-math.log10(tick)))
            elif ft == "LOT_SIZE":
                step = float(f["stepSize"])
                if step > 0:
                    qty_decimals = int(round(-math.log10(step)))
                raw_min = float(f.get("minQty", 0.0))
                if raw_min > 0:
                    min_qty = raw_min
            elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                val = float(f.get("minNotional", 0.0))
                if val > 0:
                    min_notional = val

        return SymbolInfo(
            price_decimals=price_decimals,
            qty_decimals=qty_decimals,
            min_qty=min_qty,
            min_notional=min_notional,
        )

    # ── Données de marché ─────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """
        Retourne un DataFrame OHLCV avec les colonnes standard.
        Les colonnes open/high/low/close/volume sont déjà castées en float.
        """
        raw = self._client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(
            raw,
            columns=["time", "open", "high", "low", "close", "volume",
                     "ct", "qav", "trades", "tbb", "tbq", "i"],
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df

    # ── Ordres ───────────────────────────────────────────────────

    def get_open_orders(self, symbol: str) -> list[dict]:
        """
        Retourne les ordres ouverts avec les clés normalisées
        (order_id, orig_qty, executed_qty au lieu des noms Binance bruts).
        """
        raw = self._client.get_open_orders(symbol=symbol)
        return [
            {
                "order_id":     o["orderId"],
                "side":         o["side"],
                "orig_qty":     float(o["origQty"]),
                "executed_qty": float(o.get("executedQty", 0.0)),
                "price":        float(o["price"]),
                "status":       o["status"],
            }
            for o in raw
        ]

    def cancel_order(self, symbol: str, order_id: str | int) -> None:
        self._client.cancel_order(symbol=symbol, orderId=order_id)

    def get_order(self, symbol: str, order_id: str | int) -> OrderResult:
        raw = self._client.get_order(symbol=symbol, orderId=order_id)
        return self._normalize(raw)

    def create_limit_order(
        self,
        symbol:         str,
        side:           str,
        qty:            float,
        price:          float,
        price_decimals: int,
    ) -> OrderResult:
        raw = self._client.create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_LIMIT,
            timeInForce=Client.TIME_IN_FORCE_GTC,
            quantity=qty,
            price=f"{price:.{price_decimals}f}",
        )
        return self._normalize(raw)

    def create_market_order(
        self,
        symbol: str,
        side:   str,
        qty:    float,
        reference_price: float | None = None,
    ) -> OrderResult:
        raw = self._client.create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_MARKET,
            quantity=qty,
        )
        result = self._normalize(raw)
        # Sur certains symboles, executedQty est absent de la réponse directe
        # mais les fills sont présents — on reconstruit depuis les fills.
        if result.executed_qty <= 0 and raw.get("fills"):
            fills      = raw["fills"]
            total_qty  = sum(float(f["qty"])                      for f in fills)
            total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            result = OrderResult(
                order_id=result.order_id,
                status=result.status,
                executed_qty=total_qty,
                cum_quote_qty=total_cost,
            )
        return result

    # ── Soldes ───────────────────────────────────────────────────

    def get_balances(
        self,
        quote_asset: str,
        base_asset:  str,
    ) -> tuple[float, float]:
        now   = time.time()
        cache = self._balance_cache
        if now - cache["timestamp"] < self._BALANCE_CACHE_TTL:
            return cache["quote"], cache["base"]
        try:
            acc  = self._client.get_account()
            bals = {b["asset"]: float(b["free"]) for b in acc["balances"]}
            quote = bals.get(quote_asset, 0.0)
            base  = bals.get(base_asset,  0.0)
            cache.update({"quote": quote, "base": base, "timestamp": now})
            return quote, base
        except Exception:
            logger.exception("❌ Erreur récupération soldes réels")
            return cache["quote"], cache["base"]

    def invalidate_balance_cache(self) -> None:
        self._balance_cache["timestamp"] = 0.0

    # ── WebSocket ────────────────────────────────────────────────

    def get_ws_stream_url(self, symbol: str) -> str:
        """Ex : "wss://stream.binance.com/ws/injusdc@trade" """
        return f"wss://stream.binance.com/ws/{symbol.lower()}@trade"

    def parse_ws_trade_price(self, raw_message: str) -> float | None:
        """
        Format Binance @trade :
            {"e": "trade", "p": "22.4500", ...}
        Le champ "p" contient le prix de la transaction.
        """
        try:
            price = float(json.loads(raw_message).get("p", 0))
            return price if price > 0 else None
        except Exception:
            return None

    # ── Erreurs ──────────────────────────────────────────────────

    def is_rate_limit_error(self, exception: Exception) -> bool:
        """Code -1003 = trop de requêtes sur Binance."""
        return (
            isinstance(exception, BinanceAPIException)
            and exception.code == -1003
        )

    # ── Helper interne ───────────────────────────────────────────

    @staticmethod
    def _normalize(raw: dict) -> OrderResult:
        """Convertit une réponse brute Binance en OrderResult unifié."""
        return OrderResult(
            order_id=raw.get("orderId"),
            status=raw.get("status", ""),
            executed_qty=float(raw.get("executedQty", 0.0)),
            cum_quote_qty=float(raw.get("cummulativeQuoteQty", 0.0)),
        )


# ══════════════════════════════════════════════════════════════════
# RÉTROCOMPATIBILITÉ (wrappers module-level)
# ══════════════════════════════════════════════════════════════════
# Ces fonctions permettent aux scripts qui importaient directement
# get_client / get_real_balances / invalidate_balance_cache de
# continuer à fonctionner sans modification pendant la migration.
#
# Le bot V103+ utilisera directement ExchangeBinance() à la place.

_default_instance: ExchangeBinance | None = None


def _get_default() -> ExchangeBinance:
    global _default_instance
    if _default_instance is None:
        _default_instance = ExchangeBinance()
    return _default_instance


def get_client() -> Client:
    """Rétrocompatibilité : retourne le client python-binance interne."""
    return _get_default()._client


def get_real_balances(
    quote_asset: str,
    base_asset:  str,
) -> tuple[float, float]:
    """Rétrocompatibilité : délègue à ExchangeBinance.get_balances()."""
    return _get_default().get_balances(quote_asset, base_asset)


def invalidate_balance_cache() -> None:
    """Rétrocompatibilité : délègue à ExchangeBinance.invalidate_balance_cache()."""
    _get_default().invalidate_balance_cache()


#----------Multi-echangeurs----------------

def get_balance(self, asset: str) -> float:
    """Solde disponible (free) pour l'actif."""
    bal = self._client.get_asset_balance(asset=asset)
    return float(bal["free"])

def get_my_trades(self, symbol: str, from_id: int | None = None,
                  start_time: int | None = None, limit: int = 1000) -> list[dict]:
    """
    Récupère les trades et les normalise.
    """
    kwargs = {"symbol": symbol, "limit": limit}
    if from_id is not None:
        kwargs["fromId"] = from_id
    if start_time is not None:
        kwargs["startTime"] = start_time

    raw = self._client.get_my_trades(**kwargs)
    # Normalisation des champs
    normalized = []
    for t in raw:
        normalized.append({
            "id":              t["id"],
            "quoteQty":        float(t["quoteQty"]),
            "commission":      float(t["commission"]),
            "commissionAsset": t["commissionAsset"],
            "isBuyer":         t["isBuyer"],
        })
    return normalized
