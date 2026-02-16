"""
Web dashboard for monitoring the trading bot.

Serves a single-page dashboard with real-time P/L, positions, and trade
history. Data is fetched via JSON API endpoints and auto-refreshed.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from utils.logger import get_logger

log = get_logger("dashboard")

app = FastAPI(title="Kalshi Crypto Trader Dashboard", docs_url=None, redoc_url=None)

# Reference to the TradingBot instance — set by main.py at startup
_bot: Any = None


def set_bot(bot: Any) -> None:
    """Wire the TradingBot instance into the dashboard."""
    global _bot
    _bot = bot


@app.get("/api/status")
async def api_status() -> dict:
    """Overall bot status and P/L summary."""
    if _bot is None or _bot.position_tracker is None:
        return {"status": "initializing"}

    import config

    pt = _bot.position_tracker
    summary = pt.get_portfolio_summary()
    uptime_sec = time.time() - _bot._session_start

    # Crypto prices
    prices: dict[str, float] = {}
    if _bot.price_feed:
        for asset in ["BTC", "ETH", "SOL"]:
            p = _bot.price_feed.get_price(asset)
            if p > 0:
                prices[asset] = round(p, 2)

    return {
        "status": "running" if _bot._running else "stopped",
        "session_id": _bot._session_id,
        "mode": config.TRADING_MODE,
        "paper": config.PAPER_TRADING,
        "uptime_sec": round(uptime_sec),
        "uptime_fmt": _fmt_duration(uptime_sec),
        "initial_balance": round(pt.initial_balance, 2),
        "realized_pnl": round(summary["realized_pnl"], 4),
        "unrealized_pnl": round(summary["unrealized_pnl"], 4),
        "total_pnl": round(summary["total_pnl"], 4),
        "daily_pnl": round(summary["daily_pnl"], 4),
        "total_fees": round(summary["total_fees"], 4),
        "net_exposure": round(summary["net_exposure"], 4),
        "active_positions": summary["active_positions"],
        "trades_today": summary["trades_today"],
        "crypto_prices": prices,
        "watched_markets": len(_bot._watched_tickers),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/positions")
async def api_positions() -> list[dict]:
    """Current open positions."""
    if _bot is None or _bot.position_tracker is None:
        return []

    positions = []
    for ticker, pos in _bot.position_tracker.positions.items():
        if pos.net_contracts == 0:
            continue
        positions.append({
            "ticker": ticker,
            "contracts": pos.net_contracts,
            "avg_entry": round(pos.avg_entry_price, 4),
            "market_price": round(pos.current_market_price, 4),
            "unrealized_pnl": round(pos.unrealized_pnl, 4),
            "realized_pnl": round(pos.realized_pnl, 4),
            "fees": round(pos.fees_paid, 4),
            "bought": pos.total_bought,
            "sold": pos.total_sold,
        })

    positions.sort(key=lambda p: abs(p["unrealized_pnl"]), reverse=True)
    return positions


@app.get("/api/trades")
async def api_trades() -> list[dict]:
    """Recent trade history (last 100)."""
    if _bot is None or _bot.position_tracker is None:
        return []

    trades = []
    for t in reversed(_bot.position_tracker._trade_history[-100:]):
        trades.append({
            "time": t.timestamp,
            "ticker": t.ticker,
            "side": t.side,
            "action": t.action,
            "contracts": t.contracts,
            "price": round(t.price_dollars, 4),
            "fee": round(t.fee_dollars, 4),
            "maker": t.is_maker,
            "strategy": t.strategy,
        })
    return trades


@app.get("/api/strategies")
async def api_strategies() -> dict:
    """Per-strategy breakdown."""
    if _bot is None or _bot.position_tracker is None:
        return {}

    strat_stats: dict[str, dict] = {}
    for t in _bot.position_tracker._trade_history:
        s = t.strategy or "unknown"
        if s not in strat_stats:
            strat_stats[s] = {"trades": 0, "volume": 0.0, "fees": 0.0}
        strat_stats[s]["trades"] += 1
        strat_stats[s]["volume"] += t.price_dollars * t.contracts
        strat_stats[s]["fees"] += t.fee_dollars

    for v in strat_stats.values():
        v["volume"] = round(v["volume"], 4)
        v["fees"] = round(v["fees"], 4)

    return strat_stats


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the main dashboard page."""
    return DASHBOARD_HTML


