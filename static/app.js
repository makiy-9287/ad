let ws = null;
let currentSymbol = "BTCUSDT";
let currentTf = "5m";
const chart = new FootprintChart(
  document.getElementById("fp-canvas"),
  document.getElementById("bar-info")
);

function fmtPx(px) {
  if (px == null || isNaN(px)) return "--";
  if (px >= 1000) return Number(px).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  if (px >= 10) return Number(px).toFixed(3);
  if (px >= 1) return Number(px).toFixed(4);
  return Number(px).toFixed(5);
}

function fmtUsd(v) {
  if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.innerHTML = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 5500);
}

// Navigation tabs
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    const viewId = `view-${btn.dataset.view}`;
    document.getElementById(viewId).classList.add("active");
    if (btn.dataset.view === "terminal") chart.resize();
    if (btn.dataset.view === "signals") loadSignals();
    if (btn.dataset.view === "settings") loadSettings();
  });
});

// Timeframe buttons
document.querySelectorAll(".tf-btn[data-tf]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tf-btn[data-tf]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentTf = btn.dataset.tf;
    subscribeChart(currentSymbol, currentTf);
  });
});
document.getElementById("btn-reset-zoom").addEventListener("click", () => chart.resetView());

function subscribeChart(symbol, tf) {
  currentSymbol = symbol;
  currentTf = tf;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "subscribe", symbol, tf }));
  } else {
    fetch(`/api/chart/${symbol}?tf=${tf}`)
      .then((r) => r.json())
      .then((d) => renderChartPayload(d, true));
  }
}

function renderWatchlist(items) {
  const container = document.getElementById("watchlist-container");
  container.innerHTML = "";
  for (const item of items || []) {
    const div = document.createElement("div");
    div.className = `coin-item ${item.symbol === currentSymbol ? "selected" : ""}`;
    const chgCol = item.change_24h >= 0 ? "green" : "red";
    div.innerHTML = `
      <div class="coin-row-top">
        <span>${item.symbol.replace("USDT", "")}<small class="muted">/USDT</small></span>
        <span>${fmtPx(item.price)}</span>
      </div>
      <div class="coin-row-bot">
        <span class="${chgCol}">${item.change_24h >= 0 ? "+" : ""}${item.change_24h}% · ${item.bias}</span>
        <span class="state-badge state-${item.state}">${item.state.replace("_", " ")}</span>
      </div>
    `;
    div.addEventListener("click", () => subscribeChart(item.symbol, currentTf));
    container.appendChild(div);
  }
}

function renderStats(stats) {
  if (!stats) return;
  document.getElementById("st-total").textContent = stats.total_closed ?? 0;
  document.getElementById("st-wr").textContent = `${stats.win_rate ?? 0}%`;
  const r = Number(stats.net_r ?? 0);
  const elR = document.getElementById("st-r");
  elR.textContent = `${r >= 0 ? "+" : ""}${r.toFixed(2)}R`;
  elR.className = r >= 0 ? "green" : "red";
}

