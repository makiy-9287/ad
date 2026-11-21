class FootprintChart {
  constructor(canvas, infoEl) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.infoEl = infoEl;

    this.data = null;
    this.visibleCount = 32;
    this.rightOffset = 6;
    this.panIndexOffset = 0;

    // CoinGlass-style layer toggles
    this.showFootprint = true;
    this.showHeatmap = true;
    this.showHtfZones = true;
    this.showVp = true;

    this.mouseX = null;
    this.mouseY = null;
    this.isDragging = false;
    this.dragStartX = 0;
    this.dragStartPan = 0;

    this._rafPending = false;
    this._bindEvents();
    this.resize();
  }

  _scheduleDraw() {
    if (this._rafPending) return;
    this._rafPending = true;
    requestAnimationFrame(() => {
      this._rafPending = false;
      this.draw();
    });
  }

  _bindEvents() {
    window.addEventListener("resize", () => this.resize());

    this.canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const step = this.visibleCount > 60 ? 8 : (this.visibleCount > 25 ? 4 : 2);
      const delta = e.deltaY > 0 ? step : -step;
      this.visibleCount = Math.max(8, Math.min(250, this.visibleCount + delta));
      this._scheduleDraw();
    }, { passive: false });

    this.canvas.addEventListener("mousedown", (e) => {
      this.isDragging = true;
      this.dragStartX = e.clientX;
      this.dragStartPan = this.panIndexOffset;
    });

    window.addEventListener("mouseup", () => { this.isDragging = false; });

    this.canvas.addEventListener("mousemove", (e) => {
      const rect = this.canvas.getBoundingClientRect();
      this.mouseX = e.clientX - rect.left;
      this.mouseY = e.clientY - rect.top;
      if (this.isDragging && this.data) {
        const pxPerBar = Math.max(2, (rect.width - 90) / this.visibleCount);
        const shiftBars = Math.round((e.clientX - this.dragStartX) / pxPerBar);
        const maxPan = Math.max(0, (this.data.candles?.length || 0) - 8);
        this.panIndexOffset = Math.max(0, Math.min(maxPan, this.dragStartPan + shiftBars));
      }
      this._scheduleDraw();
    });

    this.canvas.addEventListener("mouseleave", () => {
      this.mouseX = null;
      this.mouseY = null;
      this._scheduleDraw();
    });

    this.canvas.addEventListener("dblclick", () => this.resetView());
  }

  setLayerOptions(opts) {
    if (opts.fp !== undefined) this.showFootprint = !!opts.fp;
    if (opts.heat !== undefined) this.showHeatmap = !!opts.heat;
    if (opts.htf !== undefined) this.showHtfZones = !!opts.htf;
    if (opts.vp !== undefined) this.showVp = !!opts.vp;
    this._scheduleDraw();
  }

  setFootprintZoom() {
    this.visibleCount = 14;
    this.panIndexOffset = 0;
    this._scheduleDraw();
  }

  resetView() {
    this.visibleCount = 56;
    this.panIndexOffset = 0;
    this._scheduleDraw();
  }

  setData(payload, resetPan = false) {
    if (!payload) return;
    if (resetPan) {
      this.panIndexOffset = 0;
    }
    if (payload.full || !this.data || this.data.symbol !== payload.symbol || this.data.tf !== payload.tf) {
      this.data = payload;
    } else {
      const map = new Map((this.data.candles || []).map((c) => [c.t, c]));
      for (const c of payload.candles || []) {
        map.set(c.t, c);
      }
      const merged = Array.from(map.values()).sort((a, b) => a.t - b.t).slice(-260);
      this.data = {
        ...this.data,
        ...payload,
        vp: payload.vp || this.data.vp,
        npocs: payload.npocs || this.data.npocs,
        fvgs: payload.fvgs || this.data.fvgs,
        obs: payload.obs || this.data.obs,
        liq_heatmap: payload.liq_heatmap || this.data.liq_heatmap,
        candles: merged,
      };
    }
    this._scheduleDraw();
  }

  resize() {
    const parent = this.canvas.parentElement;
    if (!parent) return;
    const dpr = window.devicePixelRatio || 1;
    const w = parent.clientWidth;
    const h = parent.clientHeight;
    if (w <= 0 || h <= 0) return;
    this.canvas.width = Math.floor(w * dpr);
    this.canvas.height = Math.floor(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._scheduleDraw();
  }

  fmtPx(px) {
    if (px == null || isNaN(px)) return "--";
    if (px >= 1000) return px.toFixed(1);
    if (px >= 50) return px.toFixed(2);
    if (px >= 5) return px.toFixed(3);
    if (px >= 0.5) return px.toFixed(4);
    return px.toFixed(5);
  }

  fmtVol(v) {
    const av = Math.abs(v);
    const sign = v < 0 ? "-" : "";
    if (av >= 1e6) return sign + (av / 1e6).toFixed(1) + "M";
    if (av >= 1000) return sign + (av / 1000).toFixed(1) + "K";
    if (av >= 10) return sign + av.toFixed(0);
    if (av >= 1) return sign + av.toFixed(1);
    return sign + av.toFixed(2);
  }

  fmtCountdown(closeTime) {
    const diff = Math.max(0, Math.floor((closeTime - Date.now()) / 1000));
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  _heatColor(intensity) {
    if (intensity >= 0.78) return `rgba(253, 231, 37, ${0.14 + intensity * 0.16})`;
    if (intensity >= 0.52) return `rgba(53, 183, 121, ${0.10 + intensity * 0.12})`;
    return `rgba(49, 104, 142, ${0.08 + intensity * 0.10})`;
  }

  draw() {
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const W = this.canvas.width / dpr;
    const H = this.canvas.height / dpr;
    if (W <= 0 || H <= 0) return;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#0b0f17";
    ctx.fillRect(0, 0, W, H);

    if (!this.data || !this.data.candles || this.data.candles.length === 0) {
      ctx.fillStyle = "#8490a6";
      ctx.font = "13px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Loading Dual-Exchange (Binance + Bybit) 250-Bar Orderflow...", W / 2, H / 2);
      return;
    }

    const candlesAll = this.data.candles;
    const totalBars = candlesAll.length;
    const endIdx = Math.max(1, totalBars - this.panIndexOffset);
    const drawSlots = this.visibleCount;
    const rightGap = this.panIndexOffset === 0 ? Math.min(this.rightOffset, Math.floor(drawSlots * 0.25)) : 0;
    const candleSlots = Math.max(4, drawSlots - rightGap);
    const startIdx = Math.max(0, endIdx - candleSlots);
    const bars = candlesAll.slice(startIdx, endIdx);
    if (!bars.length) return;

    const rightAxisW = 84;
    const timeAxisH = 18;
    const vpWidth = 66;
    const plotW = W - rightAxisW;
    const availH = H - timeAxisH;
    const mainH = Math.floor(availH * 0.72);
    const volH = Math.floor(availH * 0.09);
    const deltaH = Math.floor(availH * 0.09);
    const cvdH = availH - mainH - volH - deltaH - 12;

    const barStep = plotW / drawSlots;
    const barW = Math.max(2, barStep * 0.80);
    const showFpCells = this.showFootprint && barStep >= 40;

    let pMin = Infinity;
    let pMax = -Infinity;
    for (const b of bars) {
      if (b.l < pMin) pMin = b.l;
      if (b.h > pMax) pMax = b.h;
    }
    const lastPx = this.data.last_price || bars[bars.length - 1].c;
    if (lastPx > 0) {
      pMin = Math.min(pMin, lastPx);
      pMax = Math.max(pMax, lastPx);
    }
    for (const s of this.data.active_signals || []) {
      pMin = Math.min(pMin, s.stop_loss, s.tp2);
      pMax = Math.max(pMax, s.stop_loss, s.tp2);
    }
    const pad = Math.max((pMax - pMin) * 0.11, pMax * 0.0008);
    pMin -= pad;
    pMax += pad;

    const yOf = (px) => 10 + ((pMax - px) / (pMax - pMin)) * (mainH - 20);
    const pxOf = (y) => pMax - ((y - 10) / (mainH - 20)) * (pMax - pMin);

    // 1. Synthetic Liquidation Heatmap Background Bands
    if (this.showHeatmap && this.data.liq_heatmap) {
      for (const band of this.data.liq_heatmap) {
        if (band.high < pMin || band.price > pMax) continue;
        const yTop = Math.max(0, yOf(band.high));
        const yBot = Math.min(mainH, yOf(band.price));
        const bh = Math.max(3, yBot - yTop);
        ctx.fillStyle = this._heatColor(band.intensity);
        ctx.fillRect(0, yTop, plotW, bh);
        if (band.intensity >= 0.78) {
          ctx.fillStyle = "rgba(253, 231, 37, 0.75)";
          ctx.font = "9.5px monospace";
          ctx.textAlign = "right";
          const lblUsd = band.est_usd >= 1e6 ? `$${(band.est_usd / 1e6).toFixed(1)}M` : `$${(band.est_usd / 1e3).toFixed(0)}K`;
          ctx.fillText(`LIQ ${lblUsd}`, plotW - vpWidth - 6, yTop + bh * 0.7);
        }
      }
    }

    // 2. Grid & Price Axis
    ctx.strokeStyle = "rgba(34, 43, 62, 0.45)";
    ctx.lineWidth = 1;
    ctx.font = "11px monospace";
    ctx.fillStyle = "#8490a6";
    ctx.textAlign = "left";
    const gridRows = 7;
    for (let i = 0; i <= gridRows; i++) {
      const px = pMin + ((pMax - pMin) * i) / gridRows;
      const y = yOf(px);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
      ctx.fillText(this.fmtPx(px), plotW + 6, y + 4);
    }

    // 3. 4H / 1H Unmitigated FVGs & Order Blocks
    if (this.showHtfZones) {
      for (const fvg of this.data.fvgs || []) {
        if (fvg.high < pMin || fvg.low > pMax) continue;
        const yTop = Math.max(0, yOf(fvg.high));
        const yBot = Math.min(mainH, yOf(fvg.low));
        ctx.fillStyle = fvg.side === "BULL" ? "rgba(22, 199, 132, 0.08)" : "rgba(234, 57, 67, 0.08)";
        ctx.fillRect(0, yTop, plotW, Math.max(2, yBot - yTop));
        ctx.fillStyle = fvg.side === "BULL" ? "rgba(22, 199, 132, 0.55)" : "rgba(234, 57, 67, 0.55)";
        ctx.font = "9.5px sans-serif";
        ctx.textAlign = "right";
        ctx.fillText(`${fvg.tf} FVG`, plotW - vpWidth - 6, yTop + 10);
      }
      for (const ob of this.data.obs || []) {
        if (ob.high < pMin || ob.low > pMax) continue;
        const yTop = Math.max(0, yOf(ob.high));
        const yBot = Math.min(mainH, yOf(ob.low));
        ctx.strokeStyle = ob.side === "BULL" ? "rgba(34, 211, 238, 0.35)" : "rgba(245, 183, 0, 0.35)";
        ctx.strokeRect(0, yTop, plotW, Math.max(2, yBot - yTop));
        ctx.fillStyle = ob.side === "BULL" ? "#22d3ee" : "#f5b700";
        ctx.font = "9.5px sans-serif";
        ctx.textAlign = "right";
        ctx.fillText(`${ob.tf} OB`, plotW - vpWidth - 6, yBot - 3);
      }
    }

    // 4. 250-Bar Visible Volume Profile
    if (this.showVp && this.data.vp && this.data.vp.bins && this.data.vp.bins.length) {
      const vp = this.data.vp;
      const maxBinVol = Math.max(...vp.bins.map((b) => b.t), 1e-9);
      for (const bin of vp.bins) {
        if (bin.p < pMin || bin.p > pMax) continue;
        const yTop = yOf(bin.p + (vp.rs || 1));
        const yBot = yOf(bin.p);
        const h = Math.max(1.5, yBot - yTop - 1);
        const w = (bin.t / maxBinVol) * vpWidth;
        const isPoc = Math.abs(bin.p - vp.poc) < (vp.rs || 1) * 0.25;
        ctx.fillStyle = isPoc ? "rgba(245, 183, 0, 0.55)" : "rgba(59, 130, 246, 0.22)";
        ctx.fillRect(plotW - w, yTop, w, h);
      }
      ctx.font = "10px monospace";
      ctx.textAlign = "left";
      for (const [lvl, lbl] of [[vp.vah, "VAH"], [vp.val, "VAL"]]) {
        if (lvl > pMin && lvl < pMax) {
          const y = yOf(lvl);
          ctx.save(); ctx.setLineDash([4, 4]); ctx.strokeStyle = "rgba(132,144,166,0.4)";
          ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke(); ctx.restore();
          ctx.fillStyle = "#8490a6"; ctx.fillText(lbl, plotW - vpWidth - 28, y - 3);
        }
      }
    }

    // 5. Anti-Overlap Liquidity Pool Labels
    const usedLabelYs = [];
    const canPlaceLabel = (y) => {
      for (const uy of usedLabelYs) if (Math.abs(uy - y) < 14) return false;
      usedLabelYs.push(y);
      return true;
    };
    ctx.textAlign = "left";
    ctx.font = "10px sans-serif";

    if (this.data.vwap && this.data.vwap > pMin && this.data.vwap < pMax) {
      const y = yOf(this.data.vwap);
      ctx.strokeStyle = "rgba(245, 183, 0, 0.72)";
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
      if (canPlaceLabel(y)) { ctx.fillStyle = "#f5b700"; ctx.fillText("VWAP", 8, y - 3); }
    }

    for (const np of (this.data.npocs || []).slice(-3)) {
      if (np.price > pMin && np.price < pMax) {
        const y = yOf(np.price);
        ctx.save(); ctx.setLineDash([3, 3]); ctx.strokeStyle = "rgba(34, 211, 238, 0.55)";
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke(); ctx.restore();
        if (canPlaceLabel(y)) { ctx.fillStyle = "#22d3ee"; ctx.fillText("nPOC", 8, y - 3); }
      }
    }

    const drawPool = (p, color) => {
      if (p.price < pMin || p.price > pMax) return;
      const y = yOf(p.price);
      ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = color;
      ctx.lineWidth = p.strength >= 4 ? 1.3 : 0.9;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke(); ctx.restore();
      if (canPlaceLabel(y)) {
        ctx.fillStyle = color;
        const meta = p.meta && (p.tf === "OB" || p.tf === "HEAT") ? ` (${p.meta})` : "";
        ctx.fillText(`${p.name}${meta}`, 8, y - 3);
      }
    };
    for (const p of (this.data.bsl || []).filter((x) => x.strength >= 3).slice(0, 4)) drawPool(p, "rgba(234, 57, 67, 0.72)");
    for (const p of (this.data.ssl || []).filter((x) => x.strength >= 3).slice(0, 4)) drawPool(p, "rgba(22, 199, 132, 0.72)");

    // 6. Active Signal Position Overlay (CMP / LIMIT Entry, SL, TP1, TP2)
    for (const sig of this.data.active_signals || []) {
      const boxStart = Math.max(0, plotW - barStep * 11);
      const boxW = plotW - boxStart;
      const yEntryMid = yOf(sig.entry_mid);
      const ySL = yOf(sig.stop_loss);
      const yTP1 = yOf(sig.tp1);
      const yTP2 = yOf(sig.tp2);
      const yELo = yOf(sig.entry_low);
      const yEHi = yOf(sig.entry_high);

      ctx.fillStyle = "rgba(234, 57, 67, 0.16)";
      ctx.fillRect(boxStart, Math.min(yEntryMid, ySL), boxW, Math.abs(ySL - yEntryMid));
      ctx.fillStyle = "rgba(22, 199, 132, 0.14)";
      ctx.fillRect(boxStart, Math.min(yEntryMid, yTP2), boxW, Math.abs(yTP2 - yEntryMid));
      ctx.fillStyle = "rgba(59, 130, 246, 0.30)";
      ctx.fillRect(boxStart, Math.min(yELo, yEHi), boxW, Math.max(3, Math.abs(yELo - yEHi)));

      ctx.font = "bold 10px monospace";
      ctx.textAlign = "left";
      ctx.fillStyle = "#ea3943"; ctx.fillText(`SL ${this.fmtPx(sig.stop_loss)}`, boxStart + 4, ySL - 3);
      ctx.fillStyle = "#3b82f6"; ctx.fillText(`${sig.entry_type} ${sig.direction} ${this.fmtPx(sig.entry_mid)}`, boxStart + 4, yEntryMid - 3);
      ctx.fillStyle = "#f5b700"; ctx.fillText(`TP1(BE) ${this.fmtPx(sig.tp1)}`, boxStart + 4, yTP1 - 3);
      ctx.fillStyle = "#16c784"; ctx.fillText(`TP2 ${this.fmtPx(sig.tp2)} (1:${sig.rr})`, boxStart + 4, yTP2 + 11);
    }

    // 7. Candles + Dual-Exchange Footprint Cells + Orderflow Signals
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      const cx = (i + 0.5) * barStep;
      const xLeft = cx - barW / 2;
      const isBull = b.c >= b.o;
      const col = isBull ? "#16c784" : "#ea3943";

      if (this.showFootprint) {
        for (const sz of b.stacked || []) {
          const yTop = yOf(sz.high);
          const yBot = yOf(sz.low);
          const sCol = sz.direction === "BULL" ? "rgba(22, 199, 132, 0.14)" : "rgba(234, 57, 67, 0.14)";
          const sBdr = sz.direction === "BULL" ? "rgba(22, 199, 132, 0.65)" : "rgba(234, 57, 67, 0.65)";
          ctx.fillStyle = sCol;
          ctx.fillRect(xLeft, yTop, plotW - xLeft, Math.max(3, yBot - yTop));
          ctx.save(); ctx.setLineDash([3, 3]); ctx.strokeStyle = sBdr;
          ctx.strokeRect(xLeft, yTop, plotW - xLeft, Math.max(3, yBot - yTop)); ctx.restore();
        }
      }

      ctx.strokeStyle = col;
      ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.moveTo(cx, yOf(b.h)); ctx.lineTo(cx, yOf(b.l)); ctx.stroke();

      if (showFpCells && b.rows && b.rows.length) {
        const bodyTop = yOf(Math.max(b.o, b.c));
        const bodyBot = yOf(Math.min(b.o, b.c));
        ctx.fillStyle = col;
        ctx.fillRect(xLeft, bodyTop, 4, Math.max(2, bodyBot - bodyTop));

        const rs = b.rs || 1;
        const isLiveFp = !!b.fp;
        for (const row of b.rows) {
          const yTop = yOf(row.p + rs);
          const yBot = yOf(row.p);
          const rh = Math.max(9, yBot - yTop - 1);
          const cellX = xLeft + 5;
          const cellW = barW - 5;

          if (isLiveFp) {
            ctx.fillStyle = row.bi ? "rgba(22, 199, 132, 0.27)" : (row.si ? "rgba(234, 57, 67, 0.27)" : "rgba(22, 28, 42, 0.90)");
          } else {
            ctx.fillStyle = "rgba(20, 25, 38, 0.55)";
          }
          ctx.fillRect(cellX, yTop, cellW, rh);

          if (row.poc) {
            ctx.strokeStyle = isLiveFp ? "#f5b700" : "rgba(245, 183, 0, 0.45)";
            ctx.lineWidth = 1.2;
            ctx.strokeRect(cellX, yTop, cellW, rh);
          }

          if (rh >= 10) {
            ctx.font = barStep >= 62 ? "9.5px monospace" : "8.5px monospace";
            ctx.textAlign = "right";
            ctx.fillStyle = isLiveFp ? (row.si ? "#ff7b82" : "#d0d6e2") : "#6a758c";
            ctx.fillText(this.fmtVol(row.b), cellX + cellW * 0.46, yTop + rh * 0.74);
            ctx.textAlign = "left";
            ctx.fillStyle = isLiveFp ? (row.bi ? "#3ee8a5" : "#d0d6e2") : "#6a758c";
            ctx.fillText(this.fmtVol(row.a), cellX + cellW * 0.54, yTop + rh * 0.74);
          }
        }
      } else {
        const bodyTop = yOf(Math.max(b.o, b.c));
        const bodyBot = yOf(Math.min(b.o, b.c));
        ctx.fillStyle = col;
        ctx.fillRect(xLeft, bodyTop, barW, Math.max(2, bodyBot - bodyTop));
      }

      if (this.showFootprint) {
        if (b.abs && b.abs_p) {
          const ay = yOf(b.abs_p);
          ctx.beginPath();
          ctx.arc(cx, ay, Math.max(5, Math.min(9, barW * 0.35)), 0, Math.PI * 2);
          ctx.strokeStyle = b.abs === "BULL" ? "#16c784" : "#ea3943";
          ctx.lineWidth = 2;
          ctx.stroke();
        }
        if (b.trap) {
          ctx.font = "bold 9px sans-serif";
          ctx.textAlign = "center";
          ctx.fillStyle = b.trap === "TRAPPED_SHORTS" ? "#16c784" : "#ea3943";
          const ty = b.trap === "TRAPPED_SHORTS" ? yOf(b.l) + 12 : yOf(b.h) - 6;
          ctx.fillText(b.trap === "TRAPPED_SHORTS" ? "TRAP S" : "TRAP L", cx, ty);
        }
        if (b.div || b.sdiv) {
          const lbl = b.sdiv ? (b.sdiv === "SWING_BULL_DIV" ? "SwBullDiv" : "SwBearDiv") : (b.div === "BULL_DIV" ? "BullDiv" : "BearDiv");
          const isBullDiv = lbl.includes("Bull");
          ctx.font = "bold 9px sans-serif";
          ctx.textAlign = "center";
          ctx.fillStyle = isBullDiv ? "#22d3ee" : "#f472b6";
          const dy = isBullDiv ? yOf(b.l) + 22 : yOf(b.h) - 16;
          ctx.fillText(lbl, cx, dy);
        }
      }
    }

    // 8. Live Price Line + Countdown Timer
    const liveBar = bars[bars.length - 1];
    if (lastPx >= pMin && lastPx <= pMax) {
      const ly = yOf(lastPx);
      const isUp = liveBar.c >= liveBar.o;
      ctx.save(); ctx.setLineDash([2, 2]); ctx.strokeStyle = isUp ? "#16c784" : "#ea3943";
      ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(plotW, ly); ctx.stroke(); ctx.restore();

      ctx.fillStyle = isUp ? "#16c784" : "#ea3943";
      ctx.fillRect(plotW + 2, ly - 15, rightAxisW - 4, 30);
      ctx.fillStyle = "#080b10";
      ctx.font = "bold 11px monospace";
      ctx.textAlign = "left";
      ctx.fillText(this.fmtPx(lastPx), plotW + 6, ly - 2);
      ctx.font = "10px monospace";
      ctx.fillText(this.fmtCountdown(liveBar.ct), plotW + 6, ly + 11);
    }

    // 9. Bottom Panes: Volume, Delta, CVD + Time Axis
    const yVolBase = mainH + volH;
    const yDelMid = yVolBase + deltaH * 0.5 + 3;
    const yCvdTop = yVolBase + deltaH + 6;
    const yTimeTop = availH;

    ctx.strokeStyle = "#222b3e";
    ctx.beginPath();
    ctx.moveTo(0, mainH); ctx.lineTo(W, mainH);
    ctx.moveTo(0, yVolBase + 2); ctx.lineTo(W, yVolBase + 2);
    ctx.moveTo(0, yCvdTop); ctx.lineTo(W, yCvdTop);
    ctx.moveTo(0, yTimeTop); ctx.lineTo(W, yTimeTop);
    ctx.stroke();

    const maxV = Math.max(...bars.map((b) => b.v), 1e-9);
    const maxAbsD = Math.max(...bars.map((b) => Math.abs(b.delta)), 1e-9);
    const cvds = bars.map((b) => b.cvd);
    const cvdMin = Math.min(...cvds);
    const cvdMax = Math.max(...cvds);
    const cvdSpan = Math.max(cvdMax - cvdMin, 1e-9);

    ctx.font = "9.5px sans-serif";
    ctx.fillStyle = "#8490a6";
    ctx.textAlign = "left";
    ctx.fillText("VOL", plotW + 6, mainH + 13);
    ctx.fillText("DELTA", plotW + 6, yVolBase + 15);
    ctx.fillText("CVD", plotW + 6, yCvdTop + 13);

    ctx.beginPath();
    const labelEvery = Math.max(1, Math.ceil(bars.length / 7));
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      const cx = (i + 0.5) * barStep;
      const xLeft = cx - barW / 2;

      const vh = (b.v / maxV) * (volH - 5);
      ctx.fillStyle = b.c >= b.o ? "rgba(22,199,132,0.45)" : "rgba(234,57,67,0.45)";
      ctx.fillRect(xLeft, yVolBase - vh, barW, vh);

      const dh = (Math.abs(b.delta) / maxAbsD) * (deltaH * 0.44);
      ctx.fillStyle = b.delta >= 0 ? "#16c784" : "#ea3943";
      if (b.delta >= 0) ctx.fillRect(xLeft, yDelMid - dh, barW, dh);
      else ctx.fillRect(xLeft, yDelMid, barW, dh);

      const cy = yCvdTop + 3 + ((cvdMax - b.cvd) / cvdSpan) * (cvdH - 6);
      if (i === 0) ctx.moveTo(cx, cy);
      else ctx.lineTo(cx, cy);

      if (i % labelEvery === 0) {
        const dt = new Date(b.t);
        const tsLbl = this.data.tf === "1d" || this.data.tf === "4h"
          ? `${dt.getMonth() + 1}/${dt.getDate()}`
          : dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        ctx.fillStyle = "#8490a6";
        ctx.textAlign = "center";
        ctx.font = "10px monospace";
        ctx.fillText(tsLbl, cx, yTimeTop + 12);
      }
    }
    ctx.strokeStyle = "#22d3ee";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // 10. Crosshair & Rich Dual-Exchange Info Bar
    let infoBar = liveBar;
    if (this.mouseX !== null && this.mouseY !== null && this.mouseX < plotW && this.mouseY < yTimeTop) {
      ctx.save(); ctx.setLineDash([3, 3]); ctx.strokeStyle = "rgba(132, 144, 166, 0.5)";
      ctx.beginPath(); ctx.moveTo(this.mouseX, 0); ctx.lineTo(this.mouseX, yTimeTop);
      ctx.moveTo(0, this.mouseY); ctx.lineTo(plotW, this.mouseY); ctx.stroke(); ctx.restore();

      const hIdx = Math.floor(this.mouseX / barStep);
      if (hIdx >= 0 && hIdx < bars.length) infoBar = bars[hIdx];
      if (this.mouseY < mainH) {
        const hoverPx = pxOf(this.mouseY);
        ctx.fillStyle = "#3b82f6";
        ctx.fillRect(plotW + 2, this.mouseY - 9, rightAxisW - 4, 18);
        ctx.fillStyle = "#fff";
        ctx.font = "11px monospace";
        ctx.textAlign = "left";
        ctx.fillText(this.fmtPx(hoverPx), plotW + 6, this.mouseY + 4);
      }
    }

    if (this.infoEl && infoBar) {
      const dt = new Date(infoBar.t).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      const fpTag = infoBar.fp_mode === "LIVE"
        ? `<span style="color:#16c784">[LIVE FP]</span>`
        : `<span style="color:#8490a6">[HIST EST]</span>`;
      const dCol = infoBar.delta >= 0 ? "#16c784" : "#ea3943";
      this.infoEl.innerHTML =
        `<b>${dt}</b> ${fpTag} · O:${this.fmtPx(infoBar.o)} H:${this.fmtPx(infoBar.h)} L:${this.fmtPx(infoBar.l)} C:${this.fmtPx(infoBar.c)} · ` +
        `Delta:<span style="color:${dCol}">${infoBar.delta >= 0 ? "+" : ""}${this.fmtVol(infoBar.delta)}</span> ` +
        `(BN:${this.fmtVol(infoBar.bn_delta || 0)} | BB:${this.fmtVol(infoBar.bb_delta || 0)}) · ` +
        `POC:${this.fmtPx(infoBar.poc)} · CVD:${this.fmtVol(infoBar.cvd)}`;
    }
  }
}