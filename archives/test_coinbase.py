from exchange_coinbase import ExchangeCoinbase

ex = ExchangeCoinbase()

print(ex.get_balances("USDC", "FIL"))
print(ex.get_ticker_price("FIL-USDC"))
print(ex.get_symbol_info("FIL-USDC"))