def _fmt_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Inline HTML dashboard — single file, no template engine needed
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Crypto Trader</title>
<style>
:root {
  --bg: #0f1117;
  --surface: #1a1d27;
  --surface2: #242736;
  --border: #2e3247;
  --text: #e4e6f0;
  --text2: #8b8fa3;
  --green: #22c55e;
  --red: #ef4444;
  --blue: #3b82f6;
  --amber: #f59e0b;
  --purple: #a855f7;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
  font-size: 14px;
  line-height: 1.5;
  padding: 20px;
  max-width: 1400px;
  margin: 0 auto;
}
h1 {
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 4px;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
}
.header-right {
  display: flex;
  gap: 12px;
  align-items: center;
}
.badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.badge-paper {
  background: rgba(168, 85, 247, 0.15);
  color: var(--purple);
  border: 1px solid rgba(168, 85, 247, 0.3);
}
.badge-live {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
  border: 1px solid rgba(239, 68, 68, 0.3);
}
.badge-running {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
  border: 1px solid rgba(34, 197, 94, 0.3);
}
.badge-stopped {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
}
.meta { color: var(--text2); font-size: 12px; }
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}
.card-label {
  font-size: 11px;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 6px;
}
.card-value {
  font-size: 24px;
  font-weight: 700;
}
.card-sub {
  font-size: 11px;
  color: var(--text2);
  margin-top: 4px;
}
.positive { color: var(--green); }
.negative { color: var(--red); }
.neutral { color: var(--text2); }
.section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 20px;
  overflow: hidden;
}
.section-header {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.section-header .count {
  background: var(--surface2);
  padding: 2px 8px;
  border-radius: 8px;
  font-size: 11px;
  color: var(--text2);
}
table {
  width: 100%;
  border-collapse: collapse;
}
th {
  text-align: left;
  padding: 10px 16px;
  font-size: 11px;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
  background: var(--surface2);
}
td {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,0.02); }
.ticker {
  color: var(--blue);
  font-weight: 500;
}
.side-yes { color: var(--green); }
.side-no { color: var(--red); }
.strat-tag {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
  background: var(--surface2);
  color: var(--text2);
}
.prices-bar {
  display: flex;
  gap: 20px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}
.price-chip {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.price-symbol {
  font-weight: 600;
  color: var(--amber);
  font-size: 12px;
}
.price-val { font-size: 15px; font-weight: 600; }
.empty-state {
  padding: 40px;
  text-align: center;
  color: var(--text2);
  font-size: 13px;
}
.refresh-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--green);
  display: inline-block;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
.pnl-chart {
  padding: 16px;
  height: 120px;
  position: relative;
}
.pnl-chart canvas {
  width: 100% !important;
  height: 100% !important;
}
@media (max-width: 768px) {
  body { padding: 12px; }
  .cards { grid-template-columns: repeat(2, 1fr); }
  .header { flex-direction: column; align-items: flex-start; gap: 8px; }
}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>Kalshi Crypto Trader</h1>
    <div class="meta">Session <span id="session-id">—</span> &middot; Uptime <span id="uptime">—</span> &middot; <span id="market-count">0</span> markets watched</div>
  </div>
  <div class="header-right">
    <span class="refresh-dot"></span>
    <span id="mode-badge" class="badge badge-paper">PAPER</span>
    <span id="status-badge" class="badge badge-running">RUNNING</span>
  </div>
</div>

<div class="prices-bar" id="prices-bar"></div>

<div class="cards">
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-value" id="total-pnl">$0.00</div>
    <div class="card-sub" id="pnl-sub">Realized + Unrealized</div>
  </div>
  <div class="card">
    <div class="card-label">Daily P&L</div>
    <div class="card-value" id="daily-pnl">$0.00</div>
    <div class="card-sub">Since midnight UTC</div>
  </div>
  <div class="card">
    <div class="card-label">Realized P&L</div>
    <div class="card-value" id="realized-pnl">$0.00</div>
    <div class="card-sub" id="fees-sub">Fees: $0.00</div>
  </div>
  <div class="card">
    <div class="card-label">Unrealized P&L</div>
    <div class="card-value" id="unrealized-pnl">$0.00</div>
    <div class="card-sub" id="exposure-sub">Exposure: $0.00</div>
  </div>
  <div class="card">
    <div class="card-label">Starting Balance</div>
    <div class="card-value" id="balance">$0.00</div>
    <div class="card-sub" id="balance-sub">—</div>
  </div>
  <div class="card">
    <div class="card-label">Trades Today</div>
    <div class="card-value" id="trades-count">0</div>
    <div class="card-sub" id="positions-sub">0 open positions</div>
  </div>
</div>

<div class="section" id="pnl-section">
  <div class="section-header">P&L History<span class="count" id="pnl-points">0 points</span></div>
  <div class="pnl-chart"><canvas id="pnl-canvas"></canvas></div>
</div>

<div class="section">
  <div class="section-header">Open Positions<span class="count" id="pos-count">0</span></div>
  <div id="positions-table"></div>
