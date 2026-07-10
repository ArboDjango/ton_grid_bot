"""
exchange_base.py

Interface abstraite pour les connexions d'exchange.
Permet de basculer entre Binance, Coinbase ou Gate.io
sans modifier la logique métier du bot.

Version : 1.1
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
import pandas as pd




# ══════════════════════════════════════════════════════════════════
# STRUCTURES DE DONNÉES NORMALISÉES
# ══════════════════════════════════════════════════════════════════

@dataclass
class SymbolInfo:
    """
    Précisions et contraintes d'un symbole de trading.

    Obtenu via ExchangeBase.get_symbol_info(symbol).
    """
    price_decimals: int    # décimales du prix (ex: 4 pour INJ/USDC)
    qty_decimals:   int    # décimales de la quantité
    min_qty:        float  # quantité minimale par ordre
    min_notional:   float  # valeur minimale d'un ordre en quote


@dataclass
class OrderResult:
    """
    Résultat normalisé d'un ordre (création ou consultation).

    Retourné par : create_limit_order, create_market_order, get_order.
    """
    order_id:      str | int  # int sur Binance, str sur Coinbase/Gate.io
    status:        str         # voir constantes STATUS_* de l'exchange
    executed_qty:  float       # quantité exécutée en base asset
    cum_quote_qty: float       # montant total exécuté en quote asset

    @property
    def avg_price(self) -> float:
        """Prix moyen d'exécution. Retourne 0.0 si rien n'a été exécuté."""
        if self.executed_qty > 0:
            return self.cum_quote_qty / self.executed_qty
        return 0.0

    @property
    def is_filled(self) -> bool:
        """True si l'ordre est entièrement exécuté."""
        return self.status == "FILLED"

    @property
    def is_terminal(self) -> bool:
        """True si l'ordre est dans un état définitif (ne peut plus évoluer)."""
        return self.status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")


@dataclass
class TradeResult:
    """
    Trade exécuté (historique).

    Utilisé par audit.py pour reconstruire le PnL.
    """
    trade_id: int | str
    is_buyer: bool
    quote_qty: float
    commission: float
    commission_asset: str
    timestamp: int    # epoch ms

# ══════════════════════════════════════════════════════════════════
# INTERFACE ABSTRAITE
# ══════════════════════════════════════════════════════════════════

