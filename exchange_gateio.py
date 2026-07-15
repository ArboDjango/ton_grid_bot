"""
exchange_gateio.py

Implémentation Gate.io Spot de l'interface ExchangeBase.
Utilise le SDK officiel gate-api.

Version : 1.1
Changements v1.1 :
  - Pré-validation locale (min_qty, min_notional) dans create_limit_order /
    create_market_order : fail-fast avant d'atteindre Gate.io.
  - get_symbol_info() : ApiException traduite en ValueError (contrat ExchangeBase).
  - cancel_order()    : silencieux sur ORDER_NOT_FOUND / ORDER_CLOSED
                        (ordre déjà terminal).
  - _normalize()      : fallback avg_deal_price × executed_qty si filled_total
                        est absent (robustesse ordres market partiellement exécutés).
  - get_open_orders() : filtre défensif status=="open" en sortie.
  - get_order()      : fallback sur list_orders(status="finished") quand
                        GET /spot/orders/{id} retourne 404 (ordres annulés).
  - _extract_gate_label() : parsing centralisé du body ApiException Gate.io.

Particularités Gate.io vs Binance :
  - Symboles : FILUSDT → FIL_USDT  (méthode _symbol)
  - Côtés    : "buy" / "sell"        (minuscules)
  - Statuts  : "open" / "closed" / "cancelled"
  - Bougies  : pas de bucket 3m → KLINE_3M remappé sur "5m"
  - WebSocket: endpoint unique wss://api.gateio.ws/ws/v4/
               l'abonnement se fait via un message JSON post-connexion
  - Rate limit: HTTP 429 via gate_api.ApiException.status
"""

from __future__ import annotations

import json
import logging
import os
import time

import gate_api
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from exchange_base import ExchangeBase, OrderResult, SymbolInfo

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# IMPLÉMENTATION GATE.IO
# ══════════════════════════════════════════════════════════════════