</div>

<div class="section">
  <div class="section-header">Recent Trades<span class="count" id="trades-total">0</span></div>
  <div id="trades-table"></div>
</div>

<div class="section">
  <div class="section-header">Strategy Breakdown</div>
  <div id="strategies-table"></div>
</div>

<script>
const pnlHistory = [];
const MAX_PNL_POINTS = 360;

function $(id) { return document.getElementById(id); }

function pnlClass(v) {
  if (v > 0.001) return 'positive';
  if (v < -0.001) return 'negative';
  return 'neutral';
}

function fmt(v) {
  const s = v < 0 ? '-' : v > 0 ? '+' : '';
  return s + '$' + Math.abs(v).toFixed(2);
}

function fmtPrice(v) {
  return v >= 1 ? '$' + v.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : (v * 100).toFixed(0) + 'c';
}

function drawChart() {
  const canvas = $('pnl-canvas');
  if (!canvas || pnlHistory.length < 2) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const vals = pnlHistory.map(p => p.v);
  const mn = Math.min(0, ...vals);
  const mx = Math.max(0, ...vals);
  const range = mx - mn || 1;
  const pad = 4;

  function y(v) { return pad + (H - 2 * pad) * (1 - (v - mn) / range); }

  // zero line
  const zeroY = y(0);
  ctx.strokeStyle = '#2e3247';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, zeroY);
  ctx.lineTo(W, zeroY);
  ctx.stroke();
  ctx.setLineDash([]);

  // fill
  const stepX = W / (pnlHistory.length - 1);
  ctx.beginPath();
  ctx.moveTo(0, zeroY);
  for (let i = 0; i < pnlHistory.length; i++) {
    ctx.lineTo(i * stepX, y(vals[i]));
  }
  ctx.lineTo((pnlHistory.length - 1) * stepX, zeroY);
  ctx.closePath();
  const last = vals[vals.length - 1];
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  if (last >= 0) {
    grad.addColorStop(0, 'rgba(34,197,94,0.2)');
    grad.addColorStop(1, 'rgba(34,197,94,0)');
  } else {
    grad.addColorStop(0, 'rgba(239,68,68,0)');
    grad.addColorStop(1, 'rgba(239,68,68,0.2)');
  }
  ctx.fillStyle = grad;
  ctx.fill();

  // line
  ctx.beginPath();
  for (let i = 0; i < pnlHistory.length; i++) {
    if (i === 0) ctx.moveTo(0, y(vals[i]));
    else ctx.lineTo(i * stepX, y(vals[i]));
  }
  ctx.strokeStyle = last >= 0 ? '#22c55e' : '#ef4444';
  ctx.lineWidth = 2;
  ctx.stroke();

  // dot
  const lx = (pnlHistory.length - 1) * stepX;
  const ly = y(last);
  ctx.beginPath();
  ctx.arc(lx, ly, 4, 0, Math.PI * 2);
  ctx.fillStyle = last >= 0 ? '#22c55e' : '#ef4444';
  ctx.fill();
}

