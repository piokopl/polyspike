#!/usr/bin/env python3
# dashboard_web.py - Enhanced Web Dashboard for High Probability Bot v2
#
# Features:
# - Real-time positions with trailing SL indicator
# - PnL charts (daily, cumulative)
# - Win rate by hour heatmap
# - Performance by close reason
# - Trade history
#
# Run: python3 dashboard_web.py
# Open: http://localhost:5000

import os
import json
import csv
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# Files
POSITIONS_FILE = "positions.json"
TRADES_HISTORY_FILE = "trades_history.csv"
OPPORTUNITIES_FILE = "opportunities.json"
BLACKLIST_FILE = "blacklist.json"
CONFIG_FILE = "config.json"
STATS_FILE = "stats.json"

def load_json(filepath: str) -> Any:
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except:
        return None

def load_csv(filepath: str) -> List[Dict]:
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except:
        return []

def get_dashboard_data() -> Dict:
    config = load_json(CONFIG_FILE) or {}
    positions = load_json(POSITIONS_FILE) or {}
    opportunities = load_json(OPPORTUNITIES_FILE) or []
    blacklist = load_json(BLACKLIST_FILE) or []
    stats = load_json(STATS_FILE) or {}
    trades = load_csv(TRADES_HISTORY_FILE)
    
    # Process positions
    positions_list = []
    total_invested = 0
    total_open_pnl = 0
    
    for token_id, pos in positions.items():
        entry = pos.get("entry_price", 0)
        current = pos.get("current_price", entry)
        pnl_pct = pos.get("pnl_pct", 0)
        amount = pos.get("amount_usd", 0)
        
        try:
            end_date = pos.get("end_date", "")
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except:
            hours_left = 0
        
        total_invested += amount
        total_open_pnl += amount * pnl_pct
        
        positions_list.append({
            "market": pos.get("market_slug", "?"),
            "outcome": pos.get("outcome", "?"),
            "entry": entry,
            "current": current,
            "pnl_pct": pnl_pct * 100,
            "pnl_usd": amount * pnl_pct,
            "hours_left": hours_left,
            "stop_loss": pos.get("stop_loss", 0),
            "take_profit": pos.get("take_profit", 0.98),
            "initial_sl": pos.get("initial_stop_loss", 0),
            "trailing_activated": pos.get("trailing_sl_activated", False),
            "peak_price": pos.get("peak_price", 0),
        })
    
    positions_list.sort(key=lambda x: x["hours_left"])
    
    # Process trades
    wins = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    realized_pnl = sum(float(t.get("pnl_usd", 0)) for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    
    # Daily PnL for chart
    daily_pnl = stats.get("daily_pnl", {})
    daily_chart = []
    for date, pnl in sorted(daily_pnl.items())[-14:]:  # Last 14 days
        daily_chart.append({"date": date, "pnl": pnl})
    
    # Cumulative PnL
    cumulative = 0
    cumulative_chart = []
    for item in daily_chart:
        cumulative += item["pnl"]
        cumulative_chart.append({"date": item["date"], "pnl": cumulative})
    
    # Hourly distribution
    hourly = stats.get("hourly_trades", {})
    hourly_chart = [{"hour": h, "count": hourly.get(h, 0)} for h in [f"{i:02d}" for i in range(24)]]
    
    # By reason stats
    by_reason = stats.get("by_reason", {})
    reason_chart = []
    for reason, data in by_reason.items():
        wr = (data.get("wins", 0) / data.get("count", 1)) * 100 if data.get("count", 0) > 0 else 0
        reason_chart.append({
            "reason": reason,
            "count": data.get("count", 0),
            "pnl": data.get("pnl", 0),
            "win_rate": wr
        })
    
    # Opportunities
    available_opps = []
    for opp in opportunities[:10]:
        token_key = f"{opp.get('slug')}:{opp.get('outcome')}"
        if token_key not in blacklist:
            available_opps.append({
                "market": opp.get("slug", "?"),
                "outcome": opp.get("outcome", "?"),
                "price": opp.get("price", 0),
                "hours": opp.get("hours_to_end", 0),
                "upside": opp.get("upside", 0) * 100,
            })
    
    # Recent trades
    recent_trades = []
    for trade in reversed(trades[-20:]):
        recent_trades.append({
            "time": trade.get("timestamp", "")[:16],
            "market": trade.get("market_slug", "?"),
            "outcome": trade.get("outcome", "?"),
            "entry": float(trade.get("entry_price", 0)),
            "exit": float(trade.get("exit_price", 0)),
            "pnl_usd": float(trade.get("pnl_usd", 0)),
            "pnl_pct": trade.get("pnl_pct", "0%"),
            "result": trade.get("result", "?"),
            "reason": trade.get("close_reason", "?"),
            "duration": trade.get("duration_hours", "?"),
        })
    
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "mode": config.get("mode", "unknown").upper(),
            "max_slots": config.get("trading", {}).get("max_slots", 5),
            "trade_amount": config.get("trading", {}).get("trade_amount_usd", 2),
            "stop_loss": config.get("trading", {}).get("stop_loss", 0.7),
            "take_profit": config.get("trading", {}).get("take_profit", 0.98),
            "dynamic_tp": config.get("dynamic_tp", {}).get("enabled", False),
            "trailing_sl": config.get("trailing_sl", {}).get("enabled", False),
            "websocket": config.get("websocket", {}).get("enabled", False),
        },
        "positions": positions_list,
        "stats": {
            "open_positions": len(positions_list),
            "total_invested": total_invested,
            "open_pnl": total_open_pnl,
            "realized_pnl": realized_pnl,
            "total_pnl": total_open_pnl + realized_pnl,
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "blacklisted": len(blacklist),
            "best_trade": stats.get("best_trade", 0),
            "worst_trade": stats.get("worst_trade", 0),
            "avg_hold_time": stats.get("avg_hold_time_hours", 0),
            "win_streak": stats.get("win_streak", 0),
            "loss_streak": stats.get("loss_streak", 0),
            "max_win_streak": stats.get("max_win_streak", 0),
            "max_loss_streak": stats.get("max_loss_streak", 0),
        },
        "charts": {
            "daily_pnl": daily_chart,
            "cumulative_pnl": cumulative_chart,
            "hourly": hourly_chart,
            "by_reason": reason_chart,
        },
        "opportunities": available_opps,
        "trades": recent_trades,
    }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>High Probability Bot v2 - Dashboard</title>
    <meta http-equiv="refresh" content="5">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
            color: #eee;
            min-height: 100vh;
            padding: 15px;
        }
        
        .container { max-width: 1600px; margin: 0 auto; }
        
        header {
            text-align: center;
            margin-bottom: 20px;
            padding: 15px;
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.08);
        }
        
        header h1 {
            font-size: 1.8em;
            margin-bottom: 8px;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .mode-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 15px;
            font-weight: bold;
            font-size: 0.8em;
            margin-left: 8px;
        }
        
        .mode-demo { background: #4CAF50; color: white; }
        .mode-live { background: #f44336; color: white; }
        
        .features {
            display: flex;
            justify-content: center;
            gap: 15px;
            margin-top: 10px;
            flex-wrap: wrap;
        }
        
        .feature-badge {
            padding: 3px 10px;
            border-radius: 10px;
            font-size: 0.75em;
            background: rgba(0,217,255,0.2);
            color: #00d9ff;
        }
        
        .feature-badge.off {
            background: rgba(255,255,255,0.1);
            color: #666;
        }
        
        .updated { color: #666; font-size: 0.85em; }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 15px;
            margin-bottom: 15px;
        }
        
        .grid-2 {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 15px;
            margin-bottom: 15px;
        }
        
        .card {
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            padding: 15px;
            border: 1px solid rgba(255,255,255,0.08);
        }
        
        .card h2 {
            font-size: 1em;
            margin-bottom: 12px;
            color: #00d9ff;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .stat-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
        }
        
        .stat-item {
            text-align: center;
            padding: 12px 8px;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
        }
        
        .stat-value {
            font-size: 1.5em;
            font-weight: bold;
        }
        
        .stat-label {
            font-size: 0.7em;
            color: #666;
            margin-top: 3px;
        }
        
        .positive { color: #00ff88; }
        .negative { color: #ff4757; }
        .neutral { color: #ffd93d; }
        
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8em;
        }
        
        th, td {
            padding: 10px 6px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        
        th {
            color: #666;
            font-weight: normal;
            font-size: 0.8em;
            text-transform: uppercase;
        }
        
        tr:hover { background: rgba(255,255,255,0.03); }
        
        .badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: bold;
        }
        
        .badge-win { background: #00ff88; color: #000; }
        .badge-loss { background: #ff4757; color: #fff; }
        .badge-yes { background: #4CAF50; color: #fff; }
        .badge-no { background: #2196F3; color: #fff; }
        
        .trailing-badge {
            background: #ffd93d;
            color: #000;
            font-size: 0.65em;
            padding: 1px 4px;
            border-radius: 3px;
            margin-left: 5px;
        }
        
        .chart-container {
            height: 200px;
            margin-top: 10px;
        }
        
        .empty-state {
            text-align: center;
            padding: 25px;
            color: #444;
        }
        
        .full-width { grid-column: 1 / -1; }
        
        .progress-bar {
            height: 6px;
            background: rgba(255,255,255,0.1);
            border-radius: 3px;
            overflow: hidden;
            margin-top: 4px;
        }
        
        .progress-fill {
            height: 100%;
            border-radius: 3px;
        }
        
        .mini-stats {
            display: flex;
            gap: 15px;
            justify-content: center;
            margin-top: 8px;
            font-size: 0.8em;
            color: #888;
        }
        
        @media (max-width: 768px) {
            .stat-grid { grid-template-columns: 1fr; }
            .grid-2 { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>📊 High Probability Bot v2
                <span class="mode-badge mode-{{ data.config.mode.lower() }}">{{ data.config.mode }}</span>
            </h1>
            <div class="features">
                <span class="feature-badge {{ '' if data.config.dynamic_tp else 'off' }}">Dynamic TP</span>
                <span class="feature-badge {{ '' if data.config.trailing_sl else 'off' }}">Trailing SL</span>
                <span class="feature-badge {{ '' if data.config.websocket else 'off' }}">WebSocket</span>
            </div>
            <p class="updated">Updated: {{ data.updated }} • Auto-refresh 5s</p>
        </header>
        
        <!-- Stats Cards -->
        <div class="grid">
            <div class="card">
                <h2>💰 Performance</h2>
                <div class="stat-grid">
                    <div class="stat-item">
                        <div class="stat-value {{ 'positive' if data.stats.total_pnl >= 0 else 'negative' }}">
                            ${{ "%.2f"|format(data.stats.total_pnl) }}
                        </div>
                        <div class="stat-label">Total PnL</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value {{ 'positive' if data.stats.win_rate >= 50 else 'negative' }}">
                            {{ "%.0f"|format(data.stats.win_rate) }}%
                        </div>
                        <div class="stat-label">Win Rate ({{ data.stats.wins }}/{{ data.stats.total_trades }})</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value {{ 'positive' if data.stats.open_pnl >= 0 else 'negative' }}">
                            ${{ "%.2f"|format(data.stats.open_pnl) }}
                        </div>
                        <div class="stat-label">Open PnL</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value {{ 'positive' if data.stats.realized_pnl >= 0 else 'negative' }}">
                            ${{ "%.2f"|format(data.stats.realized_pnl) }}
                        </div>
                        <div class="stat-label">Realized PnL</div>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h2>📈 Trading Stats</h2>
                <div class="stat-grid">
                    <div class="stat-item">
                        <div class="stat-value">{{ data.stats.open_positions }}/{{ data.config.max_slots }}</div>
                        <div class="stat-label">Open Positions</div>
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: {{ (data.stats.open_positions / data.config.max_slots * 100)|int }}%; background: #00d9ff;"></div>
                        </div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">${{ "%.2f"|format(data.stats.total_invested) }}</div>
                        <div class="stat-label">Invested</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value positive">${{ "%.2f"|format(data.stats.best_trade) }}</div>
                        <div class="stat-label">Best Trade</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value negative">${{ "%.2f"|format(data.stats.worst_trade) }}</div>
                        <div class="stat-label">Worst Trade</div>
                    </div>
                </div>
                <div class="mini-stats">
                    <span>Avg Hold: {{ "%.1f"|format(data.stats.avg_hold_time) }}h</span>
                    <span>Win Streak: {{ data.stats.win_streak }} (max {{ data.stats.max_win_streak }})</span>
                </div>
            </div>
            
            <div class="card">
                <h2>⚙️ Configuration</h2>
                <div class="stat-grid">
                    <div class="stat-item">
                        <div class="stat-value">${{ data.config.trade_amount }}</div>
                        <div class="stat-label">Trade Size</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ data.config.stop_loss }}</div>
                        <div class="stat-label">Stop Loss</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ data.config.take_profit }}</div>
                        <div class="stat-label">Take Profit</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ data.stats.blacklisted }}</div>
                        <div class="stat-label">Blacklisted</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Charts -->
        <div class="grid-2">
            <div class="card">
                <h2>📊 Daily PnL</h2>
                <div class="chart-container">
                    <canvas id="dailyPnlChart"></canvas>
                </div>
            </div>
            <div class="card">
                <h2>📈 Cumulative PnL</h2>
                <div class="chart-container">
                    <canvas id="cumulativePnlChart"></canvas>
                </div>
            </div>
        </div>
        
        <!-- Positions Table -->
        <div class="card full-width" style="margin-bottom: 15px;">
            <h2>📈 Open Positions</h2>
            {% if data.positions %}
            <table>
                <thead>
                    <tr>
                        <th>Market</th>
                        <th>Side</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>Peak</th>
                        <th>PnL</th>
                        <th>SL</th>
                        <th>TP</th>
                        <th>Ends</th>
                    </tr>
                </thead>
                <tbody>
                    {% for pos in data.positions %}
                    <tr>
                        <td>
                            {{ pos.market[:35] }}
                            {% if pos.trailing_activated %}<span class="trailing-badge">TSL</span>{% endif %}
                        </td>
                        <td><span class="badge badge-{{ pos.outcome.lower() }}">{{ pos.outcome }}</span></td>
                        <td>{{ "%.3f"|format(pos.entry) }}</td>
                        <td>{{ "%.3f"|format(pos.current) }}</td>
                        <td>{{ "%.3f"|format(pos.peak_price) }}</td>
                        <td class="{{ 'positive' if pos.pnl_pct >= 0 else 'negative' }}">
                            {{ "%+.1f"|format(pos.pnl_pct) }}%
                        </td>
                        <td class="{{ 'neutral' if pos.stop_loss > pos.initial_sl else '' }}">
                            {{ "%.3f"|format(pos.stop_loss) }}
                        </td>
                        <td>{{ "%.3f"|format(pos.take_profit) }}</td>
                        <td>{{ "%.1f"|format(pos.hours_left) }}h</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty-state">No open positions</div>
            {% endif %}
        </div>
        
        <div class="grid-2">
            <!-- Opportunities -->
            <div class="card">
                <h2>🎯 Top Opportunities</h2>
                {% if data.opportunities %}
                <table>
                    <thead>
                        <tr>
                            <th>Market</th>
                            <th>Side</th>
                            <th>Price</th>
                            <th>Upside</th>
                            <th>Ends</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for opp in data.opportunities %}
                        <tr>
                            <td>{{ opp.market[:30] }}</td>
                            <td><span class="badge badge-{{ opp.outcome.lower() }}">{{ opp.outcome }}</span></td>
                            <td>{{ "%.3f"|format(opp.price) }}</td>
                            <td class="positive">+{{ "%.1f"|format(opp.upside) }}%</td>
                            <td>{{ "%.1f"|format(opp.hours) }}h</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty-state">No opportunities</div>
                {% endif %}
            </div>
            
            <!-- Recent Trades -->
            <div class="card">
                <h2>📜 Recent Trades</h2>
                {% if data.trades %}
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Market</th>
                            <th>PnL</th>
                            <th>Reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for trade in data.trades[:12] %}
                        <tr>
                            <td>{{ trade.time[5:] }}</td>
                            <td>{{ trade.market[:18] }}</td>
                            <td class="{{ 'positive' if trade.pnl_usd >= 0 else 'negative' }}">
                                ${{ "%+.2f"|format(trade.pnl_usd) }}
                            </td>
                            <td>
                                <span class="badge badge-{{ trade.result.lower() }}">{{ trade.reason[:8] }}</span>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty-state">No trades yet</div>
                {% endif %}
            </div>
        </div>
        
        <!-- Performance by Reason -->
        {% if data.charts.by_reason %}
        <div class="card" style="margin-top: 15px;">
            <h2>📊 Performance by Close Reason</h2>
            <table>
                <thead>
                    <tr>
                        <th>Reason</th>
                        <th>Count</th>
                        <th>Win Rate</th>
                        <th>Total PnL</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in data.charts.by_reason %}
                    <tr>
                        <td>{{ item.reason }}</td>
                        <td>{{ item.count }}</td>
                        <td class="{{ 'positive' if item.win_rate >= 50 else 'negative' }}">
                            {{ "%.0f"|format(item.win_rate) }}%
                        </td>
                        <td class="{{ 'positive' if item.pnl >= 0 else 'negative' }}">
                            ${{ "%.2f"|format(item.pnl) }}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
    </div>
    
    <script>
        // Daily PnL Chart
        const dailyData = {{ data.charts.daily_pnl | tojson }};
        if (dailyData.length > 0) {
            new Chart(document.getElementById('dailyPnlChart'), {
                type: 'bar',
                data: {
                    labels: dailyData.map(d => d.date.slice(5)),
                    datasets: [{
                        data: dailyData.map(d => d.pnl),
                        backgroundColor: dailyData.map(d => d.pnl >= 0 ? '#00ff88' : '#ff4757'),
                        borderRadius: 4,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: '#666' } },
                        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#666' } }
                    }
                }
            });
        }
        
        // Cumulative PnL Chart
        const cumData = {{ data.charts.cumulative_pnl | tojson }};
        if (cumData.length > 0) {
            new Chart(document.getElementById('cumulativePnlChart'), {
                type: 'line',
                data: {
                    labels: cumData.map(d => d.date.slice(5)),
                    datasets: [{
                        data: cumData.map(d => d.pnl),
                        borderColor: '#00d9ff',
                        backgroundColor: 'rgba(0,217,255,0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 3,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: '#666' } },
                        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#666' } }
                    }
                }
            });
        }
    </script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    data = get_dashboard_data()
    return render_template_string(HTML_TEMPLATE, data=data)

@app.route("/api/data")
def api_data():
    return jsonify(get_dashboard_data())

if __name__ == "__main__":
    print("=" * 50)
    print("🚀 High Probability Bot v2 - Web Dashboard")
    print("=" * 50)
    print("Open: http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
