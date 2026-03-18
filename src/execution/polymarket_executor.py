import os
from py_clob_client import ClobClient

class PolymarketExecutor:
    def __init__(self):
        private_key = os.environ['POLYMARKET_PRIVATE_KEY']
        funder_address = os.environ['POLYMARKET_FUNDER_ADDRESS']
        self.client = ClobClient(private_key, funder_address, 'https://clob.polymarket.com', chain_id=137)

    def buy_yes(self, token_id, amount_usdc):
        self.client.place_order(token_id, 'YES', amount_usdc, 'FOK')

    def buy_no(self, token_id, amount_usdc):
        self.client.place_order(token_id, 'NO', amount_usdc, 'FOK')

    def get_balance(self):
        return self.client.get_balance()

    def get_positions(self):
        return self.client.get_positions()

    def cancel_all(self):
        self.client.cancel_all_orders()
