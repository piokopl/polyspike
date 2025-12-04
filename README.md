# 🎯 High Probability Bot v2

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets. The bot identifies high-probability opportunities (80-99%) and profits from small price movements as markets resolve.

## 📊 Strategy

The bot exploits a simple edge: **bet on outcomes that are already very likely**.

- Find markets where one outcome has 85-99% probability
- Buy tokens at e.g. $0.92 (92% implied probability)
- Sell at $0.97+ or wait for resolution at $1.00
- Profit: 5-8% per trade, typically within hours

**Example:**
```
Market: "Will the sun rise tomorrow?"
Buy: YES @ $0.95
Sell: YES @ $1.00 (market resolves)
Profit: 5.26%
```

## ✨ Features

### Core Trading
- **Automatic opportunity scanning** - Finds high-probability markets via Polymarket API
- **AI validation** - Claude validates each trade before execution
- **FOK orders** - Fill-or-Kill orders prevent pending/stuck orders
- **Dynamic Take-Profit** - Adjusts TP based on time to expiration
- **Trailing Stop-Loss** - Locks in profits as price rises
- **Position verification** - Checks actual holdings before selling

### Real-time Monitoring
- **WebSocket price feeds** - Instant price updates
- **Multi-source news** - Google News integration for context
- **AI emergency checks** - Monitors troubled positions

### Notifications
- **Discord webhooks** - Trade alerts, TP/SL triggers, daily summaries
- **Telegram bot** - Real-time notifications (optional)

### Safety Features
- **Demo mode** - Paper trading without real money
- **Max slots limit** - Prevents over-exposure
- **Blacklist system** - Avoids problematic markets
- **Grace period** - Prevents premature position removal

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install py-clob-client anthropic requests websocket-client python-telegram-bot
```

### 2. Configure

Edit `config_v2.json`:

```json
{
  "mode": "demo",
  "credentials": {
    "private_key": "0x...",
    "wallet_address": "0x...",
    "signature_type": 2
  },
  "trading": {
    "amount_per_trade": 10.0,
    "max_slots": 10,
    "min_probability": 0.80,
    "max_probability": 0.99,
    "take_profit": 0.99,
    "stop_loss": 0.70
  },
  "ai": {
    "enabled": true,
    "anthropic_api_key": "sk-ant-..."
  },
  "discord": {
    "enabled": true,
    "webhook_url": "https://discord.com/api/webhooks/..."
  }
}
```

### 3. Get Polymarket Credentials

1. Go to [Polymarket](https://polymarket.com) and connect wallet
2. Click profile → "Export Private Key"
3. Copy your wallet address from profile
4. Set `signature_type`:
   - `0` = EOA (direct wallet)
   - `1` = Magic/Email login
   - `2` = Browser wallet (MetaMask)

### 4. Run

```bash
# Demo mode (paper trading)
python highprob_bot_v2.py

# Live trading (edit config: "mode": "live")
python highprob_bot_v2.py
```

## ⚙️ Configuration Reference

### Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `amount_per_trade` | 10.0 | USD per trade (min $5) |
| `max_slots` | 10 | Maximum concurrent positions |
| `min_probability` | 0.80 | Minimum token price to consider |
| `max_probability` | 0.99 | Maximum token price |
| `min_hours_to_end` | 1 | Skip markets ending too soon |
| `max_hours_to_end` | 168 | Skip markets too far out |
| `take_profit` | 0.99 | TP trigger price |
| `stop_loss` | 0.70 | SL trigger price |

### Dynamic Take-Profit

Adjusts TP based on time remaining:

| Time to End | TP Target |
|-------------|-----------|
| < 6 hours | 3% profit |
| 6-24 hours | 4% profit |
| 24-72 hours | 5% profit |
| > 72 hours | 5% profit |

### Trailing Stop-Loss

```json
"trailing_sl": {
  "enabled": true,
  "activation_profit": 0.03,
  "trail_distance": 0.02
}
```

- Activates when position is +3% profitable
- Trails 2% behind highest price
- Locks in profits automatically

## 📁 File Structure

```
├── highprob_bot_v2.py     # Main bot
├── config_v2.json         # Configuration
├── bot_state.json         # Positions & state (auto-generated)
├── trades_log.csv         # Trade history
├── order_log.json         # Raw API responses (debug)
└── ai_cache.json          # AI validation cache
```

## 🔧 API Integration

### Polymarket APIs Used

| API | Purpose |
|-----|---------|
| Gamma API | Market discovery, metadata |
| CLOB API | Order placement, prices |
| Data API | Position verification |
| WebSocket | Real-time price feeds |

### Order Flow

```
1. Scan markets (Gamma API)
2. Filter high-probability opportunities
3. AI validation (Claude)
4. Place FOK buy order (CLOB API)
5. Log actual fill price
6. Monitor position (WebSocket + REST)
7. Trigger TP/SL → FOK sell order
8. Verify position exists (Data API)
9. Close and log trade
```

## 🛡️ Risk Management

### Position Verification

Before any sell order, the bot:
1. Checks if position is older than 5 minutes (grace period)
2. Queries Polymarket Data API for actual holdings
3. Only attempts sell if tokens exist on account
4. Removes "stale" positions that don't exist

### Order Types

- **FOK (Fill-or-Kill)** - Executes immediately and completely, or cancels
- No pending orders stuck in orderbook
- Bot knows immediately if trade succeeded

### Blacklist System

Markets are automatically blacklisted when:
- AI blocks the trade
- Market has issues (low liquidity, suspicious activity)
- Manual addition to `blacklisted_markets` in state

## 📈 Performance Tracking

### Metrics Logged

- Win rate (%)
- Total realized PnL
- Average trade duration
- Win/Loss streaks
- Per-market performance

### Daily Summary (Discord)

```
📊 DAILY SUMMARY
Trades: 15 | Wins: 12 | Losses: 3
Win Rate: 80%
Realized PnL: +$23.45
Best: +$5.20 (market-xyz)
Worst: -$2.10 (market-abc)
```

## 🐛 Troubleshooting

### "not enough balance / allowance"

**Cause:** Trying to sell tokens that don't exist on account

**Solutions:**
1. Position was sold manually on Polymarket website
2. Position verification will auto-remove stale positions
3. Check `bot_state.json` and remove invalid positions

### "Size lower than minimum: 5"

**Cause:** Order size too small

**Solution:** Set `amount_per_trade` to at least $6

### AI blocks everything

**Cause:** AI doesn't understand strategy

**Solution:** Clear AI cache and restart:
```bash
rm ai_cache.json
```

### WebSocket disconnects

**Cause:** Network issues

**Solution:** Bot auto-reconnects; check internet stability

## ⚠️ Disclaimer

This bot is for educational purposes. Trading on prediction markets involves risk. You may lose money. Never trade with funds you can't afford to lose.

**Not financial advice.**

## 📜 License

MIT License - see LICENSE file

## 🤝 Contributing

Pull requests welcome! Please test in demo mode before submitting.

---

**Built with:** Python, py-clob-client, Claude AI, WebSockets
