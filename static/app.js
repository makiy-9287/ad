let ws = null;
let lastWsUpdateMs = 0;
let currentSymbol = "BTCUSDT";
let currentTf = "15m";
let subscribeSeq = 0;

const chart = new FootprintChart(
  document.getElementById("fp-canvas"),
  document.getElementById("bar-info")
);

function fmtPx(px) {
  if (px == null || isNaN(px) || Number(px) <= 0) return "--";
  const n = Number(px);
  if (n >= 1000) return n.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  if (n >= 50) return n.toFixed(2);
  if (n >= 5) return n.toFixed(3);
  if (n >= 0.5) return n.toFixed(4);
  return n.toFixed(5);
}

function fmtUsd(v) {
  if (v == null || isNaN(v)) return "--";
  const n = Number(v);
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtOi(qty, px) {
  if (!qty || !px) return "--";
  return fmtUsd(qty * px);
}

function showToast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.innerHTML = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 6000);
}

// Navigation Tabs
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    const viewName = btn.dataset.view;
    const viewEl = document.getElementById(`view-${viewName}`);
    if (viewEl) viewEl.classList.add("active");
    if (viewName === "terminal") chart.resize();
    if (viewName === "levels") loadDailyLevels();
    if (viewName === "signals") loadSignals();
    if (viewName === "settings") loadSettings();
  });
});

// Timeframes
document.querySelectorAll(".tf-btn[data-tf]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tf-btn[data-tf]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentTf = btn.dataset.tf;
    subscribeChart(currentSymbol, currentTf);
  });
});

document.getElementById("btn-fp-zoom").addEventListener("click", () => chart.setFootprintZoom());
document.getElementById("btn-reset-zoom").addEventListener("click", () => chart.resetView());

// CoinGlass Layer Toggles
document.getElementById("chk-fp").addEventListener("change", (e) => chart.setLayerOptions({ fp: e.target.checked }));
document.getElementById("chk-heat").addEventListener("change", (e) => chart.setLayerOptions({ heat: e.target.checked }));
document.getElementById("chk-htf").addEventListener("change", (e) => chart.setLayerOptions({ htf: e.target.checked }));
document.getElementById("chk-vp").addEventListener("change", (e) => chart.setLayerOptions({ vp: e.target.checked }));

function subscribeChart(symbol, tf) {
  currentSymbol = symbol.toUpperCase();
  currentTf = tf;
  const seq = ++subscribeSeq;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "subscribe", symbol: currentSymbol, tf: currentTf }));
  }
  fetch(`/api/chart/${currentSymbol}?tf=${currentTf}&full=true`)
    .then((r) => r.json())
    .then((d) => {
      if (seq === subscribeSeq) renderChartPayload(d, true);
    })
    .catch(() => {});
}

