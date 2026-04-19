#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    web_dashboard.py
# Description: Lightweight HTTP server serving live Sigenergy battery dashboard.
#              Runs on port 8179. Exposes / (HTML) and /api/status (JSON).
#              Started from plugin.startup(), stopped on plugin.shutdown().
# Author:      CliveS & Claude Sonnet 4.6
# Date:        19-04-2026
# Version:     1.0

import http.server
import json
import logging
import socketserver
import threading

DASHBOARD_PORT = 8179

# ============================================================
# Embedded self-contained dashboard HTML
# ============================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sigenergy Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080d14;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#0f1724;border-bottom:1px solid #1e2d3d}
header h1{font-size:16px;font-weight:600;color:#7dd3fc;letter-spacing:.3px}
.hdr-right{text-align:right;line-height:1.6}
.hdr-right .ts{font-size:13px;color:#94a3b8}
.hdr-right .cdwn{font-size:11px;color:#4b5563}
#alert-bar{display:none;padding:8px 16px;font-size:13px;font-weight:500;background:#7c2d12;border-bottom:1px solid #991b1b;color:#fca5a5}
#alert-bar.warn{background:#713f12;border-color:#92400e;color:#fcd34d}
.main{padding:12px;display:grid;gap:12px;grid-template-columns:1fr 1fr;grid-template-rows:auto}
.card{background:#0f1724;border:1px solid #1e2d3d;border-radius:10px;padding:14px}
.card h2{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px}
/* --- flow card --- */
.flow-card{grid-column:1;grid-row:1}
#flow-svg{width:100%;height:auto}
/* --- right panel --- */
.right-panel{grid-column:2;grid-row:1;display:flex;flex-direction:column;gap:10px}
.soc-wrap{display:flex;align-items:center;gap:14px}
.soc-ring-wrap{flex-shrink:0;width:90px;height:90px}
.soc-ring-wrap svg{width:100%;height:100%}
.soc-info .soc-pct{font-size:28px;font-weight:700;color:#34d399}
.soc-info .soc-label{font-size:11px;color:#64748b;margin-top:2px}
.soc-info .bat-pw{font-size:13px;margin-top:6px}
.stat-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.stat-box{background:#0a1020;border:1px solid #1e2d3d;border-radius:8px;padding:10px;text-align:center}
.stat-box .sb-label{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.6px}
.stat-box .sb-val{font-size:17px;font-weight:700;margin:4px 0 2px}
.stat-box .sb-sub{font-size:10px;color:#64748b}
/* --- forecast --- */
.forecast-card{grid-column:1 / -1}
.fc-meta{display:flex;gap:20px;margin-bottom:8px;font-size:12px;color:#94a3b8}
.fc-meta strong{color:#e2e8f0}
#fc-svg{width:100%;height:auto}
/* --- bottom row --- */
.bottom-row{grid-column:1 / -1;display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.dl{display:flex;flex-direction:column;gap:6px}
.dl-item{display:flex;justify-content:space-between;align-items:baseline;font-size:13px}
.dl-item .dk{color:#94a3b8}
.dl-item .dv{font-weight:600}
.action-badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;margin-bottom:8px;text-transform:capitalize}
.action-self{background:#14291b;color:#4ade80;border:1px solid #166534}
.action-overflow{background:#1c2a05;color:#a3e635;border:1px solid #4d7c0f}
.action-export{background:#12282e;color:#22d3ee;border:1px solid #0e7490}
.action-import{background:#2c1515;color:#f87171;border:1px solid #991b1b}
.action-schedule{background:#1e1a08;color:#fbbf24;border:1px solid #92400e}
.action-unknown{background:#1f2937;color:#9ca3af;border:1px solid #374151}
.reason{font-size:11px;color:#64748b;line-height:1.5;margin-top:4px;word-break:break-word}
.dawn-ok{color:#34d399}
.dawn-warn{color:#f87171}
.tariff-rate{font-size:24px;font-weight:700;color:#fbbf24}
.tariff-sub{font-size:11px;color:#64748b;margin-top:2px}
.tariff-tmrw{margin-top:8px;font-size:13px;color:#94a3b8}
.tariff-tmrw span{color:#e2e8f0;font-weight:600}
.self-suff-bar{height:6px;background:#1e2d3d;border-radius:3px;margin-top:4px;overflow:hidden}
.self-suff-fill{height:100%;background:#34d399;border-radius:3px;transition:width .4s}
/* --- colors --- */
.solar{color:#fbbf24}
.bat-charge{color:#34d399}
.bat-discharge{color:#a78bfa}
.grid-import{color:#f87171}
.grid-export{color:#22d3ee}
.home-load{color:#a78bfa}
.muted{color:#64748b}
/* --- SVG flow animations --- */
@keyframes flow-fwd{to{stroke-dashoffset:-12}}
@keyframes flow-rev{to{stroke-dashoffset:12}}
.flow-fwd{animation:flow-fwd .7s linear infinite}
.flow-rev{animation:flow-rev .7s linear infinite}
/* --- responsive --- */
@media(max-width:680px){
  .main{grid-template-columns:1fr}
  .flow-card,.right-panel,.forecast-card{grid-column:1}
  .bottom-row{grid-template-columns:1fr}
}
</style>
</head>
<body>
<header>
  <h1>&#9889; Sigenergy Battery Monitor</h1>
  <div class="hdr-right">
    <div class="ts">Updated: <span id="ts">&#8212;</span></div>
    <div class="cdwn">Next refresh in <span id="cdwn">30</span>s</div>
  </div>
</header>

<div id="alert-bar"></div>

<main class="main">

  <!-- Power Flow -->
  <section class="card flow-card">
    <h2>Live Power Flow</h2>
    <svg id="flow-svg" viewBox="0 0 520 295" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <filter id="glow"><feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      </defs>
      <!-- track lines (always visible, dim) -->
      <line x1="260" y1="62" x2="260" y2="112" stroke="#1e2d3d" stroke-width="6" stroke-linecap="round"/>
      <line x1="160" y1="148" x2="207" y2="148" stroke="#1e2d3d" stroke-width="6" stroke-linecap="round"/>
      <line x1="313" y1="148" x2="360" y2="148" stroke="#1e2d3d" stroke-width="6" stroke-linecap="round"/>
      <line x1="260" y1="184" x2="260" y2="228" stroke="#1e2d3d" stroke-width="6" stroke-linecap="round"/>
      <!-- animated flow lines -->
      <line id="fl-solar" x1="260" y1="62" x2="260" y2="112" stroke="#fbbf24" stroke-width="4" stroke-linecap="round" stroke-dasharray="8 4" stroke-dashoffset="0" opacity="0"/>
      <line id="fl-bat"   x1="160" y1="148" x2="207" y2="148" stroke="#34d399" stroke-width="4" stroke-linecap="round" stroke-dasharray="8 4" stroke-dashoffset="0" opacity="0"/>
      <line id="fl-home"  x1="313" y1="148" x2="360" y2="148" stroke="#a78bfa" stroke-width="4" stroke-linecap="round" stroke-dasharray="8 4" stroke-dashoffset="0" opacity="0"/>
      <line id="fl-grid"  x1="260" y1="184" x2="260" y2="228" stroke="#22d3ee" stroke-width="4" stroke-linecap="round" stroke-dasharray="8 4" stroke-dashoffset="0" opacity="0"/>
      <!-- hub circle -->
      <circle cx="260" cy="148" r="18" fill="#0f1724" stroke="#334155" stroke-width="2"/>
      <text x="260" y="152" text-anchor="middle" fill="#475569" font-size="9" font-weight="600">INV</text>
      <!-- Solar node -->
      <rect x="190" y="8" width="140" height="54" rx="8" fill="#0f1724" stroke="#92400e" stroke-width="1.5"/>
      <text x="260" y="28" text-anchor="middle" fill="#fbbf24" font-size="12">&#9728; Solar</text>
      <text id="n-pv" x="260" y="50" text-anchor="middle" fill="#fde68a" font-size="16" font-weight="700">0 W</text>
      <!-- Battery node -->
      <rect x="8" y="108" width="150" height="80" rx="8" fill="#0f1724" stroke="#065f46" stroke-width="1.5"/>
      <text x="83" y="128" text-anchor="middle" fill="#34d399" font-size="12">&#128267; Battery</text>
      <text id="n-soc" x="83" y="153" text-anchor="middle" fill="#6ee7b7" font-size="20" font-weight="700">0%</text>
      <text id="n-bat" x="83" y="174" text-anchor="middle" fill="#94a3b8" font-size="11">0 W</text>
      <!-- Home node -->
      <rect x="362" y="108" width="150" height="80" rx="8" fill="#0f1724" stroke="#3730a3" stroke-width="1.5"/>
      <text x="437" y="128" text-anchor="middle" fill="#a78bfa" font-size="12">&#127968; Home</text>
      <text id="n-home" x="437" y="160" text-anchor="middle" fill="#c4b5fd" font-size="20" font-weight="700">0 W</text>
      <!-- Grid node -->
      <rect x="190" y="230" width="140" height="57" rx="8" fill="#0f1724" stroke="#155e75" stroke-width="1.5"/>
      <text x="260" y="250" text-anchor="middle" fill="#22d3ee" font-size="12">&#9889; Grid</text>
      <text id="n-grid" x="260" y="275" text-anchor="middle" fill="#67e8f9" font-size="14" font-weight="700">0 W</text>
    </svg>
  </section>

  <!-- Right panel: SOC + stats -->
  <div class="right-panel">
    <div class="card">
      <h2>Battery State</h2>
      <div class="soc-wrap">
        <div class="soc-ring-wrap">
          <svg viewBox="0 0 100 100">
            <circle cx="50" cy="50" r="42" fill="none" stroke="#1e2d3d" stroke-width="10"/>
            <circle id="soc-ring" cx="50" cy="50" r="42" fill="none" stroke="#34d399" stroke-width="10"
              stroke-dasharray="263.9" stroke-dashoffset="263.9"
              stroke-linecap="round" transform="rotate(-90 50 50)"/>
          </svg>
        </div>
        <div class="soc-info">
          <div class="soc-pct" id="soc-pct">0%</div>
          <div class="soc-label">State of Charge</div>
          <div class="bat-pw" id="soc-pw">&#8212;</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Live Power</h2>
      <div class="stat-row">
        <div class="stat-box">
          <div class="sb-label">Solar</div>
          <div class="sb-val solar" id="s-pv">0</div>
          <div class="sb-sub">W</div>
        </div>
        <div class="stat-box">
          <div class="sb-label">Grid</div>
          <div class="sb-val" id="s-grid">0</div>
          <div class="sb-sub" id="s-grid-dir">&#8212;</div>
        </div>
        <div class="stat-box">
          <div class="sb-label">Home</div>
          <div class="sb-val home-load" id="s-home">0</div>
          <div class="sb-sub">W</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Forecast chart -->
  <section class="card forecast-card">
    <h2>Solar Forecast &#8212; Today</h2>
    <div class="fc-meta">
      <span>Today: <strong id="fc-today">&#8212;</strong> kWh</span>
      <span>Tomorrow: <strong id="fc-tmrw">&#8212;</strong> kWh</span>
      <span>Remaining: <strong id="fc-rem">&#8212;</strong> kWh</span>
      <span>Bias factor: <strong id="fc-bias">&#8212;</strong></span>
    </div>
    <svg id="fc-svg" viewBox="0 0 756 130" xmlns="http://www.w3.org/2000/svg">
      <text x="378" y="70" text-anchor="middle" fill="#374151" font-size="13">Loading forecast...</text>
    </svg>
  </section>

  <!-- Bottom row -->
  <div class="bottom-row">

    <!-- Decision -->
    <section class="card">
      <h2>Manager Decision</h2>
      <div id="action-badge" class="action-badge action-unknown">&#8212;</div>
      <div class="dl">
        <div class="dl-item">
          <span class="dk">Dawn viable</span>
          <span class="dv" id="dec-dawn">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk">SOC at dawn</span>
          <span class="dv" id="dec-soc-dawn">&#8212;</span>
        </div>
      </div>
      <div class="reason" id="dec-reason">&#8212;</div>
    </section>

    <!-- Today summary -->
    <section class="card">
      <h2>Today&#8217;s Summary</h2>
      <div class="dl">
        <div class="dl-item"><span class="dk">PV generated</span><span class="dv solar" id="sum-pv">&#8212;</span></div>
        <div class="dl-item"><span class="dk">Home used</span><span class="dv home-load" id="sum-home">&#8212;</span></div>
        <div class="dl-item"><span class="dk">Grid import</span><span class="dv grid-import" id="sum-imp">&#8212;</span></div>
        <div class="dl-item"><span class="dk">Grid export</span><span class="dv grid-export" id="sum-exp">&#8212;</span></div>
        <div class="dl-item"><span class="dk">Peak SOC</span><span class="dv" id="sum-peak">&#8212;</span></div>
        <div class="dl-item"><span class="dk">Min SOC</span><span class="dv" id="sum-min">&#8212;</span></div>
        <div class="dl-item"><span class="dk">Self-sufficiency</span><span class="dv" id="sum-ss">&#8212;</span></div>
      </div>
      <div class="self-suff-bar"><div class="self-suff-fill" id="ss-bar" style="width:0%"></div></div>
    </section>

    <!-- Tariff -->
    <section class="card">
      <h2>Tariff</h2>
      <div class="tariff-rate"><span id="tar-rate">&#8212;</span>p</div>
      <div class="tariff-sub" id="tar-name">&#8212;</div>
      <div class="tariff-sub muted" id="tar-code">&#8212;</div>
      <div class="tariff-tmrw">Tomorrow: <span id="tar-tmrw">&#8212;</span>p</div>
    </section>

  </div>
</main>

<script>
const ACTION_LABELS = {
  self_consumption:  'Self Consumption',
  start_import:      'Starting Import',
  stop_import:       'Stopping Import',
  schedule_import:   'Import Scheduled',
  start_export:      'Night Export',
  stop_export:       'Stopping Export',
  solar_overflow:    'Solar Overflow Export',
  unknown:           'Unknown'
};
const ACTION_CLASS = {
  self_consumption: 'action-self',
  solar_overflow:   'action-overflow',
  start_export:     'action-export',
  stop_export:      'action-export',
  start_import:     'action-import',
  stop_import:      'action-import',
  schedule_import:  'action-schedule',
  unknown:          'action-unknown'
};

function fmtW(w) {
  const abs = Math.abs(w);
  if (abs >= 1000) return (w / 1000).toFixed(1) + ' kW';
  return w.toLocaleString() + ' W';
}
function fmtKwh(v) { return v !== null && v !== undefined ? v.toFixed(1) + ' kWh' : '\u2014'; }

function setFlow(id, watts, forwardPositive) {
  const el = document.getElementById(id);
  if (!el) return;
  const threshold = 30;
  if (Math.abs(watts) < threshold) {
    el.style.opacity = '0';
    el.classList.remove('flow-fwd','flow-rev');
    return;
  }
  el.style.opacity = '1';
  const goFwd = forwardPositive ? (watts > 0) : (watts < 0);
  el.classList.toggle('flow-fwd',  goFwd);
  el.classList.toggle('flow-rev', !goFwd);
}

function renderForecast(hourly) {
  const svg = document.getElementById('fc-svg');
  const entries = Object.entries(hourly).sort((a,b)=>a[0].localeCompare(b[0]));
  if (!entries.length) { svg.innerHTML = '<text x="378" y="70" text-anchor="middle" fill="#374151" font-size="13">No forecast data</text>'; return; }
  const maxWh  = Math.max(...entries.map(e=>e[1]), 1);
  const now    = new Date();
  const curHr  = now.getHours();
  const n      = entries.length;
  const bw     = Math.floor(740 / n) - 2;
  const chartH = 100;
  let out = '';
  entries.forEach(([key,wh], i) => {
    const hr  = parseInt(key.split(':')[0]);
    const kwh = (wh/1000);
    const bh  = Math.max(1, Math.round((wh/maxWh)*chartH));
    const x   = 8 + i*(bw+2);
    const y   = chartH - bh;
    const past   = hr < curHr;
    const curr   = hr === curHr;
    const col    = curr ? '#fbbf24' : '#34d399';
    const opac   = past ? '0.35' : '1';
    out += `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="${col}" opacity="${opac}" rx="2"/>`;
    if (hr % 2 === 0) {
      out += `<text x="${x+bw/2}" y="118" text-anchor="middle" fill="#4b5563" font-size="9">${hr}</text>`;
    }
    if (!past && kwh >= 0.2) {
      out += `<text x="${x+bw/2}" y="${y-3}" text-anchor="middle" fill="${curr?'#fbbf24':'#6ee7b7'}" font-size="8">${kwh.toFixed(1)}</text>`;
    }
  });
  // x-axis line
  out += `<line x1="6" y1="${chartH}" x2="750" y2="${chartH}" stroke="#1e2d3d" stroke-width="1"/>`;
  svg.innerHTML = out;
}

function updateAlerts(d) {
  const bar = document.getElementById('alert-bar');
  const msgs = [];
  if (d.vpp && d.vpp.active) {
    msgs.push('\u26a1 VPP EVENT ACTIVE \u2014 Axle controlling battery' + (d.vpp.event_str ? ' \u00b7 ' + d.vpp.event_str : ''));
  } else if (d.vpp && d.vpp.state === 'announced') {
    msgs.push('\u26a1 VPP event announced: ' + (d.vpp.event_str || ''));
  }
  if (d.storm && d.storm.level !== 'none') {
    msgs.push('\u26a0\ufe0f Storm watch: ' + d.storm.level + ' \u2014 dawn SOC target raised');
  }
  if (d.flags && d.flags.import_active) {
    msgs.push('\u2193 Charging from grid');
  }
  if (!d.flags || !d.flags.modbus_connected) {
    msgs.push('\u274c Modbus disconnected');
  }
  if (msgs.length) {
    bar.style.display = 'block';
    bar.className     = msgs.some(m=>m.includes('Storm')||m.includes('Modbus')) ? 'warn' : '';
    bar.textContent   = msgs.join(' \u2022 ');
  } else {
    bar.style.display = 'none';
  }
}

function update(d) {
  if (d.error) { document.getElementById('ts').textContent = 'Error: ' + d.error; return; }
  document.getElementById('ts').textContent = d.timestamp || '\u2014';

  // SOC ring
  const soc = d.battery ? d.battery.soc_pct : 0;
  const circ = 263.9;
  const offset = circ - (soc/100)*circ;
  const ring = document.getElementById('soc-ring');
  ring.style.strokeDashoffset = offset;
  ring.style.stroke = soc >= 60 ? '#34d399' : soc >= 30 ? '#fbbf24' : '#f87171';
  document.getElementById('soc-pct').textContent  = soc.toFixed(1) + '%';
  document.getElementById('soc-pct').style.color  = ring.style.stroke;
  const batW = d.battery ? d.battery.power_w : 0;
  const batDir = batW > 30 ? 'Charging ' : batW < -30 ? 'Discharging ' : 'Idle';
  const batCol = batW > 30 ? '#34d399' : batW < -30 ? '#a78bfa' : '#64748b';
  const pwEl = document.getElementById('soc-pw');
  pwEl.textContent = batDir + (Math.abs(batW) > 30 ? fmtW(batW) : '');
  pwEl.style.color = batCol;

  // Flow SVG nodes
  const pvW   = d.solar ? d.solar.power_w : 0;
  const gridW = d.grid  ? d.grid.power_w  : 0;
  const homeW = d.home  ? d.home.load_w   : 0;
  document.getElementById('n-pv').textContent   = fmtW(pvW);
  document.getElementById('n-soc').textContent  = soc.toFixed(1) + '%';
  document.getElementById('n-bat').textContent  = fmtW(batW) + (batW > 30 ? ' \u25b2' : batW < -30 ? ' \u25bc' : '');
  document.getElementById('n-home').textContent = fmtW(homeW);
  const gridLabel = gridW > 30 ? 'Import ' + fmtW(gridW) : gridW < -30 ? 'Export ' + fmtW(-gridW) : 'Standby';
  document.getElementById('n-grid').textContent = gridLabel;
  const gridLineCol = gridW < -30 ? '#22d3ee' : gridW > 30 ? '#f87171' : '#22d3ee';
  document.getElementById('fl-grid').style.stroke = gridLineCol;

  // Animated flow lines
  // Solar: always flows toward inverter (down) when generating
  setFlow('fl-solar', pvW,   true);
  // Battery: >0 charging (flows toward inverter from battery side), <0 discharging (reversed)
  // Line goes battery→inverter; positive batW = charging = energy going FROM grid/PV INTO battery
  // So when charging (batW>0), flow goes from right to left (inverter→battery), i.e. reversed
  setFlow('fl-bat',   batW,  false);
  // Home: always flows away from inverter (always positive load)
  setFlow('fl-home',  homeW, true);
  // Grid: positive = import (flows toward inverter = forward), negative = export (reversed)
  setFlow('fl-grid',  gridW, false);

  // Stat boxes
  document.getElementById('s-pv').textContent  = pvW.toLocaleString();
  const gEl = document.getElementById('s-grid');
  const gDir = document.getElementById('s-grid-dir');
  if (gridW > 30) {
    gEl.textContent = Math.abs(gridW).toLocaleString();
    gEl.className = 'sb-val grid-import';
    gDir.textContent = 'W import';
  } else if (gridW < -30) {
    gEl.textContent = Math.abs(gridW).toLocaleString();
    gEl.className = 'sb-val grid-export';
    gDir.textContent = 'W export';
  } else {
    gEl.textContent = '0';
    gEl.className = 'sb-val muted';
    gDir.textContent = 'standby';
  }
  document.getElementById('s-home').textContent = homeW.toLocaleString();

  // Forecast
  if (d.solar) {
    document.getElementById('fc-today').textContent = d.solar.today_kwh;
    document.getElementById('fc-tmrw').textContent  = d.solar.tomorrow_kwh;
    document.getElementById('fc-rem').textContent   = d.solar.remaining_kwh;
    document.getElementById('fc-bias').textContent  = d.solar.bias_factor;
  }
  if (d.hourly_forecast) renderForecast(d.hourly_forecast);

  // Decision
  const action = (d.decision && d.decision.action) || 'unknown';
  const badge  = document.getElementById('action-badge');
  badge.textContent = ACTION_LABELS[action] || action;
  badge.className   = 'action-badge ' + (ACTION_CLASS[action] || 'action-unknown');
  if (d.decision) {
    const dawnEl = document.getElementById('dec-dawn');
    dawnEl.textContent  = d.decision.dawn_viable ? 'Yes' : 'No';
    dawnEl.className    = 'dv ' + (d.decision.dawn_viable ? 'dawn-ok' : 'dawn-warn');
    document.getElementById('dec-soc-dawn').textContent = d.decision.soc_at_dawn_kwh.toFixed(1) + ' kWh';
    document.getElementById('dec-reason').textContent   = d.decision.reason || '\u2014';
  }

  // Today summary
  if (d.today_summary) {
    const s = d.today_summary;
    document.getElementById('sum-pv').textContent   = s.pv_kwh   + ' kWh';
    document.getElementById('sum-home').textContent = s.home_kwh + ' kWh';
    document.getElementById('sum-imp').textContent  = s.import_kwh + ' kWh';
    document.getElementById('sum-exp').textContent  = s.export_kwh + ' kWh';
    document.getElementById('sum-peak').textContent = s.peak_soc + '%';
    document.getElementById('sum-min').textContent  = s.min_soc + '%';
    document.getElementById('sum-ss').textContent   = s.self_suff + '%';
    document.getElementById('ss-bar').style.width   = Math.min(100, s.self_suff) + '%';
  }

  // Tariff
  if (d.tariff) {
    document.getElementById('tar-rate').textContent = d.tariff.today_p !== null ? d.tariff.today_p : '\u2014';
    document.getElementById('tar-name').textContent = d.tariff.name || '\u2014';
    document.getElementById('tar-code').textContent = d.tariff.product_code || '';
    document.getElementById('tar-tmrw').textContent = d.tariff.tomorrow_p !== null ? d.tariff.tomorrow_p : 'TBD';
  }

  updateAlerts(d);
}

let countdown = 30;
function startCountdown() {
  setInterval(() => {
    countdown--;
    document.getElementById('cdwn').textContent = countdown;
    if (countdown <= 0) { countdown = 30; fetchStatus(); }
  }, 1000);
}

async function fetchStatus() {
  countdown = 30;
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    update(d);
  } catch(e) {
    document.getElementById('ts').textContent = 'Fetch error: ' + e.message;
  }
}

fetchStatus();
startCountdown();
</script>
</body>
</html>"""

# Pre-encoded once at import time — avoids per-request encoding overhead.
_DASHBOARD_BYTES = DASHBOARD_HTML.encode("utf-8")


# ============================================================
# HTTP server
# ============================================================

class _DashboardTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Non-blocking threaded TCP server with address reuse."""
    allow_reuse_address = True
    daemon_threads      = True


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Request handler for the Sigenergy web dashboard."""

    # Set by WebDashboard.start() before the server thread launches.
    _plugin_ref = None

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _DASHBOARD_BYTES)

        elif self.path == "/api/status":
            if self._plugin_ref is None:
                body = b'{"error":"plugin not ready"}'
            else:
                try:
                    data = self._plugin_ref.get_dashboard_data()
                    body = json.dumps(data).encode()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            self._send(200, "application/json", body)

        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass   # suppress access log noise in Indigo event log


# ============================================================
# Public interface
# ============================================================

class WebDashboard:
    """Manages the lifecycle of the HTTP dashboard server thread."""

    def __init__(self, plugin, port=DASHBOARD_PORT):
        self._plugin = plugin
        self._port   = port
        self._server = None
        self._thread = None

    def start(self):
        _DashboardHandler._plugin_ref = self._plugin
        self._server = _DashboardTCPServer(("", self._port), _DashboardHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="SigenWebDash",
            daemon=True,
        )
        self._thread.start()
        logging.getLogger("Sigenergy").info(
            f"[Web] Dashboard started on port {self._port}"
        )

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        self._thread = None
