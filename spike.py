# highprob_bot.py - High Probability Quick Resolution Bot v2
# 
# Features:
# - Dynamic Take Profit (based on entry price)
# - Trailing Stop Loss (follows price up)
# - WebSocket real-time price monitoring
# - Telegram notifications
# - Enhanced statistics
#
# Usage:
# 1. Run scanner.py periodically (cron every hour)
# 2. Run this bot continuously: python3 highprob_bot.py

import os
import sys
import json
import time
import csv
import threading
import requests
import websocket
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any, Callable
from queue import Queue
import traceback

# Try imports
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, ApiCreds, OrderType, MarketOrderArgs
    from py_clob_client.constants import POLYGON
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    print("[WARN] py-clob-client not installed. Install with: pip install py-clob-client")

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from dotenv import load_dotenv
load_dotenv()

# ==================== CONFIGURATION ====================
CONFIG_FILE = "config_v2.json"
DEFAULT_CONFIG = {
    "mode": "demo",  # "demo" or "live"
    
    "scanner": {
        "min_price": 0.90,
        "max_price": 0.99,
        "max_opportunities": 50,
        "min_volume_24h": 1000,
        "min_liquidity": 1000,
        "scan_interval_minutes": 60
    },
    
    "trading": {
        "max_slots": 5,
        "trade_amount_usd": 2.0,
        "stop_loss": 0.70,
        "take_profit": 0.98,
        "check_interval_seconds": 5,
        "max_slippage": 0.02
    },
    
    "dynamic_tp": {
        "enabled": True,
        "min_profit_pct": 0.03,  # Minimum 3% profit
        "max_profit_pct": 0.08,  # Maximum 8% profit target
        "base_target": 0.98      # Default target if disabled
    },
    
    "trailing_sl": {
        "enabled": True,
        "activation_pct": 0.02,   # Activate after 2% profit
        "trail_pct": 0.03         # Trail 3% below peak
    },
    
    "websocket": {
        "enabled": True,
        "url": "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    },
    
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "notify_trades": True,
        "notify_daily_summary": True
    },
    
    "ai": {
        "enabled": False,
        "block_on_uncertain": True,
        "min_confidence": 0.7
    },
    
    "files": {
        "opportunities": "opportunities.json",
        "positions": "positions.json",
        "blacklist": "blacklist.json",
        "trades_history": "trades_history.csv",
        "prices_log": "prices_log.json",
        "stats": "stats.json"
    }
}

def load_config() -> dict:
    """Load configuration from file"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Deep merge with defaults
                def merge(base, override):
                    for key, value in base.items():
                        if key not in override:
                            override[key] = value
                        elif isinstance(value, dict) and isinstance(override.get(key), dict):
                            merge(value, override[key])
                    return override
                return merge(DEFAULT_CONFIG.copy(), config)
        except Exception as e:
            print(f"[ERROR] Failed to load config: {e}")
    return DEFAULT_CONFIG.copy()

CONFIG = load_config()

# ==================== LOGGING ====================
def log(msg: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")

# ==================== DATA CLASSES ====================
@dataclass
class Opportunity:
    """Token opportunity from scanner"""
    market_id: str
    event_id: str
    slug: str
    question: str
    outcome: str
    price: float
    end_date: str
    seconds_to_end: float
    hours_to_end: float
    upside: float
    volume24hr: float
    liquidity: float
    url: str
    token_id: str = ""
    condition_id: str = ""
    score: float = 0.0

@dataclass  
class Position:
    """An open position with trailing SL support"""
    token_id: str
    market_slug: str
    outcome: str
    side: str
    entry_price: float
    entry_time: str
    amount_usd: float
    shares: float
    
    # Stop Loss & Take Profit
    stop_loss: float
    take_profit: float
    initial_stop_loss: float = 0.0  # Original SL
    
    # Trailing SL
    trailing_sl_enabled: bool = False
    trailing_sl_activated: bool = False
    peak_price: float = 0.0  # Highest price seen
    
    # Monitoring
    current_price: float = 0.0
    last_update: str = ""
    
    # Target
    end_date: str = ""
    question: str = ""
    url: str = ""
    
    # Status
    status: str = "OPEN"
    exit_price: float = 0.0
    exit_time: str = ""
    close_reason: str = ""
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0

# ==================== TELEGRAM NOTIFIER ====================
class TelegramNotifier:
    """Send notifications via Telegram"""
    
    def __init__(self, config: dict):
        self.enabled = config.get("telegram", {}).get("enabled", False)
        self.bot_token = config.get("telegram", {}).get("bot_token", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = config.get("telegram", {}).get("chat_id", "") or os.getenv("TELEGRAM_CHAT_ID", "")
        self.notify_trades = config.get("telegram", {}).get("notify_trades", True)
        self.notify_daily = config.get("telegram", {}).get("notify_daily_summary", True)
        
        if self.enabled and (not self.bot_token or not self.chat_id):
            log("Telegram enabled but missing bot_token or chat_id", "WARN")
            self.enabled = False
        
        if self.enabled:
            log("Telegram notifications enabled")
    
    def send(self, message: str, parse_mode: str = "HTML"):
        """Send message to Telegram"""
        if not self.enabled:
            return
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            requests.post(url, data=data, timeout=10)
        except Exception as e:
            log(f"Telegram send error: {e}", "ERROR")
    
    def notify_trade_opened(self, pos: Position):
        """Notify about new trade"""
        if not self.notify_trades:
            return
        
        msg = f"""🎯 <b>Trade Opened</b>

<b>Market:</b> {pos.market_slug}
<b>Side:</b> {pos.outcome}
<b>Entry:</b> ${pos.entry_price:.4f}
<b>Amount:</b> ${pos.amount_usd:.2f}
<b>Shares:</b> {pos.shares:.2f}

<b>Stop Loss:</b> {pos.stop_loss:.3f}
<b>Take Profit:</b> {pos.take_profit:.3f}
<b>Trailing SL:</b> {"ON" if pos.trailing_sl_enabled else "OFF"}"""
        
        self.send(msg)
    
    def notify_trade_closed(self, pos: Position):
        """Notify about closed trade"""
        if not self.notify_trades:
            return
        
        emoji = "✅" if pos.pnl_usd > 0 else "❌"
        result = "WIN" if pos.pnl_usd > 0 else "LOSS"
        
        msg = f"""{emoji} <b>Trade Closed - {result}</b>

<b>Market:</b> {pos.market_slug}
<b>Side:</b> {pos.outcome}
<b>Reason:</b> {pos.close_reason}

<b>Entry:</b> ${pos.entry_price:.4f}
<b>Exit:</b> ${pos.exit_price:.4f}

<b>PnL:</b> {pos.pnl_pct*100:+.1f}% (${pos.pnl_usd:+.2f})"""
        
        self.send(msg)
    
    def notify_trailing_sl_activated(self, pos: Position):
        """Notify when trailing SL activates"""
        if not self.notify_trades:
            return
        
        msg = f"""📈 <b>Trailing SL Activated</b>

<b>Market:</b> {pos.market_slug}
<b>Current Price:</b> ${pos.current_price:.4f}
<b>New Stop Loss:</b> {pos.stop_loss:.3f}
<b>Peak Price:</b> ${pos.peak_price:.4f}"""
        
        self.send(msg)
    
    def notify_daily_summary(self, stats: dict):
        """Send daily summary"""
        if not self.notify_daily:
            return
        
        msg = f"""📊 <b>Daily Summary</b>

<b>Total Trades:</b> {stats.get('total_trades', 0)}
<b>Wins:</b> {stats.get('wins', 0)} | <b>Losses:</b> {stats.get('losses', 0)}
<b>Win Rate:</b> {stats.get('win_rate', 0):.0f}%

<b>Today's PnL:</b> ${stats.get('daily_pnl', 0):+.2f}
<b>Total PnL:</b> ${stats.get('total_pnl', 0):+.2f}