function renderWatchlist(items) {
  const container = document.getElementById("watchlist-container");
  if (!container) return;
  container.innerHTML = "";
  for (const item of items || []) {
    const div = document.createElement("div");
    div.className = `coin-item ${item.symbol === currentSymbol ? "selected" : ""}`;
    const chgCol = item.change_24h >= 0 ? "green" : "red";
    const t2Badge = item.tier2 ? `<span class="t2-pill" title="Tier-2 Dual-Exchange Orderflow Active">T2</span>` : "";
    div.innerHTML = `
      <div class="coin-row-top">
        <span>${item.symbol.replace("USDT", "")}<small class="muted">/USDT</small>${t2Badge}</span>
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
  document.getElementById("st-active").textContent = stats.active_count ?? 0;
  document.getElementById("st-total").textContent = stats.total_closed ?? 0;
  document.getElementById("st-wr").textContent = `${stats.win_rate ?? 0}%`;

  const tr = Number(stats.today_r ?? 0);
  const elTr = document.getElementById("st-today-r");
  elTr.textContent = `${tr >= 0 ? "+" : ""}${tr.toFixed(2)}R`;
  elTr.className = tr >= 0 ? "green" : "red";

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
  document.getElementById("hdr-bb-price").textContent = data.bybit_price > 0 ? `BB: ${fmtPx(data.bybit_price)}` : "BB: Sync";

  const chgEl = document.getElementById("hdr-chg");
  chgEl.textContent = `${data.change_24h >= 0 ? "+" : ""}${data.change_24h}%`;
  chgEl.className = `hdr-chg ${data.change_24h >= 0 ? "green" : "red"}`;

  const biasEl = document.getElementById("hdr-bias");
  biasEl.textContent = data.htf_bias_str || `4H:${data.bias_4h}`;
  biasEl.className = `tag ${data.aligned_bias === "BULLISH" ? "green" : (data.aligned_bias === "BEARISH" ? "red" : "gold")}`;

  const t2El = document.getElementById("hdr-t2");
  if (data.tier2_active) {
    t2El.textContent = "Tier-2 BN+BB";
    t2El.className = "tag cyan";
  } else {
    t2El.textContent = "Tier-1 Watch";
    t2El.className = "tag muted";
  }

  document.getElementById("hdr-oi").textContent = `OI: ${fmtOi(data.combined_oi, data.last_price)}`;
  document.getElementById("hdr-atr").textContent = `15m ATR: ${fmtPx(data.atr_15m)}`;

  const stEl = document.getElementById("hdr-status");
  stEl.textContent = data.scanner_note || data.scanner_state;
  stEl.className = `tag state-${data.scanner_state}`;

  const sigBar = document.getElementById("active-signal-bar");
  if (data.active_signals && data.active_signals.length > 0) {
    const s = data.active_signals[0];
    const col = s.direction === "LONG" ? "green" : "red";
    sigBar.innerHTML = `
      <div>
        <b class="${col}">${s.entry_type} ${s.direction} ${s.symbol}</b> <code>[${s.timeframe}]</code> ·
        Entry: <code>${fmtPx(s.entry_mid)}</code> (Zone ${fmtPx(s.entry_low)}–${fmtPx(s.entry_high)}) ·
        SL: <code class="red">${fmtPx(s.stop_loss)}</code> ·
        TP1: <code class="gold">${fmtPx(s.tp1)}</code> ·
        TP2: <code class="green">${fmtPx(s.tp2)}</code> ·
        <b>RR 1:${s.rr}</b> · Status: <b class="gold">${s.status}</b>
      </div>
      <div class="muted">${s.reason}</div>
    `;
  } else {
    sigBar.innerHTML = `<span class="muted">${data.scanner_note || "Monitoring 1D/4H/1H liquidity pools & Dual-Exchange orderflow..."}</span>`;
  }

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

  const imbEl = document.getElementById("ob-imb");
  imbEl.textContent = `${data.ob_imbalance_pct >= 0 ? "+" : ""}${data.ob_imbalance_pct}%`;
  imbEl.className = `ob-imb ${data.ob_imbalance_pct >= 0 ? "green" : "red"}`;

  const askWallPrices = new Set((data.ask_walls || []).map((w) => w.price));
  const bidWallPrices = new Set((data.bid_walls || []).map((w) => w.price));

  const asksEl = document.getElementById("ob-asks");
  asksEl.innerHTML = (data.asks || []).slice(0, 7).reverse().map(([p, q]) => {
    const isWall = askWallPrices.has(p);
    return `<div class="ob-row ${isWall ? "wall" : ""}"><span class="red">${fmtPx(p)}</span><span>${fmtUsd(p * q)}</span></div>`;
  }).join("");

  document.getElementById("ob-spread").textContent = fmtPx(data.last_price);

  const bidsEl = document.getElementById("ob-bids");
  bidsEl.innerHTML = (data.bids || []).slice(0, 7).map(([p, q]) => {
    const isWall = bidWallPrices.has(p);
    return `<div class="ob-row ${isWall ? "wall" : ""}"><span class="green">${fmtPx(p)}</span><span>${fmtUsd(p * q)}</span></div>`;
  }).join("");

  const liqTape = document.getElementById("recent-liqs-list");
  const rLiqs = (data.recent_liqs || []).slice().reverse();
  if (rLiqs.length === 0) {
    liqTape.innerHTML = `<div class="muted" style="font-size:10.5px;">Listening for Binance+Bybit liquidations...</div>`;
  } else {
    liqTape.innerHTML = rLiqs.map((lq) => {
      const cls = lq.squeeze ? "green" : "red";
      const lbl = lq.squeeze ? "SHORT LIQ" : "LONG LIQ";
      return `<div class="liq-tape-row"><span>${lq.ex.slice(0, 2)} · <b class="${cls}">${lbl}</b></span><span>${fmtPx(lq.price)} (${fmtUsd(lq.usd)})</span></div>`;
    }).join("");
  }

  const clk = document.getElementById("live-clock");
  if (clk) clk.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  const dot = document.getElementById("conn-dot");

  ws.onopen = () => {
    dot.classList.add("online");
    lastWsUpdateMs = Date.now();
    ws.send(JSON.stringify({ action: "subscribe", symbol: currentSymbol, tf: currentTf }));
  };
  ws.onclose = () => {
    dot.classList.remove("online");
    setTimeout(connectWS, 2000);
  };
  ws.onmessage = (evt) => {
    lastWsUpdateMs = Date.now();
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
      showToast(`🔥 <b>${s.entry_type} ${s.direction} ${s.symbol}</b><br/>Entry: ${fmtPx(s.entry_mid)} · SL: ${fmtPx(s.stop_loss)} · RR 1:${s.rr}`);
      if (msg.stats) renderStats(msg.stats);
    } else if (msg.type === "signals_updated") {
      if (msg.stats) renderStats(msg.stats);
    } else if (msg.type === "daily_rollover") {
      showToast(`📅 Daily UTC Rollover (${msg.utc_day}) — 20-Coin Liquidity Maps Updated!`);
    }
  };
}

setInterval(async () => {
  if (Date.now() - lastWsUpdateMs < 1800) return;
  try {
    const res = await fetch(`/api/live/${currentSymbol}?tf=${currentTf}`);
    if (!res.ok) return;
    const data = await res.json();
    renderChartPayload(data.chart, false);
    if (data.watchlist) renderWatchlist(data.watchlist);
    if (data.stats) renderStats(data.stats);
  } catch (_) {}
}, 1000);

function biasClass(b) {
  if (b === "BULLISH") return "green";
  if (b === "BEARISH") return "red";
  return "gold";
}

async function loadDailyLevels() {
  const res = await fetch("/api/daily-levels");
  const data = await res.json();
  const tbody = document.getElementById("levels-tbody");
  tbody.innerHTML = "";
  for (const row of data.levels || []) {
    const bsl = (row.levels?.bsl || [])[0];
    const ssl = (row.levels?.ssl || [])[0];
    const tr = document.createElement("tr");
    tr.className = "clickable-row";
    tr.innerHTML = `
      <td><b>${row.symbol}</b></td>
      <td class="muted">${row.utc_day}</td>
      <td class="${biasClass(row.bias_1d)}"><b>${row.bias_1d}</b></td>
      <td class="${biasClass(row.bias_4h)}"><b>${row.bias_4h}</b></td>
      <td class="${biasClass(row.bias_1h)}"><b>${row.bias_1h}</b></td>
      <td>${fmtPx(row.pwh)}</td>
      <td class="red">${fmtPx(row.pdh)}</td>
      <td class="gold">${fmtPx(row.vwap)}</td>
      <td class="green">${fmtPx(row.pdl)}</td>
      <td>${fmtPx(row.pwl)}</td>
      <td class="red">${bsl ? `${bsl.name} (${fmtPx(bsl.price)})` : "--"}</td>
      <td class="green">${ssl ? `${ssl.name} (${fmtPx(ssl.price)})` : "--"}</td>
    `;
    tr.addEventListener("click", () => {
      document.querySelector('.nav-btn[data-view="terminal"]').click();
      subscribeChart(row.symbol, currentTf);
    });
    tbody.appendChild(tr);
  }
}

async function loadSignals() {
  const res = await fetch("/api/signals?limit=80");
  const data = await res.json();
  renderStats(data.stats);
  const tbody = document.getElementById("signals-tbody");
  tbody.innerHTML = (data.signals || []).map((s) => {
    const dt = new Date(s.created_at).toLocaleString();
    const sideCol = s.direction === "LONG" ? "green" : "red";
    const typeCol = s.entry_type === "CMP" ? "cyan" : "gold";
    const rCol = s.pnl_r > 0 ? "green" : (s.pnl_r < 0 ? "red" : "muted");
    return `
      <tr>
        <td>${dt}</td>
        <td><b>${s.symbol}</b></td>
        <td>${s.timeframe}</td>
        <td class="${typeCol}"><b>${s.entry_type}</b></td>
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
  document.getElementById("set-min-rr").value = s.min_rr ?? 2.0;
  document.getElementById("set-min-conf").value = s.min_confidence ?? 72;
  document.getElementById("set-min-sl").value = s.min_sl_pct ?? 0.55;
  document.getElementById("set-max-sl").value = s.max_sl_pct ?? 4.50;
  document.getElementById("set-atr-mult").value = s.sl_atr_mult ?? 1.25;
  document.getElementById("set-sl-buf").value = s.sl_buffer_pct ?? 0.12;
  document.getElementById("set-imb-ratio").value = s.imbalance_ratio ?? 3.0;
  document.getElementById("set-cmp-pct").value = s.cmp_threshold_pct ?? 0.22;
  document.getElementById("set-prox-pct").value = s.level_proximity_pct ?? 0.65;
  document.getElementById("set-max-t2").value = s.max_tier2_streams ?? 8;
  document.getElementById("set-cooldown").value = s.signal_cooldown_min ?? 90;
  document.getElementById("set-watchlist").value = (s.watchlist || []).join(", ");
}

document.getElementById("btn-refresh-levels").addEventListener("click", loadDailyLevels);
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
      min_rr: parseFloat(document.getElementById("set-min-rr").value),
      min_confidence: parseInt(document.getElementById("set-min-conf").value, 10),
      min_sl_pct: parseFloat(document.getElementById("set-min-sl").value),
      max_sl_pct: parseFloat(document.getElementById("set-max-sl").value),
      sl_atr_mult: parseFloat(document.getElementById("set-atr-mult").value),
      sl_buffer_pct: parseFloat(document.getElementById("set-sl-buf").value),
      imbalance_ratio: parseFloat(document.getElementById("set-imb-ratio").value),
      cmp_threshold_pct: parseFloat(document.getElementById("set-cmp-pct").value),
      level_proximity_pct: parseFloat(document.getElementById("set-prox-pct").value),
      max_tier2_streams: parseInt(document.getElementById("set-max-t2").value, 10),
      signal_cooldown_min: parseInt(document.getElementById("set-cooldown").value, 10),
      watchlist: watchlistRaw,
    }),
  });
  const data = await res.json();
  if (res.ok && data.ok) {
    fb.textContent = "All Day-Mode & Telegram settings saved & reloaded!";
    fb.className = "feedback green";
  } else {
    fb.textContent = data.detail || "Failed to save settings";
    fb.className = "feedback red";
  }
});

subscribeChart(currentSymbol, currentTf);
connectWS();