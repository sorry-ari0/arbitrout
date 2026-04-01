# Wallet Setup & Live Trading Guide

**Last updated:** 2026-03-27

This guide walks through every step to go from paper trading to live trading with real money.

---

## Architecture: Paper vs Live

The system fully separates paper and live environments:

| Component | Paper Mode | Live Mode |
|-----------|-----------|-----------|
| Positions file | `positions_paper.json` | `positions_live.json` |
| Trade journal | `trade_journal_paper.json` | `trade_journal_live.json` |
| Decision log | Shared (`decision_log.jsonl`) | Shared |
| Executors | PaperExecutor wrappers | Real exchange API calls |
| AI advisor | Groq → Gemini → OpenRouter | **Anthropic** → Groq → Gemini → OpenRouter |
| Bankroll | Fixed $2,000 | Set via `BANKROLL` env var |

Switching modes: set `PAPER_TRADING=false` in `src/.env` and restart the server. Paper positions are untouched — live starts with a clean slate.

---

## Step 1: Polymarket Wallet Setup

Polymarket runs on the **Polygon** blockchain. You need a wallet with USDC.e and a small amount of MATIC for gas.

### 1.1 Create or Import a Wallet

**Option A: Use an existing MetaMask wallet**
1. Open MetaMask → click the three dots → Account Details → Show Private Key
2. Enter your password, copy the hex private key
3. This is your `POLYMARKET_PRIVATE_KEY` (strip the `0x` prefix if present)
4. Copy your wallet address — this is your `POLYMARKET_FUNDER_ADDRESS`

**Option B: Generate a new wallet (recommended for isolation)**
```bash
# Install eth-account if needed
pip install eth-account

python -c "
from eth_account import Account
acct = Account.create()
print(f'Address:     {acct.address}')
print(f'Private Key: {acct.key.hex()[2:]}')  # strip 0x
print()
print('SAVE THESE SECURELY. The private key controls all funds.')
"
```
Save the output. The address is your `POLYMARKET_FUNDER_ADDRESS`, the private key is your `POLYMARKET_PRIVATE_KEY`.

### 1.2 Fund the Wallet

You need two tokens on Polygon:
- **USDC.e** — your trading capital (this is what you bet with)
- **MATIC** — gas for token approvals (~$0.30 worth, one-time)

**Funding via Coinbase:**
1. Buy USDC on Coinbase
2. Go to Send → enter your wallet address from Step 1.1
3. **IMPORTANT:** Select **Polygon** network (not Ethereum mainnet — fees are 100x higher)
4. Send your desired trading capital (e.g., $100)
5. Also send ~1 MATIC ($0.50 worth) for gas

**Funding via bridge (if you have ETH mainnet USDC):**
1. Go to https://portal.polygon.technology/bridge
2. Bridge USDC from Ethereum → Polygon
3. Takes ~7-30 minutes, costs ~$2-5 in ETH gas

**Verify funding:**
- Check your balance at https://polygonscan.com/address/YOUR_ADDRESS
- You should see USDC.e and MATIC balances

### 1.3 Token Approvals (One-Time)

Before the CLOB can move your tokens, you must approve three contracts. This costs ~$0.30 total in MATIC gas.

**Contract addresses (Polygon mainnet):**
| Contract | Address |
|----------|---------|
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Neg Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |

**Approve via Python script:**
```python
"""Run this ONCE to approve all Polymarket contracts."""
import os
from web3 import Web3

# Connect to Polygon
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

# Your wallet
PRIVATE_KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER = os.environ["POLYMARKET_FUNDER_ADDRESS"]

# USDC.e contract
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Contracts that need USDC.e approval
SPENDERS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # CTF Exchange
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # Neg Risk CTF Exchange
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # Neg Risk Adapter
]

# ERC20 approve ABI (minimal)
ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
        "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]

usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)
MAX_UINT = 2**256 - 1  # Unlimited approval

for spender in SPENDERS:
    spender_cs = Web3.to_checksum_address(spender)
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(FUNDER))
    tx = usdc.functions.approve(spender_cs, MAX_UINT).build_transaction({
        "from": Web3.to_checksum_address(FUNDER),
        "nonce": nonce,
        "gas": 60000,
        "gasPrice": w3.to_wei("50", "gwei"),
        "chainId": 137,
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = "OK" if receipt["status"] == 1 else "FAILED"
    print(f"  {status} Approved {spender[:10]}... tx={tx_hash.hex()[:16]}...")

# Also approve CTF tokens (conditional tokens) for the same contracts
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # CTF token contract
ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=[
    {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
     "name":"setApprovalForAll","outputs":[],"type":"function"}
])

for spender in SPENDERS:
    spender_cs = Web3.to_checksum_address(spender)
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(FUNDER))
    tx = ctf.functions.setApprovalForAll(spender_cs, True).build_transaction({
        "from": Web3.to_checksum_address(FUNDER),
        "nonce": nonce,
        "gas": 60000,
        "gasPrice": w3.to_wei("50", "gwei"),
        "chainId": 137,
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = "OK" if receipt["status"] == 1 else "FAILED"
    print(f"  {status} CTF approved {spender[:10]}... tx={tx_hash.hex()[:16]}...")

print("\nAll approvals complete. You can now trade on Polymarket.")
```