class ExchangeGateIO(ExchangeBase):
    """
    Implémentation Gate.io Spot de ExchangeBase.

    Usage :
        exchange = ExchangeGateIO()
        price = exchange.get_ticker_price("FILUSDT")

    Les credentials sont lus depuis les variables d'environnement :
        GATEIO_API_KEY, GATEIO_API_SECRET
    """

    NAME = "Gate.io"
    DEFAULT_QUOTE = "USDT"

    # ── Constantes Gate.io (override de ExchangeBase) ────────────
    SIDE_BUY  = "buy"
    SIDE_SELL = "sell"

    TIME_IN_FORCE_GTC = "gtc"

    # Gate.io ne propose pas de bucket 3m — 5m est le plus proche.
    # Le bot utilise exchange.KLINE_3M : en overridant ici,
    # aucune modification du moteur n'est nécessaire.
    KLINE_3M  = "5m"
    KLINE_15M = "15m"

    STATUS_NEW              = "NEW"
    STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
    STATUS_FILLED           = "FILLED"
    STATUS_CANCELED         = "CANCELED"
    # Gate.io n'expose pas REJECTED / EXPIRED comme états distincts.
    # Les ordres invalides ou expirés apparaissent comme "cancelled".
    STATUS_REJECTED         = "CANCELED"
    STATUS_EXPIRED          = "CANCELED"

    _BALANCE_CACHE_TTL = 5.0  # secondes

    # ── Init ─────────────────────────────────────────────────────

    def __init__(self) -> None:
        configuration = gate_api.Configuration(
            host="https://api.gateio.ws/api/v4",
            key=os.getenv("GATEIO_API_KEY"),
            secret=os.getenv("GATEIO_API_SECRET"),
        )
        self._client = gate_api.ApiClient(configuration)
        self._spot   = gate_api.SpotApi(self._client)

        self._balance_cache: dict = {
            "balances": {},
            "timestamp": 0.0,
        }

        # Cache SymbolInfo : les précisions d'une paire ne changent jamais
        # en production — pas de TTL nécessaire.
        self._symbol_info_cache: dict[str, SymbolInfo] = {}

    # ── Conversion de symbole ─────────────────────────────────────

    @staticmethod
    def _symbol(symbol: str) -> str:
        """Convertit le format Binance vers Gate.io : FILUSDT → FIL_USDT."""
        symbol = symbol.upper()
        if symbol.endswith("USDT"):
            return symbol[:-4] + "_USDT"
        if symbol.endswith("USDC"):
            return symbol[:-4] + "_USDC"
        return symbol

    # ── Prix ─────────────────────────────────────────────────────

    def get_ticker_price(self, symbol: str) -> float | None:
        try:
            ticker = self._spot.list_tickers(currency_pair=self._symbol(symbol))
            if not ticker or ticker[0].last is None:
                return None
            price = float(ticker[0].last)
            return price if price > 0 else None
        except Exception:
            logger.exception(f"❌ Erreur prix {symbol}")
            return None

    # ── Symbole ──────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """
        Récupère les précisions et contraintes d'une paire.

        CurrencyPair Gate.io :
          precision        → price_decimals  (int, ex: 4 pour FIL/USDT)
          amount_precision → qty_decimals    (int)
          min_base_amount  → min_qty         (str → float)
          min_quote_amount → min_notional    (str → float)

        Lève ValueError si le symbole est introuvable (contrat ExchangeBase).
        """
        pair   = self._symbol(symbol)
        cached = self._symbol_info_cache.get(pair)
        if cached is not None:
            return cached

        try:
            pair_info = self._spot.get_currency_pair(pair)
        except gate_api.ApiException as e:
            raise ValueError(
                f"Symbole {symbol} introuvable sur Gate.io "
                f"(HTTP {e.status} — {self._extract_gate_label(e.body) or e.reason})"
            ) from e

        info = SymbolInfo(
            price_decimals = int(pair_info.precision        or 4),
            qty_decimals   = int(pair_info.amount_precision or 2),
            min_qty        = float(pair_info.min_base_amount  or 0.0),
            min_notional   = float(pair_info.min_quote_amount or 0.0),
        )
        self._symbol_info_cache[pair] = info
        return info

    # ── Données de marché ─────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """
        Retourne un DataFrame OHLCV avec les colonnes standard.

        Gate.io renvoie list[list[str]] avec l'ordre réel :
          [0] timestamp   (Unix secondes)
          [1] vol_quote   (volume en devise de cotation)
          [2] close
          [3] high
          [4] low
          [5] open
          [6] vol_base    (volume en token de base)
          [7] is_closed   (bougie fermée : "true" / "false")

        On mappe vol_base [6] comme Binance expose le base asset volume.
        """
        raw = self._spot.list_candlesticks(
            self._symbol(symbol),
            limit=limit,
            interval=interval,
        )
        if not raw:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        data = [
            {
                "open":   float(row[5]),
                "high":   float(row[3]),
                "low":    float(row[4]),
                "close":  float(row[2]),
                # vol_base (index 6) si présent, sinon vol_quote (index 1)
                "volume": float(row[6]) if len(row) > 6 else float(row[1]),
            }
            for row in raw
        ]
        return pd.DataFrame(data)

    # ── Ordres ───────────────────────────────────────────────────

    def get_open_orders(self, symbol: str) -> list[dict]:
        """
        Retourne tous les ordres ouverts pour le symbole.

        Gate.io limite les ordres ouverts à 100 par page.
        On pagine jusqu'à épuisement pour supporter plusieurs milliers d'ordres.
        Chaque dict est normalisé selon la convention ExchangeBinance.
        """
        pair   = self._symbol(symbol)
        result = []
        page   = 1

        while True:
            batch = self._spot.list_orders(
                currency_pair=pair,
                status="open",
                page=page,
                limit=100,
            )
            if not batch:
                break
            result.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        return [
            {
                "order_id":     o.id,
                # ExchangeBase impose BUY/SELL, indépendamment du protocole
                # interne Gate.io (buy/sell).
                "side":         str(o.side).upper(),
                "orig_qty":     float(o.amount       or 0),
                "executed_qty": float(o.filled_amount or 0),
                "price":        float(o.price         or 0),
                "status":       self._to_status(o),
            }
            for o in result
            # Garde défensive : exclure tout ordre dont Gate.io aurait changé le statut
            # entre la pagination et la normalisation (concurrence d'exécution).
            if o.status == "open"
        ]

    def cancel_order(self, symbol: str, order_id: str | int) -> None:
        """
        Annule un ordre.

        Si l'ordre est déjà dans un état terminal (exécuté ou déjà annulé),
        Gate.io retourne HTTP 400 avec label ORDER_NOT_FOUND ou ORDER_CLOSED.
        On absorbe silencieusement ces cas — l'objectif (ordre absent du carnet)
        est déjà atteint — et on relève tout autre échec.
        """
        try:
            self._spot.cancel_order(
                order_id=str(order_id),
                currency_pair=self._symbol(symbol),
            )
        except gate_api.ApiException as e:
            label = self._extract_gate_label(e.body)
            if e.status == 400 and label in ("ORDER_NOT_FOUND", "ORDER_CLOSED"):
                logger.warning(
                    f"⚠️ cancel_order({order_id}) ignoré : ordre déjà terminal "
                    f"[{label}]"
                )
                return
            raise

    def get_order(self, symbol: str, order_id: str | int) -> OrderResult:
        """
        Retourne le statut d'un ordre, quel que soit son état.

        Contrairement à Binance, Gate.io retourne 404 (ORDER_NOT_FOUND) sur
        GET /spot/orders/{id} pour les ordres annulés — ils ne sont accessibles
        que via l'endpoint des ordres terminés (status="finished").

        Stratégie :
          1. Appel direct get_order() — couvre les ordres ouverts et exécutés.
          2. Sur 404 → fallback sur list_orders(status="finished") qui retourne
             tous les ordres terminaux (filled + cancelled), les plus récents
             en premier.  On pagine jusqu'à 5 pages × 100 = 500 ordres.
        """
        oid  = str(order_id)
        pair = self._symbol(symbol)

        try:
            raw = self._spot.get_order(order_id=oid, currency_pair=pair)
            return self._normalize(raw)
        except gate_api.ApiException as e:
            # Tout autre code que 404 est une vraie erreur → on propage.
            if e.status != 404:
                raise
            # 404 ORDER_NOT_FOUND → l'ordre est probablement terminal.

        return self._find_in_finished_orders(pair, oid, symbol)

    def create_limit_order(
        self,
        symbol:         str,
        side:           str,
        qty:            float,
        price:          float,
        price_decimals: int,
    ) -> OrderResult:
        """
        Crée un ordre limite GTC.

        Valide localement min_qty et min_notional avant d'envoyer à Gate.io
        (fail-fast : évite un aller-retour réseau pour une erreur prévisible).
        qty est formaté selon qty_decimals de la paire (récupéré via cache).
        price est formaté selon price_decimals (fourni par l'appelant).
        """
        info = self.get_symbol_info(symbol)
        self._validate_order(symbol, qty, info, price=price)

        order = gate_api.Order(
            currency_pair  = self._symbol(symbol),
            side           = side,
            amount         = f"{qty:.{info.qty_decimals}f}",
            price          = f"{price:.{price_decimals}f}",
            type           = "limit",
            time_in_force  = self.TIME_IN_FORCE_GTC,
        )
        raw = self._spot.create_order(order)
        return self._normalize(raw)

    def create_market_order(
        self,
        symbol: str,
        side:   str,
        qty:    float,
        reference_price: float | None = None,
    ) -> OrderResult:
        """
        Crée un ordre marché.

        Valide min_qty localement (pas de prix connu → min_notional non vérifiable).
        Le moteur reste exprimé en quantité d'actif. La conversion en devise de cotation est spécifique à Gate.io et est effectuée ici. Un re-fetch défensif est effectué si executed_qty est absent
        (race condition réseau rare).
        """
        info = self.get_symbol_info(symbol)
        self._validate_order(symbol, qty, info)  # price=None → skip min_notional

        # Conversion spécifique Gate.io
        if side == self.SIDE_BUY:
            if reference_price is None:
                raise ValueError("reference_price requis pour MARKET BUY Gate.io")

            amount = qty * reference_price      # qty (FIL) -> USDT
        else:
            amount = qty                        # qty reste en FIL

        order = gate_api.Order(
            currency_pair = self._symbol(symbol),
            side          = side,
            amount=f"{amount:.8f}",
            type          = "market",
            time_in_force = "ioc",
        )
        raw    = self._spot.create_order(order)
        result = self._normalize(raw)

        # Sur certains symboles, filled_amount peut être absent de la réponse
        # immédiate alors que l'ordre est déjà exécuté — on re-fetch une fois.
        if result.executed_qty <= 0 and raw.id:
            try:
                refreshed = self._spot.get_order(
                    order_id=str(raw.id),
                    currency_pair=self._symbol(symbol),
                )
                result = self._normalize(refreshed)
            except Exception:
                logger.exception(f"❌ Erreur refresh ordre market {symbol}")

        return result

    #----
    def get_my_trades(
        self,
        symbol: str,
        *,
        start_time: int | None = None,
        from_id: str | int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Historique des exécutions, format normalisé.

        Retour :
            [{
                "trade_id": int,
                "order_id": str,
                "price": float,
                "qty": float,
                "quote_qty": float,
                "commission": float,
                "commission_asset": str,
                "is_buyer": bool,
                "timestamp": int,
            }, ...]
        """
        pair = self._symbol(symbol)

        trades = self._spot.list_my_trades(
            currency_pair=pair,
            limit=min(limit, 1000),
        )

        out = []

        for t in trades:
            
            ts = int(float(t.create_time_ms))
            trade_id = int(t.id)
            
            if start_time is not None and ts < start_time:
                continue

            # Compatible avec la logique Binance
            if from_id is not None and trade_id < int(from_id):
                continue

            qty = float(t.amount)
            price = float(t.price)

            out.append({
                "id": trade_id,
                "order_id": str(t.order_id),
                "price": price,
                "qty": qty,
                "quoteQty": qty * price,
                "commission": float(t.fee),
                "commissionAsset": t.fee_currency,
                "isBuyer": t.side == "buy",
                "time": ts,
            })

        out.sort(key=lambda x: x["id"])

        return out
    
    # ── Soldes ───────────────────────────────────────────────────
    
    def get_balance(self, asset: str) -> float:
        """
        Retourne le solde disponible (free) d'un actif.
        """
        try:
            accounts = self._spot.list_spot_accounts()
            for a in accounts:
                if a.currency.upper() == asset.upper():
                    return float(a.available)
            return 0.0
        except Exception:
            logger.exception(f"❌ Erreur récupération solde {asset}")
            return 0.0
    
    def get_balances(
        self,
        quote_asset: str,
        base_asset: str,
    ) -> tuple[float, float]:
        """
        Retourne les soldes disponibles (hors ordres ouverts) avec cache TTL.

        Le cache mémorise l'ensemble des soldes du compte et non plus
        uniquement un couple (quote/base). Ainsi plusieurs paires peuvent
        interroger les soldes sans se polluer mutuellement.
        """
        now = time.time()
        cache = self._balance_cache

        # Cache valide
        if now - cache["timestamp"] < self._BALANCE_CACHE_TTL:
            balances = cache["balances"]
        else:
            try:
                accounts = self._spot.list_spot_accounts()
                balances = {
                    a.currency.upper(): float(a.available)
                    for a in accounts
                }
                cache["balances"] = balances
                cache["timestamp"] = now
            except Exception:
                logger.exception("❌ Erreur récupération soldes réels")
                balances = cache.get("balances", {})

        quote = balances.get(quote_asset.upper(), 0.0)
        base = balances.get(base_asset.upper(), 0.0)

        return quote, base

    def invalidate_balance_cache(self) -> None:
        """Force le prochain get_balances() à aller chercher en live sur l'API."""
        self._balance_cache["timestamp"] = 0.0
        
        
    def get_quote_balance(self) -> float:
        return self.get_balance("USDT")

    # ── WebSocket ────────────────────────────────────────────────

        
    def get_ws_stream_url(self, symbol: str) -> str:
        """
        Gate.io utilise un endpoint WebSocket unique pour toutes les paires.
        L'abonnement au canal spot.trades se fait en envoyant un message JSON
        après connexion (à la charge du gestionnaire WebSocket du bot) :

            {
              "time": <unix_ts>,
              "channel": "spot.trades",
              "event": "subscribe",
              "payload": ["FIL_USDT"]
            }
        """
        return "wss://api.gateio.ws/ws/v4/"
        
        
    def get_ws_subscribe_message(self, symbol: str) -> dict:
        return {
            "time": int(time.time()),
            "channel": "spot.trades",
            "event": "subscribe",
            "payload": [self._symbol(symbol)],
        }

    def parse_ws_trade_price(self, raw_message: str) -> float | None:
        """
        Extrait le prix depuis un message Gate.io spot.trades.

        Format du message entrant (event=update) :
            {
              "channel": "spot.trades",
              "event":   "update",
              "result":  {
                "id":            12345,
                "currency_pair": "FIL_USDT",
                "price":         "6.1234",
                "amount":        "10.0",
                "side":          "buy",
                ...
              }
            }

        Le champ "price" est dans result (contrairement à Binance où il est dans "p").
        """
        try:
            msg    = json.loads(raw_message)
            result = msg.get("result")
            if not isinstance(result, dict):
                return None
            price = float(result.get("price", 0))
            return price if price > 0 else None
        except Exception:
            return None

    # ── Gestion des erreurs ──────────────────────────────────────

    def is_rate_limit_error(self, exception: Exception) -> bool:
        """HTTP 429 = trop de requêtes sur Gate.io."""
        return (
            isinstance(exception, gate_api.ApiException)
            and exception.status == 429
        )

    # ── Helpers internes ─────────────────────────────────────────

    def _find_in_finished_orders(
        self,
        pair:   str,
        oid:    str,
        symbol: str,
    ) -> OrderResult:
        """
        Recherche un ordre dans l'historique des ordres terminés Gate.io.

        list_orders(status="finished") retourne les ordres finished (filled +
        cancelled) du plus récent au plus ancien.  On pagine jusqu'à 5 pages
        de 100 (= 500 ordres), ce qui est largement suffisant pour un bot
        dont les ordres sont par définition très récents.

        Lève ValueError si l'ordre reste introuvable après épuisement des pages.
        """
        for page in range(1, 6):
            batch = self._spot.list_orders(
                currency_pair=pair,
                status="finished",
                limit=100,
                page=page,
            )
            if not batch:
                break
            for order in batch:
                if str(order.id) == oid:
                    return self._normalize(order)
            if len(batch) < 100:
                break  # dernière page atteinte

        raise ValueError(
            f"❌ Ordre {oid} introuvable pour {symbol} "
            f"(absent des ordres ouverts et des 500 derniers ordres terminés)"
        )

    @staticmethod
    def _extract_gate_label(body) -> str:
        """
        Extrait le label d'erreur depuis le body d'une gate_api.ApiException.

        Gate.io retourne : {"label": "ORDER_NOT_FOUND", "message": "..."}
        Retourne "" si le body est absent ou non parsable.
        """
        if not body:
            return ""
        try:
            text = body.decode() if isinstance(body, bytes) else str(body)
            return json.loads(text).get("label", "")
        except Exception:
            return ""

    @staticmethod
    def _validate_order(
        symbol: str,
        qty:    float,
        info:   SymbolInfo,
        price:  float | None = None,
    ) -> None:
        """
        Valide localement les contraintes min_qty et min_notional avant d'envoyer
        l'ordre à Gate.io.  Lève ValueError avec message explicite si un critère
        n'est pas satisfait — évite un aller-retour réseau pour une erreur
        prévisible (INVALID_PARAM_VALUE).

        price=None  →  la vérification min_notional est ignorée
                       (ordres market : le prix d'exécution est inconnu à l'avance).
        """
        if info.min_qty > 0 and qty < info.min_qty:
            raise ValueError(
                f"❌ Quantité trop faible pour {symbol} : "
                f"{qty} < min_qty {info.min_qty}"
            )
        if price is not None and info.min_notional > 0:
            notional = qty * price
            if notional < info.min_notional:
                raise ValueError(
                    f"❌ Notionnel trop faible pour {symbol} : "
                    f"{notional:.4f} < min_notional {info.min_notional}"
                )

    @staticmethod
    def _to_status(order: gate_api.Order) -> str:
        """
        Traduit le statut Gate.io vers la nomenclature ExchangeBase.

        Mapping :
          "closed"    → "FILLED"
          "cancelled" → "CANCELED"
          "open" + filled_amount > 0 → "PARTIALLY_FILLED"
          "open" + filled_amount = 0 → "NEW"
        """
        gateio_status = order.status or ""
        if gateio_status == "closed":
            return "FILLED"
        if gateio_status == "cancelled":
            return "CANCELED"
        if gateio_status == "open":
            filled = float(order.filled_amount or 0)
            return "PARTIALLY_FILLED" if filled > 0 else "NEW"
        # Statut inconnu — fallback conservateur pour ne pas bloquer le bot.
        return "NEW"

    @classmethod
    def _normalize(cls, order: gate_api.Order) -> OrderResult:
        """
        Convertit un objet gate_api.Order en OrderResult unifié.

        Équivalences Gate.io → ExchangeBase :
          order.id            → order_id
          order.filled_amount → executed_qty   (quantité exécutée en base asset)
          order.filled_total  → cum_quote_qty  ("Total filled in quote currency")

        Fallback cum_quote_qty : si filled_total est absent ou nul (rare sur
        ordres market partiellement exécutés), on reconstruit via
        avg_deal_price × executed_qty — ce qui donne le même résultat que
        OrderResult.avg_price le ferait dans l'autre sens.
        """
        executed_qty  = float(order.filled_amount or 0)
        cum_quote_qty = float(order.filled_total  or 0)

        if cum_quote_qty <= 0 and executed_qty > 0 and order.avg_deal_price:
            try:
                cum_quote_qty = executed_qty * float(order.avg_deal_price)
            except (TypeError, ValueError):
                pass  # laisser cum_quote_qty à 0 — avg_price retournera 0.0

        return OrderResult(
            order_id      = order.id,
            status        = cls._to_status(order),
            executed_qty  = executed_qty,
            cum_quote_qty = cum_quote_qty,
        )