function renderChartPayload(data, resetPan = false) {
  if (!data || data.symbol !== currentSymbol) return;
  chart.setData(data, resetPan);

  document.getElementById("hdr-symbol").textContent = data.symbol;
  document.getElementById("hdr-price").textContent = fmtPx(data.last_price);
  const chgEl = document.getElementById("hdr-chg");
  chgEl.textContent = `${data.change_24h >= 0 ? "+" : ""}${data.change_24h}%`;
  chgEl.className = `hdr-chg ${data.change_24h >= 0 ? "green" : "red"}`;

  const biasEl = document.getElementById("hdr-bias");
  biasEl.textContent = `1H: ${data.bias_1h} · 4H: ${data.bias_4h}`;
  biasEl.className = `tag ${data.bias_1h === "BULLISH" ? "green" : (data.bias_1h === "BEARISH" ? "red" : "gold")}`;

  const stEl = document.getElementById("hdr-status");
  stEl.textContent = data.scanner_note || data.scanner_state;
  stEl.className = `tag state-${data.scanner_state}`;

  // Active signal bar
  const sigBar = document.getElementById("active-signal-bar");
  if (data.active_signals && data.active_signals.length > 0) {
    const s = data.active_signals[0];
    const col = s.direction === "LONG" ? "green" : "red";
    sigBar.innerHTML = `
      <div>
        <b class="${col}">${s.direction} ${s.symbol} [${s.mode}]</b> ·
        Entry: <code>${fmtPx(s.entry_low)}–${fmtPx(s.entry_high)}</code> ·
        SL: <code class="red">${fmtPx(s.stop_loss)}</code> ·
        TP1: <code class="gold">${fmtPx(s.tp1)}</code> ·
        TP2: <code class="green">${fmtPx(s.tp2)}</code> ·
        <b>RR 1:${s.rr}</b> · Status: <b class="gold">${s.status}</b>
      </div>
      <div class="muted">${s.reason}</div>
    `;
  } else {
    sigBar.innerHTML = `<span class="muted">${data.scanner_note || "Monitoring liquidity pools & orderflow confirmations..."}</span>`;
  }

  // Liquidity pools right panel
  const liqEl = document.getElementById("liq-pools-list");
  liqEl.innerHTML = "";
  const bslTop = (data.bsl || []).slice(0, 4).reverse();
  const sslTop = (data.ssl || []).slice(0, 4);
  for (const p of bslTop) {
    liqEl.innerHTML += `<div class="liq-row liq-bsl"><span>${p.name} ${p.meta ? "· " + p.meta : ""}</span><b class="red">${fmtPx(p.price)}</b></div>`;
  }
  for (const p of sslTop) {
    liqEl.innerHTML += `<div class="liq-row liq-ssl"><span>${p.name} ${p.meta ? "· " + p.meta : ""}</span><b class="green">${fmtPx(p.price)}</b></div>`;
  }

  // Orderbook depth
  const imbEl = document.getElementById("ob-imb");
  imbEl.textContent = `${data.ob_imbalance_pct >= 0 ? "+" : ""}${data.ob_imbalance_pct}%`;
  imbEl.className = `ob-imb ${data.ob_imbalance_pct >= 0 ? "green" : "red"}`;

  const askWallPrices = new Set((data.ask_walls || []).map((w) => w.price));
  const bidWallPrices = new Set((data.bid_walls || []).map((w) => w.price));

  const asksEl = document.getElementById("ob-asks");
  asksEl.innerHTML = (data.asks || []).slice(0, 8).reverse().map(([p, q]) => {
    const isWall = askWallPrices.has(p);
    return `<div class="ob-row ${isWall ? "wall" : ""}"><span class="red">${fmtPx(p)}</span><span>${fmtUsd(p * q)}</span></div>`;
  }).join("");

  document.getElementById("ob-spread").textContent = fmtPx(data.last_price);

  const bidsEl = document.getElementById("ob-bids");
  bidsEl.innerHTML = (data.bids || []).slice(0, 8).map(([p, q]) => {
    const isWall = bidWallPrices.has(p);
    return `<div class="ob-row ${isWall ? "wall" : ""}"><span class="green">${fmtPx(p)}</span><span>${fmtUsd(p * q)}</span></div>`;
  }).join("");
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  const dot = document.getElementById("conn-dot");

  ws.onopen = () => {
    dot.classList.add("online");
    ws.send(JSON.stringify({ action: "subscribe", symbol: currentSymbol, tf: currentTf }));
  };
  ws.onclose = () => {
    dot.classList.remove("online");
    setTimeout(connectWS, 2500);
  };
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === "init" || msg.type === "chart_full") {
      renderChartPayload(msg.chart, true);
      if (msg.watchlist) renderWatchlist(msg.watchlist);
      if (msg.stats) renderStats(msg.stats);
    } else if (msg.type === "live_update") {
      renderChartPayload(msg.chart, false);
      if (msg.watchlist) renderWatchlist(msg.watchlist);
      if (msg.stats) renderStats(msg.stats);
    } else if (msg.type === "signal_alert") {
      const s = msg.signal;
      showToast(`🔥 <b>${s.direction} ${s.symbol}</b> [${s.mode}]<br/>Entry: ${fmtPx(s.entry_mid)} · RR 1:${s.rr}`);
      if (msg.stats) renderStats(msg.stats);
    } else if (msg.type === "signals_updated") {
      if (msg.stats) renderStats(msg.stats);
    }
  };
}