Save this as `scripts/approve_polymarket.py` and run it after setting your env vars.

### 1.4 Verify CLOB Auth

Before going live, test that your credentials work:
```python
"""Test Polymarket CLOB authentication."""
import os
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=os.environ["POLYMARKET_PRIVATE_KEY"],
    funder=os.environ["POLYMARKET_FUNDER_ADDRESS"],
    signature_type=0,  # EOA wallet
)

# Derive API credentials (Level 2 auth)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

# Check balance
balance = client.get_balance_allowance()
print(f"Balance: {balance}")

# Check open orders
orders = client.get_orders()
print(f"Open orders: {len(orders)}")

print("\nCLOB auth working. Ready for live trading.")
```

---

## Step 2: Configure Environment

Edit `src/.env`:

```bash
# ── Trading Mode ──────────────────────────────────────────
PAPER_TRADING=false

# ── Polymarket ────────────────────────────────────────────
POLYMARKET_PRIVATE_KEY=your_hex_key_no_0x_prefix
POLYMARKET_FUNDER_ADDRESS=0xYourWalletAddress

# ── AI Advisor ────────────────────────────────────────────
# Anthropic is primary for live trading (best reasoning for real money decisions)
# Falls back to Groq/Gemini/OpenRouter when Anthropic tokens run out
ANTHROPIC_API_KEY=sk-ant-your-key-here

# ── Bankroll ──────────────────────────────────────────────
# Position sizing scales from this. Start conservative.
BANKROLL=100
```

**Position sizing at $100 bankroll:**
- Max single trade: $2.50
- Min trade: $1.00 (Polymarket floor)
- Max total exposure: $17.50
- Max concurrent positions: 20

---

## Step 3: Pre-Flight Checklist

Run these checks BEFORE setting `PAPER_TRADING=false`:

```bash
# 1. Start server in paper mode first
cd src && python -m uvicorn server:app --host 127.0.0.1 --port 8500

# 2. Check wallet health (in another terminal)
curl http://127.0.0.1:8500/api/derivatives/wallet-health

# 3. Test kill switch works
curl -X POST http://127.0.0.1:8500/api/derivatives/kill-switch/pause
curl -X POST http://127.0.0.1:8500/api/derivatives/kill-switch/resume

# 4. Verify Polymarket price feed is working
curl http://127.0.0.1:8500/api/arbitrage/scan
```

---

## Step 4: Go Live

1. Stop the server
2. Set `PAPER_TRADING=false` in `src/.env`
3. Set `BANKROLL=100` (or your chosen amount)
4. Set `ANTHROPIC_API_KEY` for best AI decisions
5. Start the server: `python -m uvicorn server:app --host 127.0.0.1 --port 8500`
6. **Monitor the first 10 trades manually** — check the dashboard at `http://127.0.0.1:8500`

Live mode uses `positions_live.json` and `trade_journal_live.json` — completely separate from paper data. You can switch back to paper mode at any time without losing live state.

---

## Step 5: Monitoring

**Daily checks:**
- `GET /api/derivatives/wallet-health` — balance and connectivity
- `GET /api/derivatives/packages` — open positions and P&L
- `GET /api/derivatives/calibration` — entry/exit quality metrics

**Emergency:**
- `POST /api/derivatives/kill-switch/pause` — stop new trades, keep positions
- `POST /api/derivatives/kill-switch` — close everything immediately

---

## Optional: Kalshi Setup (Phase 2)

1. Create account at https://kalshi.com
2. Go to Settings → API → Generate RSA Key Pair
3. Download the private key file
4. Add to `src/.env`:
   ```
   KALSHI_API_KEY=your-api-key
   KALSHI_RSA_PRIVATE_KEY=/path/to/kalshi-private-key.pem
   ```
5. Test against demo API first: set `KALSHI_DEMO=true` in env

## Optional: Coinbase Setup (Phase 3)

1. Go to https://portal.cdp.coinbase.com → API Keys
2. Create an Advanced Trade API key with trade permissions
3. Add to `src/.env`:
   ```
   COINBASE_ADV_API_KEY=organizations/xxx/apiKeys/yyy
   COINBASE_ADV_API_SECRET=-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----
   ```

---

## Staging Workflow (Paper → Live)

The system is designed as a staging/production pipeline:

1. **Test changes in paper mode** (`PAPER_TRADING=true`)
   - Uses `positions_paper.json`, `trade_journal_paper.json`
   - AI advisor: Groq → Gemini → OpenRouter (saves Anthropic tokens)
   - Bankroll: $2,000 simulated

2. **Validate with paper results** — check the journal, decision log, P&L

3. **Deploy to live** (`PAPER_TRADING=false`)
   - Uses `positions_live.json`, `trade_journal_live.json`
   - AI advisor: **Anthropic** → Groq → Gemini → OpenRouter
   - Bankroll: real capital from `BANKROLL` env var
   - All code changes (filters, scoring, exit rules) apply to both modes

Code changes (auto_trader scoring, exit_engine rules, etc.) apply to both environments because they're in the shared Python source. Only data files are separated.
