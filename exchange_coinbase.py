"""
exchange_coinbase.py

Implémentation Coinbase Advanced Trade de l'interface ExchangeBase.
Utilise le SDK officiel coinbase-advanced-py.

Version : 1.0
Méthodes implémentées : init, get_balances, get_ticker_price,
                        get_symbol_info, get_klines, invalidate_balance_cache
Ordres et WebSocket : NotImplementedError (phase ultérieure).
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from coinbase.rest import RESTClient
from requests.exceptions import RequestException

from exchange_base import ExchangeBase, OrderResult, SymbolInfo

logger = logging.getLogger(__name__)

DEFAULT_KEY_FILE = os.path.expanduser("~/.coinbase/cdp_api_key.json")

# Granularités Coinbase Advanced Trade (pas de bucket 3m natif)
_GRANULARITY_SECONDS: dict[str, int] = {
    "ONE_MINUTE":     60,
    "FIVE_MINUTE":    300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE":  1800,
    "ONE_HOUR":       3600,
    "TWO_HOUR":       7200,
    "SIX_HOUR":       21600,
    "ONE_DAY":        86400,
}

# Mapping des intervales style Binance → granularité Coinbase
_INTERVAL_TO_GRANULARITY: dict[str, str] = {
    "3m":  "FIVE_MINUTE",    # pas de 3m sur Coinbase — bucket le plus proche
    "15m": "FIFTEEN_MINUTE",
    **{g: g for g in _GRANULARITY_SECONDS},
}


class ExchangeCoinbase(ExchangeBase):
    """
    Implémentation Coinbase Advanced Trade de ExchangeBase.

    Usage :
        exchange = ExchangeCoinbase()
        price = exchange.get_ticker_price("INJUSDC")

    Les credentials sont lus depuis ~/.coinbase/cdp_api_key.json par défaut
    (format CDP : {"name": "...", "privateKey": "..."}).
    """

    # Coinbase n'offre pas des bougies 3m ; FIVE_MINUTE est le bucket le plus proche.
    KLINE_3M  = "FIVE_MINUTE"
    KLINE_15M = "FIFTEEN_MINUTE"

    STATUS_NEW              = "OPEN"
    STATUS_PARTIALLY_FILLED = "OPEN"
    STATUS_FILLED           = "FILLED"
    STATUS_CANCELED         = "CANCELLED"
    STATUS_REJECTED         = "REJECTED"
    STATUS_EXPIRED          = "EXPIRED"

    _BALANCE_CACHE_TTL = 5.0

    def __init__(self, key_file: str | None = None) -> None:
        path = key_file or DEFAULT_KEY_FILE
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Fichier de clé API introuvable : {path}\n"
                "Créez une clé CDP Coinbase et placez cdp_api_key.json dans ~/.coinbase/"
            )
        self._client = RESTClient(key_file=path, timeout=15)
        self._balance_cache: dict = {
            "quote":     0.0,
            "base":      0.0,
            "timestamp": 0.0,
        }
        # Cache des SymbolInfo : clé = symbol (ex. "INJUSDC"), valeur = SymbolInfo.
        # Pas de TTL : la précision d'un produit ne change jamais en production.
        self._symbol_info_cache: dict[str, SymbolInfo] = {}

    # ── Prix ─────────────────────────────────────────────────────

    def get_ticker_price(self, symbol: str) -> float | None:
        try:
            product_id = self._to_product_id(symbol)
            product = self._client.get_product(product_id)
            price = float(product.price)
            return price if price > 0 else None
        except (RequestException, ValueError):
            logger.exception(f"❌ Erreur prix {symbol}")
            return None

    # ── Symbole ──────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        # Normalisation en product_id Coinbase avant la clé de cache pour
        # traiter "INJUSDC" et "INJ-USDC" comme le même produit.
        product_id = self._to_product_id(symbol)
        cached = self._symbol_info_cache.get(product_id)
        if cached is not None:
            return cached

        try:
            product = self._client.get_product(product_id)
        except RequestException as e:
            raise ValueError(f"Symbole {symbol} introuvable sur Coinbase") from e

        price_increment = getattr(product, "price_increment", None) or getattr(
            product, "quote_increment", "0.01"
        )
        base_increment = getattr(product, "base_increment", "0.01")

        price_decimals = self._decimals_from_increment(str(price_increment))
        qty_decimals   = self._decimals_from_increment(str(base_increment))

        min_qty = float(getattr(product, "base_min_size", 0.0) or 0.0)
        min_notional = float(getattr(product, "quote_min_size", 0.0) or 0.0)

        info = SymbolInfo(
            price_decimals=price_decimals,
            qty_decimals=qty_decimals,
            min_qty=min_qty,
            min_notional=min_notional,
        )
        self._symbol_info_cache[product_id] = info
        return info

    # ── Données de marché ─────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        product_id = self._to_product_id(symbol)
        granularity = self._resolve_granularity(interval)
        bucket_secs = _GRANULARITY_SECONDS.get(granularity, 300)

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(seconds=bucket_secs * limit)
        start = str(int(start_dt.timestamp()))
        end = str(int(end_dt.timestamp()))

        response = self._client.get_candles(
            product_id=product_id,
            start=start,
            end=end,
            granularity=granularity,
            limit=limit,
        )

        rows: list[dict] = []
        for candle in response.candles or []:
            rows.append({
                "time":   int(candle.start),
                "open":   float(candle.open),
                "high":   float(candle.high),
                "low":    float(candle.low),
                "close":  float(candle.close),
                "volume": float(candle.volume),
            })

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows)
        df = df.sort_values("time").tail(limit).reset_index(drop=True)
        return df

    # ── Ordres (non implémentés) ─────────────────────────────────

    def get_open_orders(self, symbol: str) -> list[dict]:
        """
        Retourne tous les ordres OPEN pour le symbole donné.

        La pagination Coinbase utilise has_next / cursor (identique à
        _fetch_all_accounts).  Chaque ordre est normalisé en dict dont les
        clés reprennent la convention ExchangeBinance pour la compatibilité
        avec le reste du bot.
        """
        product_id = self._to_product_id(symbol)
        orders: list[dict] = []
        cursor: str | None = None

        while True:
            response = self._client.list_orders(
                product_ids=[product_id],
                order_status=["OPEN"],
                limit=100,
                cursor=cursor,
            )
            for order in (response.orders or []):
                orders.append(self._order_to_dict(order))

            if not getattr(response, "has_next", False) or not response.cursor:
                break
            cursor = response.cursor

        return orders

    def cancel_order(self, symbol: str, order_id: str | int) -> None:
        """
        Annule un ordre Coinbase.

        Le SDK n'expose qu'une API batch (cancel_orders).  On l'appelle
        avec une liste d'un seul élément et on vérifie le champ success du
        premier résultat.
        """
        response = self._client.cancel_orders(order_ids=[str(order_id)])

        if not response.results:
            raise Exception(
                f"❌ Annulation ordre {order_id} : réponse vide (aucun résultat)"
            )

        result = response.results[0]
        if not result.success:
            reason = getattr(result, "failure_reason", "inconnu")
            raise Exception(
                f"❌ Échec annulation ordre {order_id} : {reason}"
            )

        # L'annulation a réussi : le capital bloqué est libéré → cache obsolète.
        self.invalidate_balance_cache()

    def get_order(self, symbol: str, order_id: str | int) -> OrderResult:
        """
        Retourne le statut normalisé d'un ordre Coinbase.

        Points d'attention :
        - Coinbase n'a pas de statut PARTIALLY_FILLED : un ordre partiellement
          exécuté reste OPEN avec completion_percentage > 0.
        - La normalisation est déléguée à _normalize_order_status().
        """
        response = self._client.get_order(order_id=str(order_id))
        return self._order_to_result(response.order)

    def create_limit_order(
        self,
        symbol:         str,
        side:           str,
        qty:            float,
        price:          float,
        price_decimals: int,
    ) -> OrderResult:
        product_id = self._to_product_id(symbol)
        side_upper = side.upper()
        if side_upper not in (self.SIDE_BUY, self.SIDE_SELL):
            raise ValueError(f"Side invalide : {side}")

        symbol_info = self.get_symbol_info(symbol)
        base_size = f"{qty:.{symbol_info.qty_decimals}f}"
        limit_price = f"{price:.{price_decimals}f}"

        from coinbase.rest.orders import generate_client_order_id

        response = self._client.limit_order_gtc(
            client_order_id=generate_client_order_id(),
            product_id=product_id,
            side=side_upper,
            base_size=base_size,
            limit_price=limit_price,
            post_only=True,
        )

        if not response.success:
            error = response.error_response
            detail = ""
            if error:
                detail = getattr(error, "message", None) or getattr(error, "error", "") or ""
            raise Exception(
                f"Échec création ordre limite Coinbase {product_id}: {detail}"
            )

        if not response.success_response:
            raise Exception(f"Réponse Coinbase sans success_response pour {product_id}")

        order_id = response.success_response.get("order_id")

        if not order_id:
            raise Exception(f"Réponse Coinbase sans order_id pour {product_id}")

        # Le capital USDC est désormais bloqué par l'ordre → cache obsolète.
        self.invalidate_balance_cache()

        # Aligné sur ExchangeBinance : ordre limite nouvellement créé → NEW, rien exécuté.
        return OrderResult(
            order_id=order_id,
            status="NEW",
            executed_qty=0.0,
            cum_quote_qty=0.0,
        )

    def create_market_order(
        self,
        symbol: str,
        side:   str,
        qty:    float,
    ) -> OrderResult:
        """
        Passe un ordre market BUY ou SELL.

        Coinbase expose deux méthodes distinctes (market_order_buy /
        market_order_sell).  Dans les deux cas on passe base_size (quantité
        en token de base) pour rester cohérent avec l'interface ExchangeBase
        et avec create_limit_order.

        Note : un ordre market est typiquement rempli instantanément ;
        le bot peut appeler get_order() immédiatement après pour obtenir
        les champs filled_size / filled_value définitifs.
        """
        product_id = self._to_product_id(symbol)
        side_upper = side.upper()
        if side_upper not in (self.SIDE_BUY, self.SIDE_SELL):
            raise ValueError(f"Side invalide : {side}")

        symbol_info = self.get_symbol_info(symbol)
        base_size = f"{qty:.{symbol_info.qty_decimals}f}"

        from coinbase.rest.orders import generate_client_order_id
        client_order_id = generate_client_order_id()

        if side_upper == self.SIDE_BUY:
            response = self._client.market_order_buy(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=base_size,
            )
        else:
            response = self._client.market_order_sell(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=base_size,
            )

        if not response.success:
            error = response.error_response
            detail = ""
            if error:
                detail = getattr(error, "message", None) or getattr(error, "error", "") or ""
            raise Exception(
                f"❌ Échec création ordre market Coinbase {product_id} {side_upper}: {detail}"
            )

        if not response.success_response:
            raise Exception(f"Réponse Coinbase sans success_response pour {product_id}")

        order_id = response.success_response.get("order_id")
        
        if not order_id:
            raise Exception(f"Réponse Coinbase sans order_id pour {product_id}")

        # Les balances changent à l'exécution market → invalider immédiatement.
        self.invalidate_balance_cache()

        # CreateOrderResponse ne contient PAS filled_size / filled_value / status.
        # Un market_market_ioc est IOC : il peut être partiellement rempli puis
        # annulé si la liquidité manque. Seul GET /orders/{id} donne le vrai statut.
        return self.get_order(symbol, order_id)

    # ── Soldes ───────────────────────────────────────────────────

    def get_balances(
        self,
        quote_asset: str,
        base_asset:  str,
    ) -> tuple[float, float]:
        now = time.time()
        cache = self._balance_cache
        if now - cache["timestamp"] < self._BALANCE_CACHE_TTL:
            return cache["quote"], cache["base"]
        try:
            accounts = self._fetch_all_accounts()
            quote = self._balance_for_currency(accounts, quote_asset)
            base  = self._balance_for_currency(accounts, base_asset)
            cache.update({"quote": quote, "base": base, "timestamp": now})
            return quote, base
        except RequestException:
            logger.exception("❌ Erreur récupération soldes Coinbase")
            return cache["quote"], cache["base"]

    def invalidate_balance_cache(self) -> None:
        self._balance_cache["timestamp"] = 0.0

    # ── WebSocket (non implémentés) ──────────────────────────────

    def get_ws_stream_url(self, symbol: str) -> str:
        raise NotImplementedError("ExchangeCoinbase.get_ws_stream_url")

    def parse_ws_trade_price(self, raw_message: str) -> float | None:
        raise NotImplementedError("ExchangeCoinbase.parse_ws_trade_price")

    # ── Helpers internes ─────────────────────────────────────────

    @staticmethod
    def _normalize_order_status(order) -> str:
        """
        Traduit le statut Coinbase vers la nomenclature ExchangeBase.

        PARTIALLY_FILLED n'existe pas dans l'API Coinbase : un ordre
        partiellement exécuté reste OPEN avec completion_percentage > 0.
        On le détecte ici et on le re-mappe correctement.
        """
        raw = getattr(order, "status", "")
        if raw == "OPEN":
            try:
                pct = float(getattr(order, "completion_percentage", "0") or "0")
            except (TypeError, ValueError):
                pct = 0.0
            return "PARTIALLY_FILLED" if pct > 0 else "NEW"

        return {
            "FILLED":    "FILLED",
            "CANCELLED": "CANCELED",
            "EXPIRED":   "EXPIRED",
            "FAILED":    "REJECTED",
        }.get(raw, raw)

    @staticmethod
    def _extract_limit_price(order) -> float:
        """
        Extrait le prix limite depuis order_configuration.

        On inspecte limit_limit_gtc en priorité (nos ordres), puis les
        autres variantes GTC/GTD/FOK.  Retourne 0.0 si introuvable (ex.
        ordre market).
        """
        cfg = getattr(order, "order_configuration", None)
        if cfg is None:
            return 0.0
        for attr in (
            "limit_limit_gtc",
            "limit_limit_gtd",
            "limit_limit_fok",
            "sor_limit_ioc",
        ):
            sub = getattr(cfg, attr, None)
            if sub is not None:
                try:
                    return float(getattr(sub, "limit_price", 0) or 0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @staticmethod
    def _extract_orig_qty(order) -> float:
        """
        Extrait la quantité originale (base_size) depuis order_configuration.

        Couvre les ordres limite GTC/GTD/FOK et les ordres market IOC.
        Retourne 0.0 si le champ est absent ou non-parsable.
        """
        cfg = getattr(order, "order_configuration", None)
        if cfg is None:
            return 0.0
        for attr in (
            "limit_limit_gtc",
            "limit_limit_gtd",
            "limit_limit_fok",
            "sor_limit_ioc",
            "market_market_ioc",
        ):
            sub = getattr(cfg, attr, None)
            if sub is not None:
                try:
                    return float(getattr(sub, "base_size", 0) or 0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @classmethod
    def _order_to_result(cls, order) -> OrderResult:
        """Convertit un objet Order SDK en OrderResult normalisé."""
        return OrderResult(
            order_id=order.order_id,
            status=cls._normalize_order_status(order),
            executed_qty=float(getattr(order, "filled_size",  None) or 0),
            cum_quote_qty=float(getattr(order, "filled_value", None) or 0),
        )

    @classmethod
    def _order_to_dict(cls, order) -> dict:
        """
        Convertit un objet Order SDK en dict normalisé compatible
        ExchangeBinance (nommage des clés identique).
        """
        return {
            "orderId":              order.order_id,
            "clientOrderId":        getattr(order, "client_order_id", ""),
            "symbol":               getattr(order, "product_id", ""),
            "status":               cls._normalize_order_status(order),
            "side":                 getattr(order, "side", ""),
            "type":                 getattr(order, "order_type", "LIMIT"),
            "price":                cls._extract_limit_price(order),
            "origQty":              cls._extract_orig_qty(order),
            "executedQty":          float(getattr(order, "filled_size",  None) or 0),
            "cummulativeQuoteQty":  float(getattr(order, "filled_value", None) or 0),
        }

    @staticmethod
    def _to_product_id(symbol: str) -> str:
        """INJUSDC → INJ-USDC ; INJ-USDC inchangé."""
        symbol = symbol.upper()
        if "-" in symbol:
            return symbol
        if symbol.endswith("USDC"):
            return f"{symbol[:-4]}-USDC"
        if symbol.endswith("USDT"):
            return f"{symbol[:-4]}-USDT"
        if symbol.endswith("USD"):
            return f"{symbol[:-3]}-USD"
        raise ValueError(f"Paire {symbol} non supportée pour Coinbase")

    @staticmethod
    def _decimals_from_increment(increment: str) -> int:
        try:
            step = float(increment)
        except (TypeError, ValueError):
            return 2
        if step <= 0:
            return 2
        
        s = increment.rstrip("0")

        if "." not in s:
            return 0

        return len(s.split(".")[1])

    @staticmethod
    def _resolve_granularity(interval: str) -> str:
        granularity = _INTERVAL_TO_GRANULARITY.get(interval)
        if granularity is None:
            raise ValueError(
                f"Intervalle {interval!r} non supporté sur Coinbase. "
                f"Valeurs acceptées : {sorted(_INTERVAL_TO_GRANULARITY)}"
            )
        return granularity

    def _fetch_all_accounts(self) -> list:
        accounts: list = []
        cursor: str | None = None
        while True:
            response = self._client.get_accounts(limit=250, cursor=cursor)
            if response.accounts:
                accounts.extend(response.accounts)
            if not getattr(response, "has_next", False) or not response.cursor:
                break
            cursor = response.cursor
        return accounts

    
    @staticmethod
    def _balance_for_currency(accounts: list, currency: str) -> float:
        total = 0.0

        for account in accounts:
            if account.currency != currency:
                continue

            balance = account.available_balance   # <-- c'est un dict

            if balance and balance.get("value"):
                total += float(balance["value"])

        return total