async function loadSignals() {
  const res = await fetch("/api/signals?limit=60");
  const data = await res.json();
  renderStats(data.stats);
  const tbody = document.getElementById("signals-tbody");
  tbody.innerHTML = (data.signals || []).map((s) => {
    const dt = new Date(s.created_at).toLocaleString();
    const sideCol = s.direction === "LONG" ? "green" : "red";
    const rCol = s.pnl_r > 0 ? "green" : (s.pnl_r < 0 ? "red" : "muted");
    return `
      <tr>
        <td>${dt}</td>
        <td><b>${s.symbol}</b></td>
        <td>${s.mode} (${s.timeframe})</td>
        <td class="${sideCol}"><b>${s.direction}</b></td>
        <td>${fmtPx(s.entry_low)}–${fmtPx(s.entry_high)}</td>
        <td class="red">${fmtPx(s.stop_loss)}</td>
        <td class="gold">${fmtPx(s.tp1)}</td>
        <td class="green">${fmtPx(s.tp2)}</td>
        <td>1:${s.rr}</td>
        <td>${s.confidence}</td>
        <td><b>${s.status}</b></td>
        <td class="${rCol}">${s.pnl_r ? (s.pnl_r > 0 ? "+" : "") + s.pnl_r + "R" : "--"}</td>
        <td class="muted">${s.reason}</td>
      </tr>
    `;
  }).join("");
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  const s = await res.json();
  document.getElementById("set-tg-token").value = s.telegram_bot_token || "";
  document.getElementById("set-tg-chat").value = s.telegram_chat_id || "";
  document.getElementById("set-tg-enabled").checked = !!s.telegram_enabled;
  document.getElementById("set-scalp-en").checked = !!s.scalping_enabled;
  document.getElementById("set-day-en").checked = !!s.day_enabled;
  document.getElementById("set-min-rr").value = s.min_rr ?? 2.0;
  document.getElementById("set-imb-ratio").value = s.imbalance_ratio ?? 3.0;
  document.getElementById("set-sl-buf").value = s.sl_buffer_pct ?? 0.08;
  document.getElementById("set-cooldown").value = s.signal_cooldown_min ?? 45;
  document.getElementById("set-watchlist").value = (s.watchlist || []).join(", ");
}

document.getElementById("btn-refresh-signals").addEventListener("click", loadSignals);

document.getElementById("btn-test-tg").addEventListener("click", async () => {
  const fb = document.getElementById("tg-feedback");
  fb.textContent = "Sending test alert...";
  fb.className = "feedback muted";
  const res = await fetch("/api/telegram/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      telegram_bot_token: document.getElementById("set-tg-token").value,
      telegram_chat_id: document.getElementById("set-tg-chat").value,
    }),
  });
  const data = await res.json();
  fb.textContent = data.message;
  fb.className = `feedback ${data.ok ? "green" : "red"}`;
});

document.getElementById("btn-save-settings").addEventListener("click", async () => {
  const fb = document.getElementById("tg-feedback");
  const watchlistRaw = document.getElementById("set-watchlist").value.split(",").map((x) => x.trim()).filter(Boolean);
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      telegram_bot_token: document.getElementById("set-tg-token").value,
      telegram_chat_id: document.getElementById("set-tg-chat").value,
      telegram_enabled: document.getElementById("set-tg-enabled").checked,
      scalping_enabled: document.getElementById("set-scalp-en").checked,
      day_enabled: document.getElementById("set-day-en").checked,
      min_rr: parseFloat(document.getElementById("set-min-rr").value),
      imbalance_ratio: parseFloat(document.getElementById("set-imb-ratio").value),
      sl_buffer_pct: parseFloat(document.getElementById("set-sl-buf").value),
      signal_cooldown_min: parseInt(document.getElementById("set-cooldown").value, 10),
      watchlist: watchlistRaw,
    }),
  });
  const data = await res.json();
  if (res.ok && data.ok) {
    fb.textContent = "Settings saved successfully!";
    fb.className = "feedback green";
  } else {
    fb.textContent = data.detail || "Failed to save settings";
    fb.className = "feedback red";
  }
});

connectWS();