<b>Open Positions:</b> {stats.get('open_positions', 0)}
<b>Open PnL:</b> ${stats.get('open_pnl', 0):+.2f}"""
        
        self.send(msg)

# ==================== DISCORD NOTIFIER ====================
class DiscordNotifier:
    """Send notifications to Discord via webhook"""
    
    def __init__(self, config: dict):
        discord_config = config.get("discord", {})
        self.enabled = discord_config.get("enabled", False)
        self.webhook_url = discord_config.get("webhook_url", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.notify_trades = discord_config.get("notify_trades", True)
        self.notify_daily = discord_config.get("notify_daily_summary", True)
        
        if self.enabled and not self.webhook_url:
            log("Discord enabled but no webhook_url set - disabling", "WARN")
            self.enabled = False
        
        if self.enabled:
            log("Discord notifications enabled")
    
    def send(self, content: str = None, embed: dict = None):
        """Send message to Discord"""
        if not self.enabled or not self.webhook_url:
            return
        
        try:
            payload = {}
            if content:
                payload["content"] = content
            if embed:
                payload["embeds"] = [embed]
            
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )
            if resp.status_code not in [200, 204]:
                log(f"Discord error: {resp.status_code}", "DEBUG")
        except Exception as e:
            log(f"Discord send error: {e}", "DEBUG")
    
    def notify_trade_opened(self, pos: Position):
        """Notify about new trade"""
        if not self.notify_trades:
            return
        
        embed = {
            "title": "🟢 Trade Opened",
            "color": 0x00ff00,
            "fields": [
                {"name": "Market", "value": pos.market_slug[:50], "inline": False},
                {"name": "Side", "value": pos.outcome, "inline": True},
                {"name": "Entry", "value": f"${pos.entry_price:.4f}", "inline": True},
                {"name": "Amount", "value": f"${pos.amount_usd:.2f}", "inline": True},
                {"name": "Stop Loss", "value": f"{pos.stop_loss:.3f}", "inline": True},
                {"name": "Take Profit", "value": f"{pos.take_profit:.3f}", "inline": True},
                {"name": "Trailing SL", "value": "ON" if pos.trailing_sl_enabled else "OFF", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send(None, embed)
    
    def notify_trade_closed(self, pos: Position):
        """Notify about closed trade"""
        if not self.notify_trades:
            return
        
        is_win = pos.pnl_usd > 0
        color = 0x00ff00 if is_win else 0xff0000
        emoji = "✅" if is_win else "❌"
        result = "WIN" if is_win else "LOSS"
        
        embed = {
            "title": f"{emoji} Trade Closed - {result}",
            "color": color,
            "fields": [
                {"name": "Market", "value": pos.market_slug[:50], "inline": False},
                {"name": "Side", "value": pos.outcome, "inline": True},
                {"name": "Reason", "value": pos.close_reason, "inline": True},
                {"name": "Entry", "value": f"${pos.entry_price:.4f}", "inline": True},
                {"name": "Exit", "value": f"${pos.exit_price:.4f}", "inline": True},
                {"name": "PnL %", "value": f"{pos.pnl_pct*100:+.1f}%", "inline": True},
                {"name": "PnL $", "value": f"${pos.pnl_usd:+.2f}", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send(None, embed)
    
    def notify_trailing_sl_activated(self, pos: Position):
        """Notify when trailing SL activates"""
        if not self.notify_trades:
            return
        
        embed = {
            "title": "📈 Trailing SL Activated",
            "color": 0xffaa00,
            "fields": [
                {"name": "Market", "value": pos.market_slug[:50], "inline": False},
                {"name": "Current Price", "value": f"${pos.current_price:.4f}", "inline": True},
                {"name": "New Stop Loss", "value": f"{pos.stop_loss:.3f}", "inline": True},
                {"name": "Peak Price", "value": f"${pos.peak_price:.4f}", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send(None, embed)
    
    def notify_daily_summary(self, stats: dict):
        """Send daily summary"""
        if not self.notify_daily:
            return
        
        total_pnl = stats.get('total_pnl', 0)
        color = 0x00ff00 if total_pnl >= 0 else 0xff0000
        
        embed = {
            "title": "📊 Daily Summary",
            "color": color,
            "fields": [
                {"name": "Total Trades", "value": str(stats.get('total_trades', 0)), "inline": True},
                {"name": "Wins", "value": str(stats.get('wins', 0)), "inline": True},
                {"name": "Losses", "value": str(stats.get('losses', 0)), "inline": True},
                {"name": "Win Rate", "value": f"{stats.get('win_rate', 0):.0f}%", "inline": True},
                {"name": "Today's PnL", "value": f"${stats.get('daily_pnl', 0):+.2f}", "inline": True},
                {"name": "Total PnL", "value": f"${total_pnl:+.2f}", "inline": True},
                {"name": "Open Positions", "value": str(stats.get('open_positions', 0)), "inline": True},
                {"name": "Open PnL", "value": f"${stats.get('open_pnl', 0):+.2f}", "inline": True}
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send(None, embed)
    

# ==================== DYNAMIC TAKE PROFIT ====================
class DynamicTakeProfit:
    """Calculate dynamic take profit based on entry price"""
    
    def __init__(self, config: dict):
        tp_config = config.get("dynamic_tp", {})
        self.enabled = tp_config.get("enabled", True)
        self.min_profit_pct = tp_config.get("min_profit_pct", 0.03)
        self.max_profit_pct = tp_config.get("max_profit_pct", 0.08)
        self.base_target = tp_config.get("base_target", 0.98)
        
        if self.enabled:
            log(f"Dynamic TP enabled: {self.min_profit_pct*100:.0f}%-{self.max_profit_pct*100:.0f}%")
    
    def calculate(self, entry_price: float) -> float:
        """
        Calculate take profit price based on entry.
        
        Logic:
        - Lower entry → higher profit target (more room to grow)
        - Higher entry → lower profit target (closer to 1.0)
        
        Example:
        - Entry 0.90 → TP 0.97 (7.8% profit)
        - Entry 0.95 → TP 0.98 (3.2% profit)
        """
        if not self.enabled:
            return self.base_target
        
        # Calculate profit range based on distance to 1.0
        room_to_grow = 1.0 - entry_price  # e.g., 0.10 for entry 0.90
        
        # Scale profit target: more room = target higher profit %
        # entry 0.90 (room 0.10) → want ~7% profit → TP 0.97
        # entry 0.95 (room 0.05) → want ~3% profit → TP 0.98
        
        # Linear interpolation based on entry price (0.90 to 0.99 range)
        # entry_normalized: 0.90 → 0.0, 0.99 → 1.0
        entry_normalized = (entry_price - 0.90) / 0.09
        entry_normalized = max(0, min(1, entry_normalized))
        
        # Higher entry → lower profit target
        target_profit = self.max_profit_pct - (entry_normalized * (self.max_profit_pct - self.min_profit_pct))
        
        take_profit = entry_price * (1 + target_profit)
        
        # Cap at 0.99 (leave room for fees/slippage before resolution)
        take_profit = min(take_profit, 0.99)
        
        return round(take_profit, 4)

# ==================== TRAILING STOP LOSS ====================
class TrailingStopLoss:
    """Manage trailing stop loss for positions"""
    
    def __init__(self, config: dict):
        tsl_config = config.get("trailing_sl", {})
        self.enabled = tsl_config.get("enabled", True)
        self.activation_pct = tsl_config.get("activation_pct", 0.02)  # 2% profit to activate
        self.trail_pct = tsl_config.get("trail_pct", 0.03)  # Trail 3% below peak
        
        if self.enabled:
            log(f"Trailing SL enabled: activates at +{self.activation_pct*100:.0f}%, trails {self.trail_pct*100:.0f}%")
    
    def update(self, pos: Position, current_price: float) -> tuple[bool, float]:
        """
        Update trailing stop loss for position.
        
        Returns:
        - (activated, new_stop_loss)
        """
        if not self.enabled or not pos.trailing_sl_enabled:
            return False, pos.stop_loss
        
        # Update peak price
        if current_price > pos.peak_price:
            pos.peak_price = current_price
        
        # Check if should activate trailing SL
        profit_pct = (current_price - pos.entry_price) / pos.entry_price
        
        if not pos.trailing_sl_activated and profit_pct >= self.activation_pct:
            # Activate trailing SL
            pos.trailing_sl_activated = True
            new_sl = pos.peak_price * (1 - self.trail_pct)
            
            # Only update if new SL is higher than current
            if new_sl > pos.stop_loss:
                pos.stop_loss = round(new_sl, 4)
                return True, pos.stop_loss
        
        elif pos.trailing_sl_activated:
            # Update trailing SL based on peak
            new_sl = pos.peak_price * (1 - self.trail_pct)
            
            # Only move SL up, never down
            if new_sl > pos.stop_loss:
                pos.stop_loss = round(new_sl, 4)
        
        return False, pos.stop_loss

# ==================== WEBSOCKET PRICE MONITOR ====================
class WebSocketMonitor:
    """Real-time price monitoring via WebSocket - same format as spike.py"""
    
    def __init__(self, config: dict, on_price_update: Callable):
        ws_config = config.get("websocket", {})
        self.enabled = ws_config.get("enabled", True)
        self.url = ws_config.get("url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self.on_price_update = on_price_update
        
        self.ws = None
        self.subscribed_tokens: List[str] = []  # List, not set - for subscription message
        self.running = False
        self.connected = False
        self.thread = None
        self.ping_thread = None
        self.reconnect_delay = 1
        self.max_reconnect_delay = 60
        self.prices: Dict[str, float] = {}
        self.lock = threading.Lock()
        
        if self.enabled:
            log("WebSocket monitor enabled")
    
    def start(self):
        """Start WebSocket connection in background thread"""
        if not self.enabled:
            return
        
        if not self.subscribed_tokens:
            log("No tokens to subscribe - WebSocket not started", "WARN")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
    
    def stop(self):
        """Stop WebSocket connection"""
        self.running = False
        self.connected = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
    
    def add_token(self, token_id: str):
        """Add token to subscription list (call before start())"""
        if not self.enabled or not token_id:
            return
        
        if token_id not in self.subscribed_tokens:
            self.subscribed_tokens.append(token_id)
    
    def subscribe(self, token_id: str):
        """Subscribe to price updates for token (alias for add_token)"""
        self.add_token(token_id)
    
    def unsubscribe(self, token_id: str):
        """Unsubscribe from token"""
        if token_id in self.subscribed_tokens:
            self.subscribed_tokens.remove(token_id)
    
    def get_price(self, token_id: str) -> float:
        """Get cached price for token"""
        with self.lock:
            return self.prices.get(token_id, 0)
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self.connected
    
    def _run(self):
        """Main WebSocket loop with auto-reconnect"""
        while self.running:
            try:
                self._connect()
            except Exception as e:
                log(f"WebSocket error: {e}", "ERROR")
            
            if self.running:
                log(f"WebSocket reconnecting in {self.reconnect_delay}s...")
                time.sleep(self.reconnect_delay)
                # Exponential backoff
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
    
    def _start_ping(self):
        """Start ping thread - sends PING every 10 seconds to keep connection alive"""
        def ping_loop():
            while self.running and self.connected:
                try:
                    if self.ws:
                        # Send "PING" as string (not JSON!) - per Polymarket docs
                        self.ws.send("PING")
                except Exception as e:
                    log(f"Ping error: {e}", "DEBUG")
                    break
                time.sleep(10)
        
        self.ping_thread = threading.Thread(target=ping_loop, daemon=True)
        self.ping_thread.start()
    
    def _send_subscription(self):
        """Send subscription in Polymarket format"""
        if not self.ws or not self.subscribed_tokens:
            log("No tokens to subscribe", "WARN")
            return
        
        try:
            # Format per official Polymarket documentation:
            # {"assets_ids": [...], "type": "market"}
            subscribe_msg = {
                "assets_ids": self.subscribed_tokens,
                "type": "market"  # lowercase!
            }
            msg_str = json.dumps(subscribe_msg)
            self.ws.send(msg_str)
            log(f"Subscription sent: {len(self.subscribed_tokens)} tokens")
        except Exception as e:
            log(f"Subscription error: {e}", "ERROR")
    
    def _connect(self):
        """Connect to WebSocket"""
        def on_message(ws, message):
            try:
                # Ignore PONG responses
                if message == "PONG" or message == "pong":
                    return
                
                data = json.loads(message)
                
                # Ignore empty responses
                if not data or data == []:
                    return
                
                # Handle message list
                if isinstance(data, list):
                    for item in data:
                        self._handle_message(item)
                else:
                    self._handle_message(data)
                    
            except json.JSONDecodeError:
                pass  # Ignore non-JSON messages
            except Exception as e:
                log(f"WebSocket message error: {e}", "DEBUG")
        
        def on_error(ws, error):
            log(f"WebSocket error: {error}", "DEBUG")
        
        def on_close(ws, close_status, close_msg):
            self.connected = False
            log(f"WebSocket closed: {close_status}", "DEBUG")
        
        def on_open(ws):
            self.connected = True
            self.reconnect_delay = 1  # Reset backoff on successful connect
            log("WebSocket connected")
            
            # Send subscription
            self._send_subscription()
            
            # Start ping thread
            self._start_ping()
        
        log(f"Connecting to WebSocket: {self.url}")
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open
        )
        
        # Run in separate thread
        ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        ws_thread.start()
        ws_thread.join()  # Wait for thread to finish (on disconnect)
    
    def _handle_message(self, msg: dict):
        """Handle incoming WebSocket message - Polymarket format"""
        if not isinstance(msg, dict):
            return
        
        event_type = msg.get("event_type", "")
        
        if event_type == "book":
            # Full orderbook - get best bid/ask
            asset_id = msg.get("asset_id")
            bids = msg.get("bids", [])
            asks = msg.get("asks", [])
            
            if bids and asks:
                best_bid = float(bids[0]["price"]) if bids else 0
                best_ask = float(asks[0]["price"]) if asks else 1
                mid_price = (best_bid + best_ask) / 2
                
                # Ignore default 0.5 price from empty orderbooks
                if abs(mid_price - 0.5) < 0.01:
                    return
                
                if asset_id and mid_price > 0:
                    with self.lock:
                        old_price = self.prices.get(asset_id, 0)
                        if abs(mid_price - old_price) < 0.0001:
                            return  # No change
                        self.prices[asset_id] = mid_price
                    
                    # Notify callback
                    if self.on_price_update and asset_id in self.subscribed_tokens:
                        self.on_price_update(asset_id, mid_price, old_price)
        
        elif event_type == "price_change":
            # Price change - most reliable source
            for change in msg.get("price_changes", [msg]):
                asset_id = change.get("asset_id")
                best_bid = change.get("best_bid")
                best_ask = change.get("best_ask")
                
                if best_bid and best_ask:
                    mid_price = (float(best_bid) + float(best_ask)) / 2
                    
                    # Ignore default 0.5 price
                    if abs(mid_price - 0.5) < 0.01:
                        continue
                    
                    if asset_id and mid_price > 0:
                        with self.lock:
                            old_price = self.prices.get(asset_id, 0)
                            if abs(mid_price - old_price) < 0.0001:
                                continue  # No change
                            self.prices[asset_id] = mid_price
                        
                        # Notify callback
                        if self.on_price_update and asset_id in self.subscribed_tokens:
                            self.on_price_update(asset_id, mid_price, old_price)
        
        elif event_type == "last_trade_price":
            # Last trade price
            asset_id = msg.get("asset_id")
            price = msg.get("price")
            
            if asset_id and price:
                price = float(price)
                
                # Ignore default 0.5 price
                if abs(price - 0.5) < 0.01:
                    return
                
                with self.lock:
                    old_price = self.prices.get(asset_id, 0)
                    if abs(price - old_price) < 0.0001:
                        return  # No change
                    self.prices[asset_id] = price
                
                # Notify callback
                if self.on_price_update and asset_id in self.subscribed_tokens:
                    self.on_price_update(asset_id, price, old_price)

# ==================== STATISTICS TRACKER ====================
class StatsTracker:
    """Track and persist trading statistics"""
    
    def __init__(self, config: dict):
        self.stats_file = config.get("files", {}).get("stats", "stats.json")
        self.stats = self._load()
    
    def _load(self) -> dict:
        """Load stats from file"""
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, "r") as f:
                    return json.load(f)
            except:
                pass
        
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "total_volume": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "avg_hold_time_hours": 0.0,
            "win_streak": 0,
            "loss_streak": 0,
            "max_win_streak": 0,
            "max_loss_streak": 0,
            "daily_pnl": {},  # date -> pnl
            "hourly_trades": {},  # hour -> count
            "by_reason": {},  # close_reason -> {count, pnl}
            "by_outcome": {},  # Yes/No -> {count, wins, pnl}
        }
    
    def save(self):
        """Save stats to file"""
        try:
            with open(self.stats_file, "w") as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            log(f"Failed to save stats: {e}", "ERROR")
    
    def record_trade(self, pos: Position):
        """Record completed trade"""
        self.stats["total_trades"] += 1
        self.stats["total_pnl"] += pos.pnl_usd
        self.stats["total_volume"] += pos.amount_usd
        
        # Win/Loss tracking
        is_win = pos.pnl_usd > 0
        if is_win:
            self.stats["wins"] += 1
            self.stats["win_streak"] += 1
            self.stats["loss_streak"] = 0
            self.stats["max_win_streak"] = max(self.stats["max_win_streak"], self.stats["win_streak"])
        else:
            self.stats["losses"] += 1
            self.stats["loss_streak"] += 1
            self.stats["win_streak"] = 0
            self.stats["max_loss_streak"] = max(self.stats["max_loss_streak"], self.stats["loss_streak"])
        
        # Best/worst trade
        self.stats["best_trade"] = max(self.stats["best_trade"], pos.pnl_usd)
        self.stats["worst_trade"] = min(self.stats["worst_trade"], pos.pnl_usd)
        
        # Hold time
        try:
            entry = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
            exit_t = datetime.fromisoformat(pos.exit_time.replace("Z", "+00:00"))
            hold_hours = (exit_t - entry).total_seconds() / 3600
            
            n = self.stats["total_trades"]
            old_avg = self.stats["avg_hold_time_hours"]
            self.stats["avg_hold_time_hours"] = ((old_avg * (n-1)) + hold_hours) / n
        except:
            pass
        
        # Daily PnL
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self.stats["daily_pnl"]:
            self.stats["daily_pnl"][today] = 0.0
        self.stats["daily_pnl"][today] += pos.pnl_usd
        
        # Hourly distribution
        hour = datetime.now().strftime("%H")
        if hour not in self.stats["hourly_trades"]:
            self.stats["hourly_trades"][hour] = 0
        self.stats["hourly_trades"][hour] += 1
        
        # By close reason
        reason = pos.close_reason
        if reason not in self.stats["by_reason"]:
            self.stats["by_reason"][reason] = {"count": 0, "pnl": 0.0, "wins": 0}
        self.stats["by_reason"][reason]["count"] += 1
        self.stats["by_reason"][reason]["pnl"] += pos.pnl_usd
        if is_win:
            self.stats["by_reason"][reason]["wins"] += 1
        
        # By outcome (Yes/No)
        outcome = pos.outcome
        if outcome not in self.stats["by_outcome"]:
            self.stats["by_outcome"][outcome] = {"count": 0, "wins": 0, "pnl": 0.0}
        self.stats["by_outcome"][outcome]["count"] += 1
        self.stats["by_outcome"][outcome]["pnl"] += pos.pnl_usd
        if is_win:
            self.stats["by_outcome"][outcome]["wins"] += 1
        
        self.save()
    
    def get_summary(self) -> dict:
        """Get stats summary"""
        win_rate = (self.stats["wins"] / self.stats["total_trades"] * 100) if self.stats["total_trades"] > 0 else 0
        
        today = datetime.now().strftime("%Y-%m-%d")
        daily_pnl = self.stats["daily_pnl"].get(today, 0.0)
        
        return {
            "total_trades": self.stats["total_trades"],
            "wins": self.stats["wins"],
            "losses": self.stats["losses"],
            "win_rate": win_rate,
            "total_pnl": self.stats["total_pnl"],
            "daily_pnl": daily_pnl,
            "best_trade": self.stats["best_trade"],
            "worst_trade": self.stats["worst_trade"],
            "avg_hold_time": self.stats["avg_hold_time_hours"],
            "win_streak": self.stats["win_streak"],
            "loss_streak": self.stats["loss_streak"],
            "max_win_streak": self.stats["max_win_streak"],
            "max_loss_streak": self.stats["max_loss_streak"],
        }

# ==================== FILE HANDLERS ====================
class FileManager:
    """Manages all file operations"""
    
    def __init__(self, config: dict):
        self.files = config.get("files", DEFAULT_CONFIG["files"])
    
    def load_opportunities(self) -> List[Opportunity]:
        """Load opportunities from scanner output"""
        filepath = self.files["opportunities"]
        if not os.path.exists(filepath):
            log(f"Opportunities file not found: {filepath}", "WARN")
            return []
        
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            
            opportunities = []
            for item in data:
                opp = Opportunity(
                    market_id=item.get("market_id", ""),
                    event_id=item.get("event_id", ""),
                    slug=item.get("slug", ""),
                    question=item.get("question", "")[:100],
                    outcome=item.get("outcome", ""),
                    price=float(item.get("price", 0)),
                    end_date=item.get("end_date", ""),
                    seconds_to_end=float(item.get("seconds_to_end", 0)),
                    hours_to_end=float(item.get("hours_to_end", 0)),
                    upside=float(item.get("upside", 0)),
                    volume24hr=float(item.get("volume24hr", 0)),
                    liquidity=float(item.get("liquidity", 0)),
                    url=item.get("url", ""),
                    token_id=item.get("token_id", ""),
                    condition_id=item.get("condition_id", ""),
                    score=float(item.get("score", 0))
                )
                opportunities.append(opp)
            
            return opportunities
        except Exception as e:
            log(f"Failed to load opportunities: {e}", "ERROR")
            return []
    
    def save_opportunities(self, opportunities: List[Opportunity]):
        """Save opportunities to file"""
        filepath = self.files["opportunities"]
        try:
            data = [asdict(opp) for opp in opportunities]
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log(f"Failed to save opportunities: {e}", "ERROR")
    
    def load_positions(self) -> Dict[str, Position]:
        """Load open positions"""
        filepath = self.files["positions"]
        if not os.path.exists(filepath):
            return {}
        
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            
            positions = {}
            for token_id, item in data.items():
                pos = Position(**item)
                positions[token_id] = pos
            
            return positions
        except Exception as e:
            log(f"Failed to load positions: {e}", "ERROR")
            return {}
    
    def save_positions(self, positions: Dict[str, Position]):
        """Save open positions"""
        filepath = self.files["positions"]
        try:
            data = {tid: asdict(pos) for tid, pos in positions.items()}
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log(f"Failed to save positions: {e}", "ERROR")
    
    def load_blacklist(self) -> set:
        """Load blacklisted tokens"""
        filepath = self.files["blacklist"]
        if not os.path.exists(filepath):
            return set()
        
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            return set(data)
        except Exception as e:
            log(f"Failed to load blacklist: {e}", "ERROR")
            return set()
    
    def save_blacklist(self, blacklist: set):
        """Save blacklist"""
        filepath = self.files["blacklist"]
        try:
            with open(filepath, "w") as f:
                json.dump(list(blacklist), f, indent=2)
        except Exception as e:
            log(f"Failed to save blacklist: {e}", "ERROR")
    
    def add_to_blacklist(self, token_key: str):
        """Add token to blacklist"""
        blacklist = self.load_blacklist()
        blacklist.add(token_key)
        self.save_blacklist(blacklist)
    
    def log_trade(self, position: Position):
        """Log completed trade to CSV"""
        filepath = self.files["trades_history"]
        
        try:
            entry = datetime.fromisoformat(position.entry_time.replace("Z", "+00:00"))
            exit_t = datetime.fromisoformat(position.exit_time.replace("Z", "+00:00"))
            duration_hours = (exit_t - entry).total_seconds() / 3600
        except:
            duration_hours = 0
        
        result = "WIN" if position.pnl_usd > 0 else "LOSS"
        
        row = {
            "timestamp": position.exit_time,
            "token_id": position.token_id[:20] + "...",
            "market_slug": position.market_slug,
            "outcome": position.outcome,
            "side": position.side,
            "entry_price": f"{position.entry_price:.4f}",
            "exit_price": f"{position.exit_price:.4f}",
            "amount_usd": f"{position.amount_usd:.2f}",
            "pnl_usd": f"{position.pnl_usd:.2f}",
            "pnl_pct": f"{position.pnl_pct:.1%}",
            "result": result,
            "close_reason": position.close_reason,
            "duration_hours": f"{duration_hours:.1f}"
        }
        
        file_exists = os.path.exists(filepath)
        
        try:
            with open(filepath, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as e:
            log(f"Failed to log trade: {e}", "ERROR")
    
    def update_prices_log(self, positions: Dict[str, Position]):
        """Update prices log for dashboard"""
        filepath = self.files["prices_log"]
        
        data = {
            "updated": datetime.now().isoformat(),
            "positions": {}
        }
        
        for token_id, pos in positions.items():
            data["positions"][pos.market_slug] = {
                "outcome": pos.outcome,
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "trailing_activated": pos.trailing_sl_activated,
                "peak_price": pos.peak_price,
                "pnl_pct": pos.pnl_pct,
                "end_date": pos.end_date,
                "last_update": pos.last_update
            }
        
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log(f"Failed to update prices log: {e}", "ERROR")

# ==================== POLYMARKET CLIENT ====================
class PolymarketClient:
    """Handles all Polymarket API interactions - DEMO and LIVE modes"""
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    CHAIN_ID = 137  # Polygon Mainnet
    
    def __init__(self, config: dict):
        self.config = config
        self.demo_mode = config.get("mode", "demo").lower() == "demo"
        self.client = None
        
        # Trading settings
        trading_config = config.get("trading", {})
        self.slippage = trading_config.get("max_slippage", 0.02)
        
        # Credentials from config or env
        creds = config.get("credentials", {})
        self.private_key = creds.get("private_key", "") or os.getenv("POLY_PRIVATE_KEY", "")
        self.wallet_address = creds.get("wallet_address", "") or os.getenv("POLY_WALLET_ADDRESS", "")
        self.signature_type = creds.get("signature_type", 0)  # 0=EOA, 2=Browser proxy
        
        if not self.demo_mode:
            self._init_clob_client()
    
    def _init_clob_client(self):
        """Initialize CLOB client for live trading"""
        if not CLOB_AVAILABLE:
            log("py-clob-client not installed! Run: pip install py-clob-client", "ERROR")
            log("Falling back to DEMO mode", "WARN")
            self.demo_mode = True
            return
        
        if not self.private_key:
            log("Private key not set - check config.credentials.private_key or POLY_PRIVATE_KEY env", "ERROR")
            log("Falling back to DEMO mode", "WARN")
            self.demo_mode = True
            return
        
        try:
            # Initialize client
            if self.wallet_address:
                # Proxy wallet mode (browser extension, Magic, etc.)
                sig_type = self.signature_type if self.signature_type > 0 else 2
                log(f"Using proxy wallet: {self.wallet_address[:10]}...{self.wallet_address[-6:]}")
                self.client = ClobClient(
                    self.CLOB_API,
                    key=self.private_key,
                    chain_id=self.CHAIN_ID,
                    signature_type=sig_type,
                    funder=self.wallet_address
                )
            else:
                # Standard EOA wallet
                self.client = ClobClient(
                    self.CLOB_API,
                    key=self.private_key,
                    chain_id=self.CHAIN_ID
                )
            
            # Set API credentials
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            log("✅ CLOB client initialized - LIVE TRADING ENABLED", "WARN")
            
        except Exception as e:
            log(f"Failed to init CLOB client: {e}", "ERROR")
            log("Falling back to DEMO mode", "WARN")
            self.demo_mode = True
    
    def get_token_price(self, token_id: str, side: str = "buy") -> float:
        """Get current token price from orderbook
        
        Args:
            token_id: Token ID
            side: "buy" = price to buy (ASK), "sell" = price to sell (BID)
        """
        try:
            resp = requests.get(
                f"{self.CLOB_API}/price",
                params={"token_id": token_id, "side": side},
                timeout=5
            )
            if resp.status_code == 200:
                return float(resp.json().get("price", 0))
        except Exception as e:
            log(f"Price fetch error: {e}", "DEBUG")
        return 0
    
    def get_sell_price(self, token_id: str) -> float:
        """Get price at which we can SELL (BID price)"""
        return self.get_token_price(token_id, side="sell")
    
    def get_buy_price(self, token_id: str) -> float:
        """Get price at which we can BUY (ASK price)"""
        return self.get_token_price(token_id, side="buy")
    
    def get_orderbook(self, token_id: str) -> dict:
        """Get full orderbook for token"""
        try:
            resp = requests.get(
                f"{self.CLOB_API}/book",
                params={"token_id": token_id},
                timeout=5
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"Orderbook fetch error: {e}", "DEBUG")
        return {}
    
    def get_my_positions(self) -> Optional[Dict[str, float]]:
        """
        Get current open positions from Polymarket Data API.
        Returns dict: {token_id: shares} or None if error
        """
        if self.demo_mode:
            return {}
        
        try:
            # Get wallet address from config
            wallet_address = self.wallet_address
            if not wallet_address:
                log("No wallet address for position check", "DEBUG")
                return None
            
            # Call Data API endpoint
            url = f"https://data-api.polymarket.com/positions?user={wallet_address}&sizeThreshold=0"
            resp = requests.get(url, timeout=10)
            
            if resp.status_code != 200:
                log(f"Position API error: {resp.status_code}", "DEBUG")
                return None
            
            positions = resp.json()
            
            # Build dict of token_id -> shares
            result = {}
            for pos in positions:
                asset = pos.get("asset")
                size = float(pos.get("size", 0))
                if asset and size > 0:
                    result[asset] = size
            
            log(f"[API] Found {len(result)} positions on Polymarket", "DEBUG")
            return result
            
        except Exception as e:
            log(f"Position check error: {e}", "DEBUG")
            return None
    
    def has_position(self, token_id: str, min_shares: float = 0.1) -> bool:
        """Check if we actually hold this token on Polymarket"""
        positions = self.get_my_positions()
        if positions is None:
            # API error - assume we have it to be safe
            return True
        return positions.get(token_id, 0) >= min_shares
    
    def _log_order_to_file(self, order_type: str, token_id: str, amount: float, price: float, response: dict):
        """Log order details to file for debugging"""
        try:
            import json
            from datetime import datetime
            
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "order_type": order_type,
                "token_id": token_id[:30] + "..." if len(token_id) > 30 else token_id,
                "amount": amount,
                "price": price,
                "response": response
            }
            
            with open("order_log.json", "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            log(f"Failed to log order to file: {e}", "DEBUG")
    
    def get_balance(self) -> float:
        """Get USDC balance"""
        if self.demo_mode or not self.client:
            return 0
        
        try:
            # Get balance from API
            balance_info = self.client.get_balance()
            if balance_info:
                return float(balance_info.get("balance", 0))
        except Exception as e:
            log(f"Balance check error: {e}", "DEBUG")
        return 0
    
    def place_market_order(self, token_id: str, amount_usd: float, price: float) -> Optional[dict]:
        """
        Place market buy order.
        
        In DEMO mode: Simulates order
        In LIVE mode: Places real order with slippage protection
        """
        if price <= 0:
            log(f"Invalid price {price}", "ERROR")
            return None
        
        shares = amount_usd / price
        
        # === DEMO MODE ===
        if self.demo_mode:
            log(f"[DEMO] BUY {shares:.2f} shares @ ${price:.4f} = ${amount_usd:.2f}")
            return {
                "order_id": f"DEMO_{int(time.time())}",
                "filled_price": price,
                "filled_shares": shares,
                "demo": True
            }
        
        # === LIVE MODE ===
        if not self.client:
            log("CLOB client not available", "ERROR")
            return None
        
        try:
            # For FOK, use MarketOrderArgs with amount in USD
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(amount_usd, 2),  # Amount in USD
                side="BUY",
            )
            
            signed = self.client.create_market_order(order_args)
            response = self.client.post_order(signed, OrderType.FOK)
            
            # Log full response for debugging
            log(f"[DEBUG] BUY post_order response: {response}", "DEBUG")
            self._log_order_to_file("BUY", token_id, amount_usd, price, response)
            
            # Check if order was filled
            status = response.get("status", "")
            error_msg = response.get("errorMsg", "")
            if status == "REJECTED" or error_msg:
                log(f"[LIVE] BUY FOK rejected: {error_msg or status}", "WARN")
                return None
            
            order_id = response.get("orderID", response.get("order_id", "unknown"))
            
            # Get actual fill details from order
            actual_price = price
            actual_shares = shares
            try:
                order_details = self.client.get_order(order_id)
                log(f"[DEBUG] BUY get_order response: {order_details}", "DEBUG")
                self._log_order_to_file("BUY_DETAILS", token_id, amount_usd, price, order_details)
                if order_details:
                    # Try to get average price and size filled
                    avg_price = order_details.get("price") or order_details.get("avg_price") or order_details.get("average_price")
                    size_filled = order_details.get("size_matched") or order_details.get("filled") or order_details.get("original_size")
                    if avg_price:
                        actual_price = float(avg_price)
                    if size_filled:
                        actual_shares = float(size_filled)
                    log(f"[LIVE] BUY FOK filled: {order_id} | {actual_shares:.2f} shares @ ${actual_price:.4f}")
                else:
                    # Fallback: calculate from amount
                    actual_price = self.get_buy_price(token_id) or price
                    actual_shares = amount_usd / actual_price if actual_price > 0 else shares
                    log(f"[LIVE] BUY FOK filled: {order_id} | ~{actual_shares:.2f} shares @ ~${actual_price:.4f} (estimated)")
            except Exception as e:
                # Fallback: calculate from amount
                actual_price = self.get_buy_price(token_id) or price
                actual_shares = amount_usd / actual_price if actual_price > 0 else shares
                log(f"[LIVE] BUY FOK filled: {order_id} | ~{actual_shares:.2f} shares @ ~${actual_price:.4f} (error: {e})")
            
            return {
                "order_id": order_id,
                "filled_price": actual_price,
                "filled_shares": actual_shares,
                "limit_price": actual_price,
                "demo": False
            }
            
        except Exception as e:
            log(f"BUY order error: {e}", "ERROR")
            return None
    
    def place_sell_order(self, token_id: str, shares: float, price: float) -> Optional[dict]:
        """
        Place sell order.
        
        In DEMO mode: Simulates order
        In LIVE mode: Places real order with slippage protection
        """
        if price <= 0 or shares <= 0:
            log(f"Invalid sell params: price={price}, shares={shares}", "ERROR")
            return None
        
        # === DEMO MODE ===
        if self.demo_mode:
            log(f"[DEMO] SELL {shares:.2f} shares @ ${price:.4f}")
            return {
                "order_id": f"DEMO_SELL_{int(time.time())}",
                "filled_price": price,
                "filled_shares": shares,
                "demo": True
            }
        
        # === LIVE MODE ===
        if not self.client:
            log("CLOB client not available", "ERROR")
            return None
        
        try:
            # For FOK SELL, use MarketOrderArgs with amount in shares
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(shares, 2),  # Amount in shares for SELL
                side="SELL",
            )
            
            signed = self.client.create_market_order(order_args)
            response = self.client.post_order(signed, OrderType.FOK)
            
            # Log full response for debugging
            log(f"[DEBUG] SELL post_order response: {response}", "DEBUG")
            self._log_order_to_file("SELL", token_id, shares, price, response)
            
            # Check if order was filled
            status = response.get("status", "")
            error_msg = response.get("errorMsg", "")
            if status == "REJECTED" or error_msg:
                log(f"[LIVE] SELL FOK rejected: {error_msg or status}", "WARN")
                return None
            
            order_id = response.get("orderID", response.get("order_id", "unknown"))
            
            # Get actual fill details from order
            actual_price = price
            actual_shares = shares
            try:
                order_details = self.client.get_order(order_id)
                log(f"[DEBUG] SELL get_order response: {order_details}", "DEBUG")
                self._log_order_to_file("SELL_DETAILS", token_id, shares, price, order_details)
                if order_details:
                    avg_price = order_details.get("price") or order_details.get("avg_price") or order_details.get("average_price")
                    size_filled = order_details.get("size_matched") or order_details.get("filled") or order_details.get("original_size")
                    if avg_price:
                        actual_price = float(avg_price)
                    if size_filled:
                        actual_shares = float(size_filled)
                    log(f"[LIVE] SELL FOK filled: {order_id} | {actual_shares:.2f} shares @ ${actual_price:.4f}")
                else:
                    actual_price = self.get_sell_price(token_id) or price
                    log(f"[LIVE] SELL FOK filled: {order_id} | ~{shares:.2f} shares @ ~${actual_price:.4f} (estimated)")
            except Exception as e:
                actual_price = self.get_sell_price(token_id) or price
                log(f"[LIVE] SELL FOK filled: {order_id} | ~{shares:.2f} shares @ ~${actual_price:.4f} (error: {e})")
            
            return {
                "order_id": order_id,
                "filled_price": actual_price,
                "filled_shares": actual_shares,
                "limit_price": actual_price,
                "demo": False
            }
            
        except Exception as e:
            log(f"SELL order error: {e}", "ERROR")
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order"""
        if self.demo_mode or not self.client:
            return True
        
        try:
            self.client.cancel(order_id)
            log(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            log(f"Cancel order error: {e}", "ERROR")
            return False
    
    def get_open_orders(self) -> List[dict]:
        """Get all open orders"""
        if self.demo_mode or not self.client:
            return []
        
        try:
            orders = self.client.get_orders()
            return [o for o in orders if o.get("status") == "live"] if orders else []
        except Exception as e:
            log(f"Get orders error: {e}", "DEBUG")
            return []
    
    def check_resolution(self, market_slug: str) -> Optional[str]:
        """Check if market is resolved, return winning outcome or None"""
        try:
            resp = requests.get(
                f"{self.GAMMA_API}/markets",
                params={"slug": market_slug},
                timeout=10
            )
            if resp.status_code == 200:
                markets = resp.json()
                if markets:
                    market = markets[0]
                    if market.get("resolved", False):
                        outcome_prices = market.get("outcomePrices", [])
                        outcomes = market.get("outcomes", ["Yes", "No"])
                        
                        if outcome_prices:
                            for i, price in enumerate(outcome_prices):
                                if float(price) >= 0.99:
                                    return outcomes[i] if i < len(outcomes) else None
        except Exception as e:
            log(f"Resolution check error: {e}", "DEBUG")
        return None

# ==================== NEWS FETCHER ====================
class NewsFetcher:
    """Fetch relevant news for market validation"""
    
    def __init__(self, config: dict):
        news_config = config.get("news", {})
        self.enabled = news_config.get("enabled", True)
        self.sources = news_config.get("sources", ["google"])
        self.newsapi_key = news_config.get("newsapi_key", "") or os.getenv("NEWSAPI_KEY", "")
        self.max_headlines = news_config.get("max_headlines", 3)
        self.cache_minutes = news_config.get("cache_minutes", 15)
        
        # Cache: {query: (timestamp, headlines)}
        self._cache: Dict[str, tuple] = {}
        
        if self.enabled:
            sources_str = ", ".join(self.sources)
            log(f"News fetcher enabled: {sources_str}")
    
    def _extract_keywords(self, question: str) -> str:
        """Extract search keywords from market question"""
        # Remove common words
        stop_words = {
            'will', 'the', 'be', 'to', 'of', 'and', 'a', 'in', 'that', 'have',
            'it', 'for', 'not', 'on', 'with', 'he', 'as', 'you', 'do', 'at',
            'this', 'but', 'his', 'by', 'from', 'they', 'we', 'say', 'her',
            'she', 'or', 'an', 'my', 'one', 'all', 'would', 'there', 'their',
            'what', 'so', 'up', 'out', 'if', 'about', 'who', 'get', 'which',
            'go', 'me', 'before', 'after', 'between', 'during', 'above', 'below',
            'yes', 'no', 'than', 'more', 'less', 'any', 'each', 'how', 'when',
            'where', 'why', 'price', 'reach', 'end', 'close', 'day', 'week'
        }
        
        # Clean and split
        import re
        words = re.findall(r'\b[A-Za-z0-9]+\b', question.lower())
        
        # Filter and keep important words
        keywords = []
        for word in words:
            if word not in stop_words and len(word) > 2:
                keywords.append(word)
        
        # Limit to first 4 keywords
        return " ".join(keywords[:4])
    
    def _fetch_google_news(self, query: str) -> list[str]:
        """Fetch from Google News RSS (free, no API key)"""
        try:
            import urllib.parse
            encoded_query = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
            
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return []
            
            # Simple XML parsing without feedparser
            import re
            titles = re.findall(r'<title>(?:<!\[CDATA\[)?([^<\]]+)(?:\]\]>)?</title>', resp.text)
            
            # Skip first title (feed title)
            headlines = []
            for title in titles[1:self.max_headlines + 1]:
                # Clean up
                title = title.strip()
                if title and len(title) > 10:
                    headlines.append(title)
            
            return headlines[:self.max_headlines]
            
        except Exception as e:
            log(f"Google News error: {e}", "DEBUG")
            return []
    
    def _fetch_newsapi(self, query: str) -> list[str]:
        """Fetch from NewsAPI.org (requires API key)"""
        if not self.newsapi_key:
            return []
        
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "sortBy": "publishedAt",
                "pageSize": self.max_headlines,
                "apiKey": self.newsapi_key
            }
            
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return []
            
            data = resp.json()
            articles = data.get("articles", [])
            
            headlines = []
            for article in articles[:self.max_headlines]:
                title = article.get("title", "")
                if title and len(title) > 10:
                    headlines.append(title)
            
            return headlines
            
        except Exception as e:
            log(f"NewsAPI error: {e}", "DEBUG")
            return []
    
    def get_headlines(self, question: str) -> list[str]:
        """Get news headlines for market question (cached)"""
        if not self.enabled:
            return []
        
        # Extract keywords
        keywords = self._extract_keywords(question)
        if not keywords:
            return []
        
        # Check cache
        cache_key = keywords.lower()
        if cache_key in self._cache:
            timestamp, headlines = self._cache[cache_key]
            age_minutes = (time.time() - timestamp) / 60
            if age_minutes < self.cache_minutes:
                return headlines
        
        # Fetch from sources
        all_headlines = []
        
        if "google" in self.sources:
            all_headlines.extend(self._fetch_google_news(keywords))
        
        if "newsapi" in self.sources and self.newsapi_key:
            all_headlines.extend(self._fetch_newsapi(keywords))
        
        # Dedupe and limit
        seen = set()
        unique = []
        for h in all_headlines:
            h_lower = h.lower()
            if h_lower not in seen:
                seen.add(h_lower)
                unique.append(h)
        
        headlines = unique[:self.max_headlines]
        
        # Cache
        self._cache[cache_key] = (time.time(), headlines)
        
        return headlines