class ExchangeBase(ABC):
    """
    Interface commune pour tous les exchanges supportés.

    Chaque exchange implémente cette classe et fournit :
    - Méthodes REST : prix, ordres, soldes, klines, historique de trades
    - Constantes de protocole : côtés, types d'ordres, statuts
    - Helpers WebSocket : URL du stream, parsing des messages

    ── Exemple d'usage dans le bot ─────────────────────────────────
        exchange = ExchangeBinance()

        price  = exchange.get_ticker_price(SYMBOL)
        df_3m  = exchange.get_klines(SYMBOL, exchange.KLINE_3M, 100)

        result = exchange.create_limit_order(
            symbol        = SYMBOL,
            side          = exchange.SIDE_BUY,
            qty           = qty_asset,
            price         = maker_price,
            price_decimals= PRICE_DECIMALS,
        )
        if result.is_filled:
            print(f"Exécuté à {result.avg_price:.4f}")

    ── Ajouter un nouvel exchange ───────────────────────────────────
        1. Créer exchange_gateio.py (ou exchange_okx.py)
        2. Implémenter ExchangeBase
        3. Overrider les constantes de classe si elles diffèrent
        4. Changer la ligne d'instanciation dans le bot :
               exchange = ExchangeGateIO()
    """

    NAME = "Base"
    DEFAULT_QUOTE = "USDC"

    # ── Constantes de protocole ──────────────────────────────────
    # Valeurs par défaut (standard industrie / compatibles Binance).
    # Les sous-classes overrident uniquement ce qui diffère.

    SIDE_BUY:  str = "BUY"
    SIDE_SELL: str = "SELL"

    ORDER_TYPE_LIMIT:  str = "LIMIT"
    ORDER_TYPE_MARKET: str = "MARKET"
    TIME_IN_FORCE_GTC: str = "GTC"

    KLINE_3M:  str = "3m"   # override si l'exchange utilise une notation différente
    KLINE_15M: str = "15m"

    STATUS_NEW:              str = "NEW"
    STATUS_PARTIALLY_FILLED: str = "PARTIALLY_FILLED"
    STATUS_FILLED:           str = "FILLED"
    STATUS_CANCELED:         str = "CANCELED"
    STATUS_REJECTED:         str = "REJECTED"
    STATUS_EXPIRED:          str = "EXPIRED"

    # ── Prix ─────────────────────────────────────────────────────

    @abstractmethod
    def get_ticker_price(self, symbol: str) -> float | None:
        """
        Dernier prix connu via REST (ticker).
        Retourne None en cas d'erreur réseau ou de symbole invalide.
        """
        ...

    # ── Symbole ──────────────────────────────────────────────────

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """
        Précisions et contraintes du symbole.
        Lève ValueError si le symbole est introuvable sur l'exchange.
        """
        ...

    # ── Données de marché ─────────────────────────────────────────

    @abstractmethod
    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """
        Bougies OHLCV pour un symbole.

        Le DataFrame retourné contient au minimum les colonnes float :
            open, high, low, close, volume
        Indexées chronologiquement (dernière ligne = bougie la plus récente).

        interval : utiliser les constantes KLINE_3M, KLINE_15M, etc.
        """
        ...

    # ── Ordres ───────────────────────────────────────────────────

    @abstractmethod
    def get_open_orders(self, symbol: str) -> list[dict]:
        """
        Ordres ouverts pour le symbole, format normalisé.

        Chaque dict contient :
            order_id:     str | int   — identifiant de l'ordre
            side:         "BUY" | "SELL"
            orig_qty:     float       — quantité originale
            executed_qty: float       — quantité déjà exécutée
            price:        float       — prix limite
            status:       str         — "NEW" | "PARTIALLY_FILLED"
        """
        ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str | int) -> None:
        """Annule un ordre. Lève une exception en cas d'échec."""
        ...

    @abstractmethod
    def get_order(self, symbol: str, order_id: str | int) -> OrderResult:
        """Statut et exécution courants d'un ordre existant."""
        ...

    @abstractmethod
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

        price_decimals : décimales à utiliser pour le prix.
                         Obtenu via get_symbol_info().price_decimals.
        """
        ...

    @abstractmethod
    def create_market_order(
        self,
        symbol: str,
        side:   str,
        qty:    float,
        reference_price: float | None = None,
    ) -> OrderResult:
        """
        Crée un ordre marché.

        qty représente TOUJOURS une quantité de l'actif de base.

        Certains exchanges (ex: Gate.io) peuvent utiliser
        reference_price pour convertir cette quantité en montant
        de devise de cotation lors d'un MARKET BUY.

        L'OrderResult.avg_price doit être calculé
        (cum_quote_qty / executed_qty).
        """
        ...

    # ── Soldes ───────────────────────────────────────────────────

    @abstractmethod
    def get_balance(self, asset: str) -> float:
        """Retourne le solde disponible (free) de l'actif donné."""
        ...

    @abstractmethod
    def get_quote_balance(self) -> float:
        """
        Retourne le solde libre de la devise de cotation par défaut
        (USDT pour Gate.io, USDC pour Binance, etc.).
        """

    @abstractmethod
    def get_balances(
        self,
        quote_asset: str,
        base_asset:  str,
    ) -> tuple[float, float]:
        """
        Soldes disponibles : retourne (quote_balance, base_balance).
        Les implémentations doivent gérer un cache TTL interne.
        """
        ...

    @abstractmethod
    def invalidate_balance_cache(self) -> None:
        """Force le prochain get_balances() à aller chercher en live sur l'API."""
        ...
    
    
    # ── Historique de trades ─────────────────────────────────────

    @abstractmethod
    def get_my_trades(
        self,
        symbol: str,
        from_id: str | int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Historique des trades utilisateur.

        Retour normalisé :

            trade_id
            order_id
            price
            qty
            quote_qty
            commission
            commission_asset
            is_buyer
            timestamp
        """
        ...

    # ── WebSocket ────────────────────────────────────────────────

    @abstractmethod
    def get_ws_stream_url(self, symbol: str) -> str:
        """
        URL WebSocket du flux de trades pour le symbole donné.
        Ex (Binance) : "wss://stream.binance.com/ws/injusdc@trade"
        Ex (Coinbase): "wss://advanced-trade-ws.coinbase.com"
        """
        ...

    @abstractmethod
    def parse_ws_trade_price(self, raw_message: str) -> float | None:
        """
        Extrait le prix d'un message WebSocket JSON brut.
        Retourne None si le message est invalide ou ne contient pas de prix.
        """
        ...

    # ── Gestion des erreurs ──────────────────────────────────────

    def is_rate_limit_error(self, exception: Exception) -> bool:
        """
        Retourne True si l'exception est une erreur de rate-limit exchange.
        Méthode optionnelle — override dans les sous-classes si applicable.
        Ex (Binance) : BinanceAPIException avec code -1003.
        """
        return False