async function refresh() {
  try {
    const [status, positions, trades, strategies] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/positions').then(r => r.json()),
      fetch('/api/trades').then(r => r.json()),
      fetch('/api/strategies').then(r => r.json()),
    ]);

    // Status
    $('session-id').textContent = status.session_id || '—';
    $('uptime').textContent = status.uptime_fmt || '—';
    $('market-count').textContent = status.watched_markets || 0;

    const modeBadge = $('mode-badge');
    if (status.paper) {
      modeBadge.textContent = 'PAPER';
      modeBadge.className = 'badge badge-paper';
    } else {
      modeBadge.textContent = 'LIVE';
      modeBadge.className = 'badge badge-live';
    }

    const statusBadge = $('status-badge');
    statusBadge.textContent = (status.status || 'unknown').toUpperCase();
    statusBadge.className = status.status === 'running' ? 'badge badge-running' : 'badge badge-stopped';

    // Prices
    const pricesBar = $('prices-bar');
    if (status.crypto_prices) {
      pricesBar.innerHTML = Object.entries(status.crypto_prices).map(([sym, price]) =>
        `<div class="price-chip"><span class="price-symbol">${sym}</span><span class="price-val">$${price.toLocaleString()}</span></div>`
      ).join('');
    }

    // Cards
    const tp = status.total_pnl || 0;
    $('total-pnl').textContent = fmt(tp);
    $('total-pnl').className = 'card-value ' + pnlClass(tp);
    $('pnl-sub').textContent = `Realized ${fmt(status.realized_pnl||0)} + Unrealized ${fmt(status.unrealized_pnl||0)}`;

    const dp = status.daily_pnl || 0;
    $('daily-pnl').textContent = fmt(dp);
    $('daily-pnl').className = 'card-value ' + pnlClass(dp);

    const rp = status.realized_pnl || 0;
    $('realized-pnl').textContent = fmt(rp);
    $('realized-pnl').className = 'card-value ' + pnlClass(rp);
    $('fees-sub').textContent = `Fees: $${(status.total_fees||0).toFixed(2)}`;

    const up = status.unrealized_pnl || 0;
    $('unrealized-pnl').textContent = fmt(up);
    $('unrealized-pnl').className = 'card-value ' + pnlClass(up);
    $('exposure-sub').textContent = `Exposure: $${(status.net_exposure||0).toFixed(2)}`;

    const bal = status.initial_balance || 0;
    $('balance').textContent = '$' + bal.toFixed(2);
    const curBal = bal + tp;
    $('balance-sub').textContent = `Current: $${curBal.toFixed(2)} (${tp >= 0 ? '+' : ''}${((tp/Math.max(bal,1))*100).toFixed(1)}%)`;

    $('trades-count').textContent = status.trades_today || 0;
    $('positions-sub').textContent = `${status.active_positions || 0} open positions`;

    // P&L chart
    pnlHistory.push({ t: Date.now(), v: tp });
    if (pnlHistory.length > MAX_PNL_POINTS) pnlHistory.shift();
    $('pnl-points').textContent = pnlHistory.length + ' points';
    drawChart();

    // Positions
    $('pos-count').textContent = positions.length;
    if (positions.length === 0) {
      $('positions-table').innerHTML = '<div class="empty-state">No open positions</div>';
    } else {
      let html = '<table><thead><tr><th>Market</th><th>Contracts</th><th>Entry</th><th>Mark</th><th>Unrealized</th><th>Realized</th><th>Fees</th></tr></thead><tbody>';
      for (const p of positions) {
        const dir = p.contracts > 0 ? 'LONG' : 'SHORT';
        const dirClass = p.contracts > 0 ? 'side-yes' : 'side-no';
        html += `<tr>
          <td class="ticker">${p.ticker}</td>
          <td><span class="${dirClass}">${dir}</span> ${Math.abs(p.contracts)}</td>
          <td>${fmtPrice(p.avg_entry)}</td>
          <td>${fmtPrice(p.market_price)}</td>
          <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)}</td>
          <td class="${pnlClass(p.realized_pnl)}">${fmt(p.realized_pnl)}</td>
          <td>$${p.fees.toFixed(2)}</td>
        </tr>`;
      }
      html += '</tbody></table>';
      $('positions-table').innerHTML = html;
    }

    // Trades
    $('trades-total').textContent = trades.length;
    if (trades.length === 0) {
      $('trades-table').innerHTML = '<div class="empty-state">No trades yet</div>';
    } else {
      let html = '<table><thead><tr><th>Time</th><th>Market</th><th>Side</th><th>Action</th><th>Qty</th><th>Price</th><th>Fee</th><th>Strategy</th></tr></thead><tbody>';
      for (const t of trades.slice(0, 50)) {
        const time = new Date(t.time).toLocaleTimeString();
        const sideClass = t.side === 'yes' ? 'side-yes' : 'side-no';
        html += `<tr>
          <td>${time}</td>
          <td class="ticker">${t.ticker}</td>
          <td class="${sideClass}">${t.side.toUpperCase()}</td>
          <td>${t.action}</td>
          <td>${t.contracts}</td>
          <td>${fmtPrice(t.price)}</td>
          <td>$${t.fee.toFixed(2)}</td>
          <td><span class="strat-tag">${t.strategy || '—'}</span></td>
        </tr>`;
      }
      html += '</tbody></table>';
      $('trades-table').innerHTML = html;
    }

    // Strategies
    const stratEntries = Object.entries(strategies);
    if (stratEntries.length === 0) {
      $('strategies-table').innerHTML = '<div class="empty-state">No strategy data yet</div>';
    } else {
      let html = '<table><thead><tr><th>Strategy</th><th>Trades</th><th>Volume</th><th>Fees</th></tr></thead><tbody>';
      for (const [name, s] of stratEntries) {
        html += `<tr><td>${name}</td><td>${s.trades}</td><td>$${s.volume.toFixed(2)}</td><td>$${s.fees.toFixed(2)}</td></tr>`;
      }
      html += '</tbody></table>';
      $('strategies-table').innerHTML = html;
    }

  } catch (err) {
    console.error('Refresh error:', err);
  }
}

// Initial + auto-refresh every 5 seconds
refresh();
setInterval(refresh, 5000);
window.addEventListener('resize', drawChart);
</script>
</body>
</html>"""