# ==================== AI VALIDATOR ====================
class AIValidator:
    """Optional AI validation of trades with news context and persistent caching"""
    
    CACHE_FILE = "ai_cache.json"
    
    def __init__(self, config: dict, news_fetcher: NewsFetcher = None):
        ai_config = config.get("ai", {})
        self.enabled = ai_config.get("enabled", False)
        self.emergency_enabled = ai_config.get("emergency_enabled", True)  # Can disable emergency checks
        self.block_on_uncertain = ai_config.get("block_on_uncertain", True)
        self.min_confidence = ai_config.get("min_confidence", 0.7)
        self.client = None
        self.news_fetcher = news_fetcher
        
        # Cache per token_id (not per question!)
        # Key: token_id, Value: (timestamp, should_trade, reason)
        self._cache_ttl = ai_config.get("cache_hours", 24) * 3600  # Default 24h cache
        self._cache: Dict[str, tuple] = self._load_cache()
        
        if self.enabled and ANTHROPIC_AVAILABLE:
            # Try config first, then env var
            api_key = ai_config.get("api_key", "") or os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                try:
                    self.client = anthropic.Anthropic(api_key=api_key)
                    log(f"AI Validator initialized (cache: {len(self._cache)} entries)")
                except Exception as e:
                    log(f"AI init error: {e}", "ERROR")
                    self.enabled = False
            else:
                log("AI api_key not set in config or ANTHROPIC_API_KEY env - AI disabled", "WARN")
                self.enabled = False
    
    def _load_cache(self) -> Dict[str, tuple]:
        """Load cache from file"""
        if not os.path.exists(self.CACHE_FILE):
            return {}
        try:
            with open(self.CACHE_FILE, "r") as f:
                data = json.load(f)
            # Convert lists back to tuples and filter expired
            cache = {}
            now = time.time()
            for token_id, entry in data.items():
                cached_time, should_trade, reason = entry
                if now - cached_time <= self._cache_ttl:
                    cache[token_id] = (cached_time, should_trade, reason)
            return cache
        except Exception as e:
            log(f"Failed to load AI cache: {e}", "DEBUG")
            return {}
    
    def _save_cache(self):
        """Save cache to file"""
        try:
            with open(self.CACHE_FILE, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            log(f"Failed to save AI cache: {e}", "DEBUG")
    
    def _get_cached(self, token_id: str) -> Optional[tuple]:
        """Get cached result for token_id if still valid"""
        if not token_id or token_id not in self._cache:
            return None
        
        cached_time, should_trade, reason = self._cache[token_id]
        if time.time() - cached_time > self._cache_ttl:
            del self._cache[token_id]
            self._save_cache()
            return None
        
        return (should_trade, reason)
    
    def _set_cache(self, token_id: str, should_trade: bool, reason: str):
        """Cache result for token_id"""
        if token_id:
            self._cache[token_id] = (time.time(), should_trade, reason)
            self._save_cache()
    
    def _get_news_context(self, question: str) -> str:
        """Get news headlines as context for AI"""
        if not self.news_fetcher:
            return ""
        
        headlines = self.news_fetcher.get_headlines(question)
        if not headlines:
            return ""
        
        news_text = "\n".join(f"- {h}" for h in headlines)
        return f"\nRECENT NEWS:\n{news_text}\n"
    
    def validate(self, opportunity: Opportunity) -> tuple[bool, str]:
        """
        Validate opportunity with AI before opening trade.
        Uses cache per token_id to avoid repeated API calls.
        Cost: ~250 tokens per call (with news)
        """
        if not self.enabled or not self.client:
            return True, "AI validation disabled"
        
        # Check cache first (by token_id, not question!)
        cached = self._get_cached(opportunity.token_id)
        if cached:
            should_trade, reason = cached
            return should_trade, f"[CACHED] {reason}"
        
        # Get news context
        news_context = self._get_news_context(opportunity.question)
        
        prompt = f"""You are validating a HIGH PROBABILITY trading strategy on Polymarket.

STRATEGY: We bet on outcomes that are ALREADY very likely (80-99% probability).
We profit when probability rises from e.g. 95% to 100% (market resolves in our favor).
Short time horizons (hours) are GOOD - faster resolution = faster profit.

MARKET: {opportunity.question}
OUR BET: {opportunity.outcome} @ ${opportunity.price:.2f} ({opportunity.price*100:.0f}% implied probability)
ENDS IN: {opportunity.hours_to_end:.1f} hours
POTENTIAL PROFIT: {opportunity.upside*100:.1f}% if we're right

ONLY block this trade if:
1. The outcome is ALREADY DECIDED (game finished, election certified, etc.)
2. Breaking news makes our side UNLIKELY to win (not just uncertain)
3. The probability is clearly WRONG (e.g. betting Yes on something impossible)
{news_context}
Short time = GOOD (faster resolution). High probability = GOOD (that's our strategy).

Reply ONLY with valid JSON:
{{"trade": true, "risk": "low", "reason": "max 10 words"}}
or
{{"trade": false, "risk": "high", "reason": "max 10 words"}}"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text.strip()
            if "```" in text:
                import re
                text = re.sub(r"```json?\n?", "", text)
                text = text.replace("```", "").strip()
            
            data = json.loads(text)
            
            should_trade = data.get("trade", False)
            risk = data.get("risk", "unknown")
            reasoning = data.get("reason", "")
            
            # Determine result
            if not should_trade and self.block_on_uncertain:
                result = (False, f"AI blocked ({risk} risk): {reasoning}")
            elif risk == "high" and self.block_on_uncertain:
                result = (False, f"AI: high risk - {reasoning}")
            else:
                result = (True, f"AI approved ({risk}): {reasoning}")
            
            # Cache the result
            self._set_cache(opportunity.token_id, result[0], result[1])
            
            return result
            
        except Exception as e:
            log(f"AI validation error: {e}", "ERROR")
            return True, "AI error - allowing trade"
    
    def emergency_check(self, pos: 'Position', market_question: str = "") -> tuple[str, str]:
        """
        Emergency AI check for troubled positions.
        Called only when:
        - PnL < -5%
        - Time to end < 2 hours
        
        Cost: ~150 tokens per call (with news)
        Returns: ("HOLD" or "SELL", reason)
        """
        if not self.enabled or not self.client:
            return "HOLD", "AI disabled"
        
        pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price * 100
        hours_left = 0
        try:
            end_dt = datetime.fromisoformat(pos.end_date.replace("Z", "+00:00"))
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except:
            hours_left = 999
        
        # Get news context
        news_context = ""
        if market_question:
            news_context = self._get_news_context(market_question)
        
        prompt = f"""URGENT trading decision:

Market: {pos.market_slug[:60]}
My bet: {pos.outcome}
Entry: ${pos.entry_price:.3f}
Now: ${pos.current_price:.3f} ({pnl_pct:+.1f}%)
Ends in: {hours_left:.1f}h
{news_context}
Should I HOLD or SELL? One word + max 5 word reason.
Format: HOLD: reason OR SELL: reason"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=50,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text.strip().upper()
            
            if "SELL" in text:
                reason = text.replace("SELL:", "").replace("SELL", "").strip()
                return "SELL", reason[:50] if reason else "AI recommends exit"
            else:
                reason = text.replace("HOLD:", "").replace("HOLD", "").strip()
                return "HOLD", reason[:50] if reason else "AI recommends hold"
            
        except Exception as e:
            log(f"AI emergency check error: {e}", "ERROR")
            return "HOLD", "AI error"
    
    def should_emergency_check(self, pos: 'Position') -> bool:
        """
        Determine if position needs emergency AI consultation.
        Returns True only for troubled positions to save tokens.
        """
        if not self.enabled or not self.emergency_enabled:
            return False
        
        # Check PnL threshold (-5%)
        pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price
        if pnl_pct < -0.05:
            return True
        
        # Check time threshold (<2 hours to end)
        try:
            end_dt = datetime.fromisoformat(pos.end_date.replace("Z", "+00:00"))
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_left < 2 and hours_left > 0:
                return True
        except:
            pass
        
        return False

# ==================== AI TRADE ANALYST ====================
class AITradeAnalyst:
    """
    AI-powered trade analysis running hourly.
    Analyzes: active positions, trade history, patterns.
    """
    
    ANALYSIS_FILE = "ai_analysis.json"
    
    def __init__(self, config: dict, news_fetcher: NewsFetcher = None):
        ai_config = config.get("ai", {})
        self.enabled = ai_config.get("analyst_enabled", False)
        self.interval_minutes = ai_config.get("analyst_interval_minutes", 60)
        self.client = None
        self.news_fetcher = news_fetcher
        self.last_analysis = 0
        self.last_recommendations: Dict = {}
        
        if self.enabled and ANTHROPIC_AVAILABLE:
            api_key = ai_config.get("api_key", "") or os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                try:
                    self.client = anthropic.Anthropic(api_key=api_key)
                    log(f"AI Trade Analyst initialized (interval: {self.interval_minutes}min)")
                except Exception as e:
                    log(f"AI Analyst init error: {e}", "ERROR")
                    self.enabled = False
            else:
                self.enabled = False
        
        # Load last analysis
        self._load_analysis()
    
    def _load_analysis(self):
        """Load previous analysis from file"""
        if os.path.exists(self.ANALYSIS_FILE):
            try:
                with open(self.ANALYSIS_FILE, "r") as f:
                    data = json.load(f)
                self.last_analysis = data.get("timestamp", 0)
                self.last_recommendations = data.get("recommendations", {})
            except:
                pass
    
    def _save_analysis(self, recommendations: Dict):
        """Save analysis to file"""
        try:
            with open(self.ANALYSIS_FILE, "w") as f:
                json.dump({
                    "timestamp": time.time(),
                    "recommendations": recommendations
                }, f, indent=2)
        except Exception as e:
            log(f"Failed to save AI analysis: {e}", "DEBUG")
    
    def should_analyze(self) -> bool:
        """Check if it's time for analysis"""
        if not self.enabled or not self.client:
            return False
        return time.time() - self.last_analysis > self.interval_minutes * 60
    
    def analyze_positions(self, positions: Dict[str, Position]) -> Dict:
        """
        Analyze active positions and recommend actions.
        
        Returns dict with recommendations per token_id:
        {
            "token_id": {
                "action": "HOLD" | "CLOSE" | "WATCH",
                "reason": "...",
                "urgency": "low" | "medium" | "high"
            }
        }
        """
        if not self.enabled or not self.client or not positions:
            return {}
        
        # Build positions summary - LIMIT to 10 most important
        pos_list = []
        for token_id, pos in positions.items():
            pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price * 100
            
            # Calculate hours left
            hours_left = 999
            try:
                end_dt = datetime.fromisoformat(pos.end_date.replace("Z", "+00:00"))
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            except:
                pass
            
            pos_list.append({
                "id": token_id[:8],
                "m": pos.market_slug[:40],  # Shortened key
                "out": pos.outcome,
                "pnl": round(pnl_pct, 1),
                "hrs": round(hours_left, 1),
            })
        
        # Sort by urgency: lowest hours first, then biggest losses
        pos_list.sort(key=lambda x: (x["hrs"], x["pnl"]))
        
        # Limit to 10 positions to keep response manageable
        pos_list = pos_list[:10]
        total_positions = len(positions)
        
        prompt = f"""Analyze {len(pos_list)} Polymarket positions (of {total_positions} total).
Strategy: Buy at 85-96% prob, hold until 100% resolution or TP at 98-99%.

POSITIONS:
{json.dumps(pos_list)}

For each position recommend: HOLD (on track), CLOSE (exit now), WATCH (monitor).
Reply ONLY valid JSON, max 8 words per reason:
{{"positions":{{"<id>":{{"a":"HOLD","r":"reason"}}}},"risk":"low|med|high","sum":"summary"}}"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text.strip()
            
            # Clean up markdown code blocks
            if "```" in text:
                import re
                text = re.sub(r"```json?\n?", "", text)
                text = text.replace("```", "").strip()
            
            # Try to fix common JSON issues
            import re
            # Remove trailing commas before } or ]
            text = re.sub(r',\s*}', '}', text)
            text = re.sub(r',\s*]', ']', text)
            
            try:
                result = json.loads(text)
            except json.JSONDecodeError as je:
                # Log the problematic text for debugging
                log(f"AI returned invalid JSON: {text[:200]}...", "DEBUG")
                log(f"JSON error: {je}", "ERROR")
                return {}
            
            # Map back to full token_ids and normalize keys
            recommendations = {}
            for token_id, pos in positions.items():
                short_id = token_id[:8]
                if short_id in result.get("positions", {}):
                    rec = result["positions"][short_id]
                    # Normalize shortened keys to full names
                    recommendations[token_id] = {
                        "action": rec.get("a", rec.get("action", "HOLD")),
                        "reason": rec.get("r", rec.get("reason", "")),
                        "urgency": rec.get("u", rec.get("urgency", "low"))
                    }
            
            recommendations["_meta"] = {
                "portfolio_risk": result.get("risk", result.get("portfolio_risk", "unknown")),
                "summary": result.get("sum", result.get("summary", "")),
                "timestamp": time.time()
            }
            
            self.last_recommendations = recommendations
            self._save_analysis(recommendations)
            self.last_analysis = time.time()
            
            log(f"✅ AI analyzed {len(recommendations)-1} positions")
            
            return recommendations
            
        except Exception as e:
            log(f"AI position analysis error: {e}", "ERROR")
            return {}
    
    def analyze_history(self, trades_file: str = "trades_history.csv") -> Dict:
        """
        Analyze trade history and recommend config changes.
        
        Returns:
        {
            "win_rate": 0.65,
            "avg_profit": 0.05,
            "recommendations": ["...", "..."],
            "category_performance": {...}
        }
        """
        if not self.enabled or not self.client:
            return {}
        
        if not os.path.exists(trades_file):
            return {"error": "No trade history file"}
        
        # Read trades
        trades = []
        try:
            with open(trades_file, "r") as f:
                lines = f.readlines()
            
            # Parse CSV (skip header if exists)
            for line in lines[-100:]:  # Last 100 trades
                parts = line.strip().split(",")
                if len(parts) >= 8 and parts[0] != "timestamp":
                    trades.append({
                        "time": parts[0],
                        "action": parts[1],
                        "market": parts[2][:40],
                        "outcome": parts[3],
                        "price": parts[4],
                        "pnl_pct": parts[7] if len(parts) > 7 else "0",
                        "reason": parts[8] if len(parts) > 8 else ""
                    })
        except Exception as e:
            log(f"Failed to read trades: {e}", "ERROR")
            return {"error": str(e)}
        
        if not trades:
            return {"error": "No trades to analyze"}
        
        # Only analyze closed trades (SELL actions)
        closed_trades = [t for t in trades if t["action"] == "SELL"]
        
        if len(closed_trades) < 5:
            return {"error": "Not enough closed trades (need 5+)"}
        
        prompt = f"""You are a trading performance analyst.

RECENT CLOSED TRADES ({len(closed_trades)}):
{json.dumps(closed_trades[-30:], indent=2)}

CURRENT STRATEGY:
- Buy tokens at 85-96% probability
- Target: price goes to 100% (market resolves in our favor)
- Take profit around 98-99%
- Hold until resolution or TP/SL

ANALYZE the performance and provide:
1. Win rate and patterns
2. Which market categories perform best/worst
3. Optimal entry price range based on results
4. Time-of-day patterns if visible
5. Specific config recommendations

Reply ONLY with valid JSON:
{{
    "metrics": {{
        "win_rate": 0.XX,
        "avg_profit_pct": X.X,
        "avg_loss_pct": -X.X,
        "best_category": "category name",
        "worst_category": "category name"
    }},
    "patterns": [
        "pattern 1",
        "pattern 2"
    ],
    "config_recommendations": [
        {{"setting": "price_min", "current": "0.85", "suggested": "0.XX", "reason": "..."}},
        ...
    ],
    "summary": "max 50 words overall assessment"
}}"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text.strip()
            if "```" in text:
                import re
                text = re.sub(r"```json?\n?", "", text)
                text = text.replace("```", "").strip()
            
            result = json.loads(text)
            result["analyzed_trades"] = len(closed_trades)
            result["timestamp"] = time.time()
            
            return result
            
        except Exception as e:
            log(f"AI history analysis error: {e}", "ERROR")
            return {"error": str(e)}
    
    def get_position_action(self, token_id: str) -> Optional[Dict]:
        """Get recommended action for a specific position"""
        return self.last_recommendations.get(token_id)
    
    def hourly_report(self, positions: Dict[str, Position], discord: 'DiscordNotifier' = None) -> str:
        """
        Run hourly analysis and return/send report.
        """
        if not self.should_analyze():
            return ""
        
        log("🤖 Running AI Trade Analysis...")
        
        # Analyze positions
        pos_analysis = self.analyze_positions(positions)
        
        # Build report
        report_lines = ["## 🤖 AI Hourly Analysis\n"]
        
        # Initialize defaults
        meta = {}
        actions = {"CLOSE": [], "WATCH": [], "HOLD": []}
        
        if pos_analysis:
            meta = pos_analysis.get("_meta", {})
            report_lines.append(f"**Portfolio Risk:** {meta.get('portfolio_risk', 'N/A')}")
            report_lines.append(f"**Summary:** {meta.get('summary', 'N/A')}\n")
            
            # Position recommendations
            for token_id, rec in pos_analysis.items():
                if token_id == "_meta":
                    continue
                action = rec.get("action", "HOLD")
                urgency = rec.get("urgency", "low")
                reason = rec.get("reason", "")
                
                # Find position for display
                pos = positions.get(token_id)
                if pos:
                    market_short = pos.market_slug[:30]
                    pnl = (pos.current_price - pos.entry_price) / pos.entry_price * 100
                    actions[action].append(f"• {market_short} ({pnl:+.1f}%) - {reason} [{urgency}]")
            
            if actions["CLOSE"]:
                report_lines.append("**🔴 CLOSE:**")
                report_lines.extend(actions["CLOSE"])
            
            if actions["WATCH"]:
                report_lines.append("\n**🟡 WATCH:**")
                report_lines.extend(actions["WATCH"])
            
            if actions["HOLD"]:
                report_lines.append(f"\n**🟢 HOLD:** {len(actions['HOLD'])} positions on track")
        else:
            report_lines.append("⚠️ Analysis failed or no data available")
        
        report = "\n".join(report_lines)
        log(report.replace("**", "").replace("##", ""))
        
        # Send to Discord only if we have valid analysis
        if discord and discord.enabled and pos_analysis:
            embed = {
                "title": "🤖 AI Hourly Analysis",
                "description": meta.get("summary", "Analysis complete"),
                "color": 0x9b59b6,  # Purple
                "fields": [],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            if actions["CLOSE"]:
                embed["fields"].append({
                    "name": "🔴 Recommend CLOSE",
                    "value": "\n".join(actions["CLOSE"][:5]) or "None",
                    "inline": False
                })
            
            if actions["WATCH"]:
                embed["fields"].append({
                    "name": "🟡 Watch Closely",
                    "value": "\n".join(actions["WATCH"][:5]) or "None",
                    "inline": False
                })
            
            embed["fields"].append({
                "name": "Portfolio Risk",
                "value": meta.get("portfolio_risk", "N/A"),
                "inline": True
            })
            
            embed["fields"].append({
                "name": "Positions Analyzed",
                "value": str(len(positions)),
                "inline": True
            })
            
            discord.send(None, embed)
        
        return report


# ==================== MAIN BOT ====================
class HighProbBot:
    """Main trading bot with all enhancements"""
    
    def __init__(self):
        self.config = load_config()
        self.file_manager = FileManager(self.config)
        self.poly_client = PolymarketClient(self.config)
        self.news_fetcher = NewsFetcher(self.config)
        self.ai_validator = AIValidator(self.config, self.news_fetcher)
        self.ai_analyst = AITradeAnalyst(self.config, self.news_fetcher)  # NEW
        self.telegram = TelegramNotifier(self.config)
        self.discord = DiscordNotifier(self.config)
        self.stats = StatsTracker(self.config)
        self.dynamic_tp = DynamicTakeProfit(self.config)
        self.trailing_sl = TrailingStopLoss(self.config)
        
        # WebSocket
        self.ws_monitor = WebSocketMonitor(self.config, self._on_price_update)
        
        # State
        self.positions: Dict[str, Position] = {}
        self.blacklist: set = set()
        self.skipped_tokens: set = set()
        self.opportunities: List[Opportunity] = []
        
        # Settings
        self.max_slots = self.config.get("trading", {}).get("max_slots", 5)
        self.trade_amount = self.config.get("trading", {}).get("trade_amount_usd", 2.0)
        self.base_stop_loss = self.config.get("trading", {}).get("stop_loss", 0.70)
        self.stop_loss_enabled = self.config.get("trading", {}).get("stop_loss_enabled", True)
        self.base_take_profit = self.config.get("trading", {}).get("take_profit", 0.98)
        self.check_interval = self.config.get("trading", {}).get("check_interval_seconds", 5)
        
        self.running = False
        
        # Load state
        self._load_state()
    
    def _load_state(self):
        """Load state from files"""
        self.positions = self.file_manager.load_positions()
        self.blacklist = self.file_manager.load_blacklist()
        self.opportunities = self.file_manager.load_opportunities()
        
        # Subscribe to WebSocket for existing positions
        for token_id in self.positions.keys():
            self.ws_monitor.subscribe(token_id)
        
        log(f"Loaded: {len(self.positions)} positions, {len(self.blacklist)} blacklisted, "
            f"{len(self.opportunities)} opportunities")
    
    def _save_state(self):
        """Save state to files"""
        self.file_manager.save_positions(self.positions)
        self.file_manager.update_prices_log(self.positions)
    
    def _get_token_key(self, opp: Opportunity) -> str:
        """Generate unique key for token"""
        return f"{opp.slug}:{opp.outcome}"
    
    def _get_free_slots(self) -> int:
        """Get number of free slots"""
        return self.max_slots - len(self.positions)
    
    def _on_price_update(self, token_id: str, price: float, old_price: float):
        """Callback for WebSocket price updates"""
        if token_id not in self.positions:
            return
        
        pos = self.positions[token_id]
        pos.current_price = price
        pos.last_update = datetime.now(timezone.utc).isoformat()
        
        # Update PnL
        pos.pnl_pct = (price - pos.entry_price) / pos.entry_price
        pos.pnl_usd = pos.amount_usd * pos.pnl_pct
        
        # Check trailing SL
        just_activated, new_sl = self.trailing_sl.update(pos, price)
        if just_activated:
            log(f"📈 Trailing SL activated: {pos.market_slug} | SL: {new_sl:.4f}")
            self.telegram.notify_trailing_sl_activated(pos)
            self.discord.notify_trailing_sl_activated(pos)
    
    def reload_opportunities(self):
        """Reload opportunities from file"""
        self.opportunities = self.file_manager.load_opportunities()
        old_skipped = len(self.skipped_tokens)
        self.skipped_tokens.clear()
        log(f"Reloaded {len(self.opportunities)} opportunities (cleared {old_skipped} skipped)")
    
    def _is_excluded_by_keywords(self, opp: Opportunity) -> bool:
        """Check if opportunity contains excluded keywords"""
        excluded = self.config.get("excluded_keywords", [])
        if not excluded:
            return False
        
        # Check question and slug (case-insensitive)
        text_to_check = f"{opp.question} {opp.slug}".lower()
        
        for keyword in excluded:
            if keyword.lower() in text_to_check:
                return True
        
        return False
    
    def get_next_opportunity(self) -> Optional[Opportunity]:
        """Get next valid opportunity to trade"""
        for opp in self.opportunities:
            token_key = self._get_token_key(opp)
            
            if token_key in self.blacklist:
                continue
            
            if token_key in self.skipped_tokens:
                continue
            
            if opp.token_id and opp.token_id in self.positions:
                continue
            
            existing_slugs = {p.market_slug for p in self.positions.values()}
            if opp.slug in existing_slugs:
                continue
            
            if opp.seconds_to_end <= 0:
                continue
            
            # Check excluded keywords
            if self._is_excluded_by_keywords(opp):
                self.skipped_tokens.add(token_key)
                continue
            
            return opp
        
        return None
    
    def open_trade(self, opp: Opportunity) -> bool:
        """Open a new trade"""
        token_key = self._get_token_key(opp)
        
        log(f"🎯 Opening trade: {opp.slug} | {opp.outcome} @ {opp.price:.4f}")
        log(f"   Ends in: {opp.hours_to_end:.1f}h | Upside: {opp.upside*100:.1f}%")
        
        # AI validation
        if self.ai_validator.enabled:
            should_trade, reason = self.ai_validator.validate(opp)
            if not should_trade:
                log(f"   ❌ AI blocked: {reason}")
                self.blacklist.add(token_key)
                self.file_manager.add_to_blacklist(token_key)
                return False
            log(f"   ✅ AI approved: {reason}")
        
        # Get current BUY price (ASK - what we need to pay)
        current_price = self.poly_client.get_buy_price(opp.token_id) if opp.token_id else opp.price
        if current_price <= 0:
            current_price = opp.price
        
        # Check price range
        min_price = self.config.get("price_min", self.config.get("scanner", {}).get("min_price", 0.90))
        max_price = self.config.get("price_max", self.config.get("scanner", {}).get("max_price", 0.99))
        
        if current_price < min_price or current_price > max_price:
            log(f"   ⚠️ Price out of range: {current_price:.4f} (need {min_price}-{max_price}) - SKIPPING")
            self.skipped_tokens.add(token_key)
            return False
        
        # Calculate dynamic take profit
        take_profit = self.dynamic_tp.calculate(current_price)
        log(f"   📊 Dynamic TP: {take_profit:.4f} ({(take_profit/current_price-1)*100:.1f}% profit)")
        
        # Place order
        result = self.poly_client.place_market_order(opp.token_id, self.trade_amount, current_price)
        
        if not result:
            log(f"   ❌ Order failed")
            return False
        
        # Create position
        position = Position(
            token_id=opp.token_id,
            market_slug=opp.slug,
            outcome=opp.outcome,
            side="BUY",
            entry_price=result["filled_price"],
            entry_time=datetime.now(timezone.utc).isoformat(),
            amount_usd=self.trade_amount,
            shares=result["filled_shares"],
            stop_loss=self.base_stop_loss,
            take_profit=take_profit,
            initial_stop_loss=self.base_stop_loss,
            trailing_sl_enabled=self.trailing_sl.enabled,
            trailing_sl_activated=False,
            peak_price=result["filled_price"],
            current_price=result["filled_price"],
            last_update=datetime.now(timezone.utc).isoformat(),
            end_date=opp.end_date,
            question=opp.question,
            url=opp.url,
            status="OPEN"
        )
        
        self.positions[opp.token_id] = position
        self._save_state()
        
        # Subscribe to WebSocket (and start if not running)
        self.ws_monitor.subscribe(opp.token_id)
        if not self.ws_monitor.running and self.ws_monitor.enabled:
            log("Starting WebSocket for new position...")
            self.ws_monitor.start()
            time.sleep(1)  # Give time to connect
        
        # Notify
        self.telegram.notify_trade_opened(position)
        self.discord.notify_trade_opened(position)
        
        log(f"   ✅ Position opened: {result['filled_shares']:.2f} shares @ ${result['filled_price']:.4f}")
        log(f"   📉 SL: {position.stop_loss:.3f} | 📈 TP: {position.take_profit:.3f} | Trailing: {'ON' if position.trailing_sl_enabled else 'OFF'}")
        
        return True
    
    def close_trade(self, token_id: str, reason: str, exit_price: float):
        """Close a trade"""
        if token_id not in self.positions:
            return
        
        pos = self.positions[token_id]
        token_key = f"{pos.market_slug}:{pos.outcome}"
        
        # Calculate PnL
        if reason == "RESOLVED":
            winning_outcome = self.poly_client.check_resolution(pos.market_slug)
            if winning_outcome:
                if winning_outcome.lower() == pos.outcome.lower():
                    exit_price = 1.0
                else:
                    exit_price = 0.0
        
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
        pnl_usd = pos.amount_usd * pnl_pct
        
        # Update position
        pos.status = "CLOSED"
        pos.exit_price = exit_price
        pos.exit_time = datetime.now(timezone.utc).isoformat()
        pos.close_reason = reason
        pos.pnl_usd = pnl_usd
        pos.pnl_pct = pnl_pct
        
        # Log and track
        self.file_manager.log_trade(pos)
        self.stats.record_trade(pos)
        
        # Blacklist
        self.blacklist.add(token_key)
        self.file_manager.add_to_blacklist(token_key)
        
        # Unsubscribe WebSocket
        self.ws_monitor.unsubscribe(token_id)
        
        # Remove from positions
        del self.positions[token_id]
        self._save_state()
        
        # Notify
        self.telegram.notify_trade_closed(pos)
        self.discord.notify_trade_closed(pos)
        
        # Log result
        result = "WIN ✅" if pnl_usd > 0 else "LOSS ❌"
        log(f"📤 CLOSED: {pos.market_slug} | {reason} | "
            f"PnL: {pnl_pct*100:+.1f}% (${pnl_usd:+.2f}) | {result}")
    
    def monitor_positions(self):
        """Monitor all open positions"""
        positions_to_close = []
        
        for token_id, pos in list(self.positions.items()):
            # Calculate position age for grace period checks
            position_age_minutes = 999
            try:
                entry = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                position_age_minutes = (datetime.now(timezone.utc) - entry).total_seconds() / 60
            except:
                pass
            
            # Get current price (prefer WebSocket, fallback to REST)
            ws_price = self.ws_monitor.get_price(token_id)
            if ws_price > 0:
                current_price = ws_price
            else:
                # Use SELL price (BID) - this is what we'd get if we sold now
                current_price = self.poly_client.get_sell_price(token_id)
            
            if current_price > 0:
                pos.current_price = current_price
                pos.last_update = datetime.now(timezone.utc).isoformat()
                
                # Update PnL
                pos.pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                pos.pnl_usd = pos.amount_usd * pos.pnl_pct
                
                # Update trailing SL
                just_activated, new_sl = self.trailing_sl.update(pos, current_price)
                if just_activated:
                    log(f"📈 Trailing SL activated: {pos.market_slug} | New SL: {new_sl:.4f}")
                    self.telegram.notify_trailing_sl_activated(pos)
                    self.discord.notify_trailing_sl_activated(pos)
            
            # ===== CHECK RESOLUTION FIRST! =====
            # This prevents false stop-loss triggers when market resolves
            winning_outcome = self.poly_client.check_resolution(pos.market_slug)
            if winning_outcome:
                # Market resolved - check if we won
                we_won = (winning_outcome == pos.outcome)
                final_price = 1.0 if we_won else 0.0
                pos.current_price = final_price
                pos.pnl_pct = (final_price - pos.entry_price) / pos.entry_price
                pos.pnl_usd = pos.amount_usd * pos.pnl_pct
                
                result = "✅ WON" if we_won else "❌ LOST"
                log(f"🏁 Market resolved: {pos.market_slug} | Winner: {winning_outcome} | We bet: {pos.outcome} | {result}")
                positions_to_close.append((token_id, "RESOLVED", final_price))
                continue
            
            # ===== Check TAKE-PROFIT (before SL to catch wins at 0.98-0.99) =====
            if pos.current_price > 0 and pos.current_price >= pos.take_profit:
                profit_pct = (pos.current_price - pos.entry_price) / pos.entry_price * 100
                log(f"🎯 TAKE-PROFIT triggered: {pos.market_slug} @ {pos.current_price:.4f} (+{profit_pct:.1f}%)")
                
                if not self.poly_client.demo_mode:
                    # Only verify position exists if older than 5 minutes (grace period for blockchain sync)
                    if position_age_minutes > 5:
                        if not self.poly_client.has_position(token_id, pos.shares * 0.5):
                            log(f"   ⚠️ Token not found on Polymarket - removing stale position", "WARN")
                            positions_to_close.append((token_id, "STALE_REMOVED", pos.current_price))
                            continue
                    
                    result = self.poly_client.place_sell_order(token_id, pos.shares, pos.current_price)
                    if not result:
                        log(f"   ⚠️ SELL failed - will retry next cycle", "WARN")
                        continue  # Don't close position if sell failed
                
                positions_to_close.append((token_id, "TAKE_PROFIT", pos.current_price))
                continue
            
            # ===== Check STOP-LOSS =====
            # Only trigger if SL is enabled and price is below SL
            if self.stop_loss_enabled and pos.current_price > 0 and pos.current_price < pos.stop_loss:
                # Extra safety: if price suddenly drops to near 0, check resolution again
                if pos.current_price < 0.05:
                    # Price near 0 might mean market resolved against us
                    # Double-check resolution status
                    log(f"⚠️ Price near 0 ({pos.current_price:.4f}), checking if market resolved...")
                    winning_outcome = self.poly_client.check_resolution(pos.market_slug)
                    if winning_outcome:
                        we_won = (winning_outcome == pos.outcome)
                        final_price = 1.0 if we_won else 0.0
                        result = "✅ WON" if we_won else "❌ LOST"
                        log(f"🏁 Market resolved: {pos.market_slug} | Winner: {winning_outcome} | {result}")
                        positions_to_close.append((token_id, "RESOLVED", final_price))
                        continue
                
                sl_type = "TRAILING_SL" if pos.trailing_sl_activated else "STOP_LOSS"
                log(f"🛑 {sl_type} triggered: {pos.market_slug} @ {pos.current_price:.4f}")
                
                if not self.poly_client.demo_mode:
                    # Only verify position exists if older than 5 minutes
                    if position_age_minutes > 5:
                        if not self.poly_client.has_position(token_id, pos.shares * 0.5):
                            log(f"   ⚠️ Token not found on Polymarket - removing stale position", "WARN")
                            positions_to_close.append((token_id, "STALE_REMOVED", pos.current_price))
                            continue
                    
                    result = self.poly_client.place_sell_order(token_id, pos.shares, pos.current_price)
                    if not result:
                        log(f"   ⚠️ SELL failed - will retry next cycle", "WARN")
                        continue  # Don't close position if sell failed
                
                positions_to_close.append((token_id, sl_type, pos.current_price))
                continue
            
            # AI EMERGENCY CHECK - only for troubled positions
            if self.ai_validator.should_emergency_check(pos):
                # Avoid checking too often - use a simple cooldown
                last_check_key = f"ai_check_{token_id}"
                last_check = getattr(self, '_ai_check_times', {}).get(last_check_key, 0)
                
                # Check at most once per 30 minutes per position
                if time.time() - last_check > 1800:
                    action, reason = self.ai_validator.emergency_check(pos, pos.question)
                    
                    # Store check time
                    if not hasattr(self, '_ai_check_times'):
                        self._ai_check_times = {}
                    self._ai_check_times[last_check_key] = time.time()
                    
                    if action == "SELL":
                        log(f"🤖 AI EMERGENCY: {pos.market_slug} | SELL recommended: {reason}")
                        
                        if not self.poly_client.demo_mode:
                            # Only verify position exists if older than 5 minutes
                            if position_age_minutes > 5:
                                if not self.poly_client.has_position(token_id, pos.shares * 0.5):
                                    log(f"   ⚠️ Token not found on Polymarket - removing stale position", "WARN")
                                    positions_to_close.append((token_id, "STALE_REMOVED", pos.current_price))
                                    continue
                            
                            result = self.poly_client.place_sell_order(token_id, pos.shares, pos.current_price)
                            if not result:
                                log(f"   ⚠️ SELL failed - will retry next cycle", "WARN")
                                continue  # Don't close position if sell failed
                        
                        positions_to_close.append((token_id, "AI_EMERGENCY", pos.current_price))
                        continue
                    else:
                        log(f"🤖 AI check: {pos.market_slug} | HOLD: {reason}", "DEBUG")
            
            # Check manual close (live mode only)
            if not self.poly_client.demo_mode:
                # This is already handled by has_position checks above
                # Remove redundant check that would spam API
                pass
        
        # Close positions
        for token_id, reason, price in positions_to_close:
            self.close_trade(token_id, reason, price)
        
        # Save state
        self._save_state()
    
    def print_status(self):
        """Print current status"""
        demo_str = "DEMO" if self.poly_client.demo_mode else "LIVE"
        free_slots = self._get_free_slots()
        
        log("-" * 70)
        log(f"📊 STATUS | Mode: {demo_str} | Slots: {len(self.positions)}/{self.max_slots} | Free: {free_slots}")
        
        # Stats summary
        stats = self.stats.get_summary()
        total_invested = sum(p.amount_usd for p in self.positions.values())
        total_open_pnl = sum(p.pnl_usd for p in self.positions.values())
        
        log(f"   Invested: ${total_invested:.2f} | Open PnL: ${total_open_pnl:+.2f} | "
            f"Realized: ${stats['total_pnl']:+.2f}")
        log(f"   Win Rate: {stats['win_rate']:.0f}% ({stats['wins']}/{stats['total_trades']}) | "
            f"Streak: W{stats['win_streak']}/L{stats['loss_streak']}")
        log(f"   Opportunities: {len(self.opportunities)} | Blacklisted: {len(self.blacklist)} | "
            f"Skipped: {len(self.skipped_tokens)}")
        
        # Show positions
        if self.positions:
            log("📈 OPEN POSITIONS:")
            for pos in sorted(self.positions.values(), key=lambda p: p.end_date):
                pnl_emoji = "🟢" if pos.pnl_pct >= 0 else "🔴"
                tsl_indicator = "🔄" if pos.trailing_sl_activated else ""
                
                try:
                    end_dt = datetime.fromisoformat(pos.end_date.replace("Z", "+00:00"))
                    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    hours_str = f"{hours_left:.1f}h"
                except:
                    hours_str = "?"
                
                log(f"   {pnl_emoji}{tsl_indicator} {pos.market_slug[:30]:<30} | {pos.outcome} | "
                    f"Entry: {pos.entry_price:.3f} | Now: {pos.current_price:.3f} | "
                    f"PnL: {pos.pnl_pct*100:+.1f}% | SL: {pos.stop_loss:.2f} | "
                    f"TP: {pos.take_profit:.2f} | Ends: {hours_str}")
        
        log("-" * 70)
    
    def run(self):
        """Main run loop"""
        self.running = True
        
        log("=" * 70)
        log("🚀 HIGH PROBABILITY BOT v2 STARTED")
        log("=" * 70)
        
        if self.poly_client.demo_mode:
            log("Mode: 🔵 DEMO (simulation only)")
        else:
            log("Mode: 🔴 LIVE TRADING - REAL MONEY!", "WARN")
            log("⚠️  Orders will be placed on Polymarket!", "WARN")
        
        log(f"Max slots: {self.max_slots} | Trade amount: ${self.trade_amount}")
        log(f"Stop Loss: {'ON (' + str(self.base_stop_loss) + ')' if self.stop_loss_enabled else 'OFF'} | Take Profit: {self.base_take_profit}")
        log(f"Dynamic TP: {'ON' if self.dynamic_tp.enabled else 'OFF'}")
        log(f"Trailing SL: {'ON' if self.trailing_sl.enabled and self.stop_loss_enabled else 'OFF'}")
        log(f"WebSocket: {'ON' if self.ws_monitor.enabled else 'OFF'}")
        log(f"Telegram: {'ON' if self.telegram.enabled else 'OFF'}")
        log(f"Discord: {'ON' if self.discord.enabled else 'OFF'}")
        log(f"News: {'ON' if self.news_fetcher.enabled else 'OFF'}")
        log(f"AI validation: {'ON' if self.ai_validator.enabled else 'OFF'}")
        log(f"AI Analyst: {'ON (' + str(self.ai_analyst.interval_minutes) + 'min)' if self.ai_analyst.enabled else 'OFF'}")
        log("=" * 70)
        
        # Start WebSocket (if there are tokens to monitor)
        if self.ws_monitor.subscribed_tokens:
            log(f"Starting WebSocket with {len(self.ws_monitor.subscribed_tokens)} tokens...")
            self.ws_monitor.start()
            time.sleep(2)  # Give time to connect
            ws_status = "✅ connected" if self.ws_monitor.is_connected() else "⏳ connecting..."
            log(f"WebSocket: {ws_status}")
        else:
            log("No existing positions - WebSocket will start when first trade opens")
        
        last_reload = time.time()
        reload_interval = 300  # 5 minutes
        
        last_daily_summary = datetime.now().date()
        status_counter = 0
        
        try:
            while self.running:
                # Reload opportunities periodically
                if time.time() - last_reload > reload_interval:
                    self.reload_opportunities()
                    last_reload = time.time()
                
                # Monitor existing positions
                self.monitor_positions()
                
                # AI Trade Analyst - hourly analysis
                if self.ai_analyst.should_analyze() and self.positions:
                    self.ai_analyst.hourly_report(self.positions, self.discord)
                
                # Open new trades
                free_slots = self._get_free_slots()
                if free_slots > 0:
                    opp = self.get_next_opportunity()
                    if opp:
                        self.open_trade(opp)
                    elif status_counter == 0:  # Only log once per status cycle
                        # Count why we have no opportunities
                        total = len(self.opportunities)
                        blacklisted = sum(1 for o in self.opportunities if self._get_token_key(o) in self.blacklist)
                        skipped = sum(1 for o in self.opportunities if self._get_token_key(o) in self.skipped_tokens)
                        in_position = sum(1 for o in self.opportunities if o.token_id in self.positions)
                        
                        if total == 0:
                            log("⏳ No opportunities in file - run scanner.py to find new ones")
                        else:
                            log(f"⏳ No valid opportunities ({total} total: {blacklisted} blacklisted, {skipped} skipped, {in_position} in position)")
                
                # Print status every 30 seconds
                status_counter += 1
                if status_counter >= 30 // self.check_interval:
                    status_counter = 0
                    self.print_status()
                
                # Daily summary
                today = datetime.now().date()
                if today != last_daily_summary:
                    stats = self.stats.get_summary()
                    stats["open_positions"] = len(self.positions)
                    stats["open_pnl"] = sum(p.pnl_usd for p in self.positions.values())
                    self.telegram.notify_daily_summary(stats)
                    self.discord.notify_daily_summary(stats)
                    last_daily_summary = today
                
                time.sleep(self.check_interval)
                
        except KeyboardInterrupt:
            log("Shutting down...")
        finally:
            self.running = False
            self.ws_monitor.stop()
            self._save_state()
            log("State saved. Bot stopped.")

# ==================== ENTRY POINT ====================
def main():
    config = load_config()
    opp_file = config.get("files", {}).get("opportunities", "opportunities.json")
    
    if not os.path.exists(opp_file):
        log(f"Opportunities file not found: {opp_file}", "ERROR")
        log("Run scanner.py first to generate opportunities")
        return
    
    bot = HighProbBot()
    bot.run()

if __name__ == "__main__":
    main()
