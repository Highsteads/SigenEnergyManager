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
body{
  background:
    linear-gradient(135deg,#0a0e22 0%,#0d1a34 30%,#0a1f2e 70%,#15102a 100%);
  background-attachment:fixed;
  color:#e2e8f0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:14px;
  min-height:100vh;
  position:relative;
  overflow-x:hidden;
}
/* Animated radial-gradient overlay — bolder glow, much more visible */
body::before{
  content:'';
  position:fixed;inset:0;
  background:
    radial-gradient(900px 600px at 18% 22%, rgba(125,211,252,0.28), transparent 55%),
    radial-gradient(800px 600px at 82% 78%, rgba(167,139,250,0.22), transparent 55%),
    radial-gradient(700px 500px at 50% 100%, rgba(52,211,153,0.18), transparent 55%);
  animation:bg-drift 28s ease-in-out infinite alternate;
  pointer-events:none;z-index:-1;
}
@keyframes bg-drift{
  0%   { transform:translate(0,0)         scale(1);     opacity:1; }
  50%  { transform:translate(-30px,20px)  scale(1.04);  opacity:0.95; }
  100% { transform:translate(30px,-25px)  scale(1.06);  opacity:1; }
}
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px;
  background:rgba(15,23,36,0.55);
  backdrop-filter:blur(14px) saturate(140%);
  -webkit-backdrop-filter:blur(14px) saturate(140%);
  border-bottom:1px solid rgba(125,211,252,0.10);
  position:sticky;top:0;z-index:10;
}
header h1{
  font-size:16px;font-weight:600;
  color:#7dd3fc;letter-spacing:.3px;
  text-shadow:0 0 14px rgba(125,211,252,0.35);
}
.hdr-right{text-align:right;line-height:1.6}
.hdr-right .ts{font-size:13px;color:#cbd5e1;font-variant-numeric:tabular-nums}
.hdr-right .ts::before{
  content:'●';
  display:inline-block;
  color:#34d399;
  margin-right:7px;
  animation:live-pulse 2.4s ease-in-out infinite;
}
@keyframes live-pulse{
  0%,100% { opacity:1;    text-shadow:0 0 12px rgba(52,211,153,0.85); }
  50%     { opacity:0.45; text-shadow:0 0 4px  rgba(52,211,153,0.20); }
}
.hdr-right .cdwn{font-size:11px;color:#64748b;font-variant-numeric:tabular-nums}
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
.bottom-row{grid-column:1 / -1;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
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
/* --- economics card (v5.3) --- */
.eco-rate{font-size:20px;font-weight:700;color:#34d399}
.eco-rate.eco-neg{color:#f87171}
.eco-sub{font-size:11px;color:#64748b;margin-top:2px}
.eco-divider{height:1px;background:#1e2d3d;margin:8px 0}
/* --- period totals table (v5.5) --- */
.period-card{grid-column:1 / -1}
.period-table{width:100%;border-collapse:collapse;font-size:12px}
.period-table th{text-align:right;color:#64748b;font-weight:500;padding:6px 8px;border-bottom:1px solid #1e2d3d;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
.period-table th:first-child{text-align:left}
.period-table td{text-align:right;padding:8px;border-bottom:1px solid #0f1724;color:#e2e8f0}
.period-table td:first-child{text-align:left;color:#cbd5e1;font-weight:600}
.period-table td.tdays{color:#64748b;font-size:11px}
.period-table tr:last-child td{border-bottom:none}
.period-pos{color:#34d399}
.period-neg{color:#f87171}
.period-table .avg{color:#94a3b8;font-size:11px;display:block}
/* --- charts (v5.2) --- */
.chart-card{grid-column:1 / -1}
.chart-tabs{display:flex;gap:6px;margin-bottom:10px}
.chart-tab{background:#0a1020;color:#94a3b8;border:1px solid #1e2d3d;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;font-family:inherit}
.chart-tab.active{background:#14291b;color:#34d399;border-color:#166534}
.chart-wrap{position:relative;height:200px;margin-bottom:14px}
.chart-wrap:last-child{margin-bottom:0}
/* --- v5.8 glamour pass: glass cards, glow numbers, hover, pulses --- */
.card{
  background:rgba(15,23,36,0.42) !important;
  backdrop-filter:blur(18px) saturate(160%);
  -webkit-backdrop-filter:blur(18px) saturate(160%);
  border:1px solid rgba(125,211,252,0.18) !important;
  border-radius:14px !important;
  box-shadow:
    0 8px 32px rgba(0,0,0,0.30),
    inset 0 0 0 1px rgba(255,255,255,0.04);
  transition:transform .25s ease, box-shadow .25s ease, border-color .25s ease;
}
.card:hover{
  transform:translateY(-3px);
  border-color:rgba(125,211,252,0.40) !important;
  box-shadow:
    0 18px 42px rgba(0,0,0,0.36),
    0 0 36px rgba(125,211,252,0.12),
    inset 0 0 0 1px rgba(255,255,255,0.06);
}
.card h2{
  font-size:11px !important;
  color:#94a3b8 !important;
  text-transform:uppercase;letter-spacing:.9px;
}
/* Headline number glows */
.soc-info .soc-pct{
  text-shadow:0 0 26px rgba(52,211,153,0.50), 0 0 12px rgba(52,211,153,0.30);
}
.eco-rate{
  text-shadow:0 0 24px rgba(52,211,153,0.45), 0 0 10px rgba(52,211,153,0.25);
  transition:text-shadow .35s ease, color .25s ease;
}
.eco-rate.eco-neg{
  text-shadow:0 0 24px rgba(248,113,113,0.45), 0 0 10px rgba(248,113,113,0.25);
}
.tariff-rate{
  text-shadow:0 0 20px rgba(251,191,36,0.40), 0 0 10px rgba(251,191,36,0.20);
}
.stat-box .sb-val{
  font-variant-numeric:tabular-nums;
  text-shadow:0 0 12px rgba(125,211,252,0.18);
}
/* Action badge — slightly stronger */
.action-badge{box-shadow:0 0 18px rgba(0,0,0,0.30) inset, 0 0 12px currentColor}
.action-badge.action-self{box-shadow:0 0 14px rgba(52,211,153,0.20)}
.action-badge.action-overflow{box-shadow:0 0 14px rgba(163,230,53,0.20)}
.action-badge.action-export{box-shadow:0 0 14px rgba(34,211,238,0.22)}
.action-badge.action-import{box-shadow:0 0 14px rgba(248,113,113,0.22)}
/* Tabular numbers everywhere — stops digit jitter on ticking values */
.soc-pct, .eco-rate, .tariff-rate, .sb-val, .dl-item .dv,
.period-table td{ font-variant-numeric:tabular-nums; }
/* Subtle fade-in for cards on load */
.card{ animation:card-in .55s cubic-bezier(.18,.78,.30,1.05) both; }
.card:nth-child(2){ animation-delay:.05s; }
.card:nth-child(3){ animation-delay:.10s; }
.card:nth-child(4){ animation-delay:.15s; }
.card:nth-child(5){ animation-delay:.20s; }
@keyframes card-in{
  from{ opacity:0; transform:translateY(8px); }
  to  { opacity:1; transform:translateY(0); }
}
/* Skeleton shimmer for empty/unloaded cells */
.skel{
  display:inline-block;
  background:linear-gradient(90deg,#1e2d3d 0%, #2a3d50 50%, #1e2d3d 100%);
  background-size:200% 100%;
  animation:skel-shim 1.4s linear infinite;
  border-radius:4px;color:transparent;
  min-width:3em;
}
@keyframes skel-shim{
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
/* SOC ring — animate stroke transition + subtle glow filter */
#soc-ring{
  transition:stroke-dashoffset .8s cubic-bezier(.2,.7,.3,1), stroke .4s ease;
  filter:drop-shadow(0 0 6px rgba(52,211,153,0.55));
}
/* Forecast bar tooltip (v5.10) — floating panel on hover */
.fc-tip{
  position:fixed;pointer-events:none;z-index:50;
  background:rgba(15,23,36,0.92);
  backdrop-filter:blur(8px) saturate(160%);
  -webkit-backdrop-filter:blur(8px) saturate(160%);
  border:1px solid rgba(125,211,252,0.30);
  border-radius:8px;padding:6px 10px;
  box-shadow:0 8px 24px rgba(0,0,0,0.40);
  opacity:0;transform:translateY(4px);
  transition:opacity .15s ease, transform .15s ease;
  font-size:11px;line-height:1.3;
  min-width:90px;
}
.fc-tip.fc-tip-show{opacity:1;transform:translateY(0)}
.fc-tip .fc-tip-time{color:#94a3b8;font-size:10px;letter-spacing:.4px}
.fc-tip .fc-tip-kwh{color:#fbbf24;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;text-shadow:0 0 10px rgba(251,191,36,0.35)}
.fc-tip .fc-tip-unit{color:#64748b;font-size:10px;font-weight:500;margin-left:1px}
.fc-bar{cursor:pointer}
.fc-bar rect:first-child{transition:opacity .12s ease}
/* Help tooltip on forecast-meta labels (v5.12) — explains what each number means */
.fc-meta-item{
  cursor:help;
  position:relative;
  border-bottom:1px dotted rgba(125,211,252,0.30);
  padding-bottom:1px;
  transition:color .15s ease, border-color .15s ease;
}
.fc-meta-item:hover{
  color:#cbd5e1;
  border-bottom-color:rgba(125,211,252,0.60);
}
.help-tip{
  position:fixed;pointer-events:none;z-index:60;
  max-width:300px;
  background:rgba(15,23,36,0.95);
  backdrop-filter:blur(10px) saturate(160%);
  -webkit-backdrop-filter:blur(10px) saturate(160%);
  border:1px solid rgba(125,211,252,0.35);
  border-radius:10px;padding:10px 12px;
  box-shadow:0 12px 32px rgba(0,0,0,0.45);
  opacity:0;transform:translateY(4px);
  transition:opacity .18s ease, transform .18s ease;
  font-size:12px;line-height:1.45;color:#cbd5e1;
}
.help-tip.help-tip-show{opacity:1;transform:translateY(0)}
.help-tip .help-tip-title{
  color:#7dd3fc;font-weight:600;font-size:11px;
  letter-spacing:.4px;text-transform:uppercase;
  margin-bottom:6px;
  text-shadow:0 0 10px rgba(125,211,252,0.30);
}
/* Sparkline (SOC card) — 24h trend */
.spark-wrap{margin-top:12px;height:36px;position:relative}
.spark-wrap svg{width:100%;height:100%;display:block}
.spark-wrap path.spark-fill{fill:url(#spark-grad)}
.spark-wrap path.spark-line{fill:none;stroke:#34d399;stroke-width:1.5;filter:drop-shadow(0 0 4px rgba(52,211,153,0.6))}
.spark-cap{font-size:10px;color:#64748b;margin-top:4px;text-align:right;letter-spacing:.4px;font-variant-numeric:tabular-nums}
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
      <div class="spark-wrap"><svg id="spark-soc" viewBox="0 0 200 36" preserveAspectRatio="none"></svg></div>
      <div class="spark-cap" id="spark-soc-cap">&#8212;</div>
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
      <span class="fc-meta-item" data-help-title="Today's forecast" data-help="Bias-corrected solar production estimate for the whole of today (sunrise to sunset), in kWh. Comes from Open-Meteo using your roof's tilt and azimuth for each PV string, with a self-learned correction factor applied based on past forecast-vs-actual accuracy.">Today: <strong id="fc-today">&#8212;</strong> kWh</span>
      <span class="fc-meta-item" data-help-title="Tomorrow's forecast" data-help="Bias-corrected solar production estimate for tomorrow, in kWh. The battery manager uses this to decide whether tonight's flood-prevention export is worthwhile and how much grid charging (if any) to schedule overnight.">Tomorrow: <strong id="fc-tmrw">&#8212;</strong> kWh</span>
      <span class="fc-meta-item" data-help-title="Remaining today" data-help="Sum of forecasted production from the current hour through dusk, in kWh. Updates every refresh — drops as the day progresses and grows briefly if a sunny patch is forecast for the next hour.">Remaining: <strong id="fc-rem">&#8212;</strong> kWh</span>
      <span class="fc-meta-item" data-help-title="Bias correction factor" data-help="Self-learned multiplier the plugin applies to the raw Open-Meteo numbers based on past accuracy.  1.00 = forecast was dead-on. >1.00 = forecast tends to under-predict (boost it up). <1.00 = over-predict (knock it down). Adjusts automatically each midnight.">Bias factor: <strong id="fc-bias">&#8212;</strong></span>
    </div>
    <svg id="fc-svg" viewBox="0 0 756 80" xmlns="http://www.w3.org/2000/svg">
      <text x="378" y="40" text-anchor="middle" fill="#374151" font-size="13">Loading forecast...</text>
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

    <!-- Today's Cost / Economics (v5.3) -->
    <section class="card">
      <h2>Today&#8217;s Cost (so far)</h2>
      <div class="eco-rate" id="eco-benefit">&#8212;</div>
      <div class="eco-sub">benefit from solar today</div>
      <div class="eco-divider"></div>
      <div class="dl">
        <div class="dl-item">
          <span class="dk grid-import">Import paid</span>
          <span class="dv" id="eco-import">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk grid-export">Export earned</span>
          <span class="dv" id="eco-export">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted">Net grid today</span>
          <span class="dv" id="eco-net">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted">Without solar</span>
          <span class="dv" id="eco-nosolar">&#8212;</span>
        </div>
      </div>
      <div class="eco-sub muted" id="eco-rates">&#8212;</div>
    </section>

    <!-- Yesterday's Cost (v5.4) -->
    <section class="card">
      <h2>Yesterday <span class="eco-sub" id="eco-y-date" style="display:inline">&nbsp;</span></h2>
      <div class="eco-rate" id="eco-y-benefit">&#8212;</div>
      <div class="eco-sub">benefit from solar yesterday</div>
      <div class="eco-divider"></div>
      <div class="dl">
        <div class="dl-item">
          <span class="dk grid-import">Import paid</span>
          <span class="dv" id="eco-y-import">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk grid-export">Export earned</span>
          <span class="dv" id="eco-y-export">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted">Net grid</span>
          <span class="dv" id="eco-y-net">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted">Without solar</span>
          <span class="dv" id="eco-y-nosolar">&#8212;</span>
        </div>
      </div>
      <div class="eco-sub muted" id="eco-y-rates">&#8212;</div>
    </section>

  </div>

  <!-- Period totals (v5.5) -->
  <section class="card period-card">
    <h2>Period totals (and per-day average)</h2>
    <table class="period-table">
      <thead>
        <tr>
          <th>Period</th>
          <th>Days</th>
          <th>Solar benefit</th>
          <th>Net grid</th>
          <th>Without solar</th>
          <th>Import paid</th>
          <th>Export earned</th>
        </tr>
      </thead>
      <tbody id="period-tbody">
        <tr><td>Week (last 7d)</td><td colspan="6" class="muted">&#8212;</td></tr>
        <tr><td>Month (so far)</td><td colspan="6" class="muted">&#8212;</td></tr>
        <tr><td>Year (last 365d)</td><td colspan="6" class="muted">&#8212;</td></tr>
      </tbody>
    </table>
  </section>

  <!-- Calendar-month totals (v5.6) -->
  <section class="card period-card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h2 style="margin-bottom:0"><span id="cal-year">&#8212;</span> calendar months</h2>
      <div class="chart-tabs" id="cal-year-tabs"></div>
    </div>
    <table class="period-table">
      <thead>
        <tr>
          <th>Month</th>
          <th>Days</th>
          <th>Solar benefit</th>
          <th>Net grid</th>
          <th>Without solar</th>
          <th>Import paid</th>
          <th>Export earned</th>
        </tr>
      </thead>
      <tbody id="cal-tbody">
        <tr><td colspan="7" class="muted">&#8212;</td></tr>
      </tbody>
      <tfoot>
        <tr id="cal-total-row" style="border-top:2px solid #1e2d3d;font-weight:600">
          <td>Year total</td>
          <td class="tdays" id="cal-total-days">&#8212;</td>
          <td id="cal-total-benefit">&#8212;</td>
          <td id="cal-total-net">&#8212;</td>
          <td id="cal-total-nosolar">&#8212;</td>
          <td id="cal-total-import">&#8212;</td>
          <td id="cal-total-export">&#8212;</td>
        </tr>
      </tfoot>
    </table>
  </section>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>

  <!-- Charts row (v5.2) -->
  <section class="card chart-card">
    <h2>Last 24 hours</h2>
    <div class="chart-tabs">
      <button class="chart-tab active" data-range="24">24h</button>
      <button class="chart-tab" data-range="48">48h</button>
      <button class="chart-tab" data-range="168">7d</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-soc"></canvas></div>
    <div class="chart-wrap"><canvas id="chart-energy"></canvas></div>
  </section>

  <section class="card chart-card">
    <h2>Daily totals (last 30 days)</h2>
    <div class="chart-wrap"><canvas id="chart-daily"></canvas></div>
  </section>

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
  if (!entries.length) { svg.innerHTML = '<text x="378" y="40" text-anchor="middle" fill="#374151" font-size="13">No forecast data</text>'; return; }
  // Values from /api/status hourly_forecast are ALREADY in kWh (plugin
  // converts from Wh in get_dashboard_data).  Pre-v5.11 code re-divided
  // by 1000 here making every tooltip read 0.00 — confirmed bug.
  const maxK   = Math.max(...entries.map(e=>e[1]), 0.1);
  const now    = new Date();
  const curHr  = now.getHours();
  const n      = entries.length;
  const bw     = Math.floor(740 / n) - 2;
  const chartH = 56;
  const labelY = 74;
  let out = '';
  entries.forEach(([key, kwh], i) => {
    const hr  = parseInt(key.split(':')[0]);
    const bh  = Math.max(1, Math.round((kwh/maxK)*chartH));
    const x   = 8 + i*(bw+2);
    const y   = chartH - bh;
    const past   = hr < curHr;
    const curr   = hr === curHr;
    const col    = curr ? '#fbbf24' : '#34d399';
    const opac   = past ? '0.32' : '1';
    out +=
      `<g class="fc-bar" data-hr="${hr}" data-kwh="${kwh.toFixed(2)}" data-curr="${curr ? '1':'0'}">`
      + `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="${col}" opacity="${opac}" rx="2"/>`
      + `<rect x="${x-1}" y="0" width="${bw+2}" height="${chartH}" fill="transparent"/>`
      + `<title>${hr.toString().padStart(2,'0')}:00 — ${kwh.toFixed(2)} kWh</title>`
      + `</g>`;
    if (hr % 2 === 0) {
      out += `<text x="${x+bw/2}" y="${labelY}" text-anchor="middle" fill="#64748b" font-size="9">${hr}</text>`;
    }
  });
  // x-axis line
  out += `<line x1="6" y1="${chartH}" x2="750" y2="${chartH}" stroke="#1e2d3d" stroke-width="1"/>`;
  svg.innerHTML = out;
  _wireForecastTooltip();
}

/* Wire up explanatory tooltips on any element with data-help (v5.12).
   Used on the forecast-meta labels (Today / Tomorrow / Remaining /
   Bias factor) but reusable for any future help-tip needs. */
function _wireHelpTips() {
  let tip = document.getElementById('help-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'help-tip';
    tip.className = 'help-tip';
    document.body.appendChild(tip);
  }
  const showTip = (e) => {
    const el = e.currentTarget;
    const title = el.dataset.helpTitle || '';
    const body  = el.dataset.help || '';
    tip.innerHTML = (title ? '<div class="help-tip-title">' + title + '</div>' : '') + body;
    tip.classList.add('help-tip-show');
    moveTip(e);
  };
  const moveTip = (e) => {
    const r = tip.getBoundingClientRect();
    const margin = 12;
    let x = e.clientX + 14;
    let y = e.clientY + 14;
    if (x + r.width  > window.innerWidth  - margin) x = window.innerWidth  - r.width  - margin;
    if (y + r.height > window.innerHeight - margin) y = e.clientY - r.height - 14;
    tip.style.left = Math.max(margin, x) + 'px';
    tip.style.top  = Math.max(margin, y) + 'px';
  };
  const hideTip = () => tip.classList.remove('help-tip-show');
  document.querySelectorAll('[data-help]').forEach(el => {
    if (el.dataset._helpWired) return;
    el.dataset._helpWired = '1';
    el.addEventListener('mouseenter', showTip);
    el.addEventListener('mousemove',  moveTip);
    el.addEventListener('mouseleave', hideTip);
  });
}

/* Custom floating tooltip for the hourly-forecast bars (v5.10). */
function _wireForecastTooltip() {
  const svg = document.getElementById('fc-svg');
  let tip = document.getElementById('fc-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'fc-tip';
    tip.className = 'fc-tip';
    document.body.appendChild(tip);
  }
  const showTip = (e) => {
    const g = e.currentTarget;
    const hr   = g.dataset.hr;
    const kwh  = g.dataset.kwh;
    const curr = g.dataset.curr === '1';
    const hrStr = hr.padStart(2,'0') + ':00';
    tip.innerHTML =
      '<div class="fc-tip-time">' + hrStr + (curr ? ' (now)' : '') + '</div>'
      + '<div class="fc-tip-kwh">' + kwh + ' <span class="fc-tip-unit">kWh</span></div>';
    tip.classList.add('fc-tip-show');
    // Highlight the hovered bar
    svg.querySelectorAll('.fc-bar rect:first-child').forEach(r => r.setAttribute('data-orig-opacity', r.getAttribute('opacity') || '1'));
    const targetRect = g.querySelector('rect');
    if (targetRect) targetRect.setAttribute('opacity', '1');
  };
  const moveTip = (e) => {
    const x = e.clientX + 12;
    const y = e.clientY - 10;
    tip.style.left = Math.min(window.innerWidth - 160, x) + 'px';
    tip.style.top  = Math.max(8, y - 60) + 'px';
  };
  const hideTip = (e) => {
    tip.classList.remove('fc-tip-show');
    // Restore original opacities
    svg.querySelectorAll('.fc-bar rect:first-child').forEach(r => {
      const orig = r.getAttribute('data-orig-opacity');
      if (orig) r.setAttribute('opacity', orig);
    });
  };
  svg.querySelectorAll('.fc-bar').forEach(g => {
    g.addEventListener('mouseenter', showTip);
    g.addEventListener('mousemove',  moveTip);
    g.addEventListener('mouseleave', hideTip);
  });
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
  tweenNumber(document.getElementById('soc-pct'), soc, {decimals:1, suffix:'%', duration:700});
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

  // Today's + Yesterday's economics (v5.3 / v5.4)
  function _fmtGbp(v) {
    if (v === null || v === undefined) return '\u2014';
    const sign = v < 0 ? '\u2212' : '';
    return sign + '\u00a3' + Math.abs(v).toFixed(2);
  }
  function _renderEconomics(prefix, econ) {
    if (!econ) return;
    const benefitEl = document.getElementById(prefix + 'benefit');
    if (econ.solar_benefit_gbp === null) {
      benefitEl.textContent = '\u2014';
      benefitEl.classList.remove('eco-neg');
    } else {
      // Smooth tween on the headline \u00a3 figure
      const v = econ.solar_benefit_gbp;
      const prefixChar = v < 0 ? '\u2212\u00a3' : '\u00a3';
      tweenNumber(benefitEl, Math.abs(v),
                  {decimals:2, prefix:prefixChar, duration:700});
      if (v < 0) benefitEl.classList.add('eco-neg');
      else benefitEl.classList.remove('eco-neg');
    }
    document.getElementById(prefix + 'import').textContent  = _fmtGbp(econ.import_cost_gbp);
    document.getElementById(prefix + 'export').textContent  = _fmtGbp(econ.export_revenue_gbp);
    const netEl = document.getElementById(prefix + 'net');
    netEl.textContent = _fmtGbp(econ.net_today_gbp);
    netEl.style.color = econ.net_today_gbp !== null && econ.net_today_gbp < 0
      ? '#f87171' : '#34d399';
    document.getElementById(prefix + 'nosolar').textContent = _fmtGbp(econ.no_solar_cost_gbp);
    const ratesParts = [];
    if (econ.import_rate_p !== null) ratesParts.push('Import ' + econ.import_rate_p + 'p');
    if (econ.export_rate_p !== null) ratesParts.push('Export ' + econ.export_rate_p + 'p');
    document.getElementById(prefix + 'rates').textContent = ratesParts.join('  /  ');
  }
  function _renderPeriods(periods) {
    if (!periods) return;
    const tbody = document.getElementById('period-tbody');
    if (!tbody) return;
    const labels = [
      ['week',  'Week (last 7d)'],
      ['month', 'Month (so far)'],
      ['year',  'Year (last 365d)'],
    ];
    const cell = (total, avg, posNeg) => {
      if (total === null || total === undefined) return '<td class="muted">—</td>';
      const cls = posNeg ? (total < 0 ? 'period-neg' : 'period-pos') : '';
      const totStr = (total < 0 ? '−£' : '£') + Math.abs(total).toFixed(2);
      const avgStr = (avg === null || avg === undefined) ? ''
        : '<span class="avg">' + (avg < 0 ? '−£' : '£') + Math.abs(avg).toFixed(2) + '/day</span>';
      return '<td class="' + cls + '">' + totStr + avgStr + '</td>';
    };
    tbody.innerHTML = labels.map(([key, label]) => {
      const p = periods[key] || {};
      const days = (p.days != null) ? p.days : 0;
      if (!days) return '<tr><td>' + label + '</td><td class="tdays">0</td>'
        + '<td colspan="5" class="muted">no data</td></tr>';
      return '<tr>'
        + '<td>' + label + '</td>'
        + '<td class="tdays">' + days + '</td>'
        + cell(p.benefit_total_gbp,  p.benefit_avg_gbp,  true)
        + cell(p.net_total_gbp,      p.net_avg_gbp,      true)
        + cell(p.no_solar_total_gbp, p.no_solar_avg_gbp, false)
        + cell(p.import_total_gbp,   p.import_avg_gbp,   false)
        + cell(p.export_total_gbp,   p.export_avg_gbp,   false)
        + '</tr>';
    }).join('');
  }

  let _calCurrentYear = null;
  let _calYearsLoaded = false;

  async function _loadYearTabs() {
    if (_calYearsLoaded) return;
    _calYearsLoaded = true;
    try {
      const r = await fetch('/api/years');
      if (!r.ok) return;
      const d = await r.json();
      const years = (d.years || []).slice();
      // Always include the current year even if not in the data set
      const cy = new Date().getFullYear().toString();
      if (years.indexOf(cy) === -1) years.push(cy);
      years.sort();
      const wrap = document.getElementById('cal-year-tabs');
      if (!wrap) return;
      wrap.innerHTML = years.map(y =>
        '<button class="chart-tab" data-year="' + y + '">' + y + '</button>'
      ).join('');
      wrap.querySelectorAll('.chart-tab').forEach(btn => {
        btn.addEventListener('click', () => _switchCalYear(btn.dataset.year));
      });
      _setActiveCalTab(_calCurrentYear || cy);
    } catch (e) { /* silent */ }
  }

  function _setActiveCalTab(year) {
    document.querySelectorAll('#cal-year-tabs .chart-tab').forEach(b => {
      if (b.dataset.year === year) b.classList.add('active');
      else b.classList.remove('active');
    });
  }

  async function _switchCalYear(year) {
    _calCurrentYear = year;
    _setActiveCalTab(year);
    try {
      const r = await fetch('/api/calendar?year=' + encodeURIComponent(year));
      if (!r.ok) return;
      const d = await r.json();
      _renderCalendarMonths(d);
    } catch (e) { /* silent */ }
  }

  function _renderCalendarMonths(cal) {
    if (!cal || !cal.months) return;
    if (_calCurrentYear === null) _calCurrentYear = String(cal.year);
    _loadYearTabs();   // populate tabs on first render
    document.getElementById('cal-year').textContent = cal.year;
    const tbody = document.getElementById('cal-tbody');
    if (!tbody) return;
    const cell = (total, avg, posNeg) => {
      if (total === null || total === undefined) return '<td class="muted">—</td>';
      const cls = posNeg ? (total < 0 ? 'period-neg' : 'period-pos') : '';
      const totStr = (total < 0 ? '−£' : '£') + Math.abs(total).toFixed(2);
      const avgStr = (avg === null || avg === undefined) ? ''
        : '<span class="avg">' + (avg < 0 ? '−£' : '£') + Math.abs(avg).toFixed(2) + '/day</span>';
      return '<td class="' + cls + '">' + totStr + avgStr + '</td>';
    };
    tbody.innerHTML = cal.months.map(m => {
      const days = m.days || 0;
      if (!days) {
        return '<tr><td>' + m.month_name + '</td>'
             + '<td class="tdays">0</td>'
             + '<td colspan="5" class="muted">&#8212;</td></tr>';
      }
      const partialFlag = m.partial ? ' <span class="muted">(partial)</span>' : '';
      return '<tr>'
        + '<td>' + m.month_name + partialFlag + '</td>'
        + '<td class="tdays">' + days + '</td>'
        + cell(m.benefit_total_gbp,  m.benefit_avg_gbp,  true)
        + cell(m.net_total_gbp,      m.net_avg_gbp,      true)
        + cell(m.no_solar_total_gbp, m.no_solar_avg_gbp, false)
        + cell(m.import_total_gbp,   m.import_avg_gbp,   false)
        + cell(m.export_total_gbp,   m.export_avg_gbp,   false)
        + '</tr>';
    }).join('');

    // Year totals (sum of month totals)
    let totDays = 0, totBen = 0, totNet = 0, totNoSol = 0, totImp = 0, totExp = 0;
    let any = false;
    cal.months.forEach(m => {
      if (!m.days) return;
      any = true;
      totDays  += m.days;
      totBen   += m.benefit_total_gbp   || 0;
      totNet   += m.net_total_gbp       || 0;
      totNoSol += m.no_solar_total_gbp  || 0;
      totImp   += m.import_total_gbp    || 0;
      totExp   += m.export_total_gbp    || 0;
    });
    document.getElementById('cal-total-days').textContent = any ? totDays : '0';
    const fmtG = (v, posNeg) => {
      if (!any) return '—';
      const cls = posNeg ? (v < 0 ? 'period-neg' : 'period-pos') : '';
      const s = (v < 0 ? '−£' : '£') + Math.abs(v).toFixed(2);
      return '<span class="' + cls + '">' + s + '</span>';
    };
    document.getElementById('cal-total-benefit').innerHTML  = fmtG(totBen,   true);
    document.getElementById('cal-total-net').innerHTML      = fmtG(totNet,   true);
    document.getElementById('cal-total-nosolar').innerHTML  = fmtG(totNoSol, false);
    document.getElementById('cal-total-import').innerHTML   = fmtG(totImp,   false);
    document.getElementById('cal-total-export').innerHTML   = fmtG(totExp,   false);
  }

  if (d.economics) {
    _renderEconomics('eco-',   d.economics.today);
    _renderEconomics('eco-y-', d.economics.yesterday);
    _renderPeriods(d.economics.periods);
    // Only auto-refresh the calendar card if the user is viewing the
    // current year. If they've switched to a historical year, leave their
    // manual selection alone.
    const incomingYear = d.economics.calendar_months && d.economics.calendar_months.year;
    if (_calCurrentYear === null || String(incomingYear) === _calCurrentYear) {
      _renderCalendarMonths(d.economics.calendar_months);
    } else {
      _loadYearTabs();   // still keep the year-tab list fresh
    }
    // Yesterday's date label
    const dateEl = document.getElementById('eco-y-date');
    if (d.economics.yesterday_date) {
      const dt = new Date(d.economics.yesterday_date + 'T12:00:00');
      if (!isNaN(dt)) {
        dateEl.textContent = '(' + dt.toLocaleDateString([], {
          weekday: 'short', day: 'numeric', month: 'short'
        }) + ')';
      } else {
        dateEl.textContent = '(' + d.economics.yesterday_date + ')';
      }
    } else {
      dateEl.textContent = '';
    }
  }

  updateAlerts(d);
}

let countdown = 5;
function startCountdown() {
  setInterval(() => {
    countdown--;
    document.getElementById('cdwn').textContent = countdown;
    if (countdown <= 0) { countdown = 5; fetchStatus(); }
  }, 1000);
}

async function fetchStatus() {
  countdown = 5;
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    update(d);
  } catch(e) {
    document.getElementById('ts').textContent = 'Fetch error: ' + e.message;
  }
}

/* ============================================================
   v5.8: smooth number transitions for headline values
   ============================================================ */
function tweenNumber(el, target, opts) {
  opts = opts || {};
  const decimals = opts.decimals || 0;
  const prefix   = opts.prefix   || '';
  const suffix   = opts.suffix   || '';
  const dur      = opts.duration || 600;
  if (!el) return;
  const from = parseFloat(el.dataset._val || '0');
  const to   = (target === null || target === undefined || isNaN(target)) ? 0 : Number(target);
  el.dataset._val = String(to);
  if (Math.abs(to - from) < 0.005) {
    el.textContent = prefix + to.toFixed(decimals) + suffix;
    return;
  }
  const t0 = performance.now();
  function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    const cur = from + (to - from) * eased;
    el.textContent = prefix + cur.toFixed(decimals) + suffix;
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* ============================================================
   Charts (v5.2) — Chart.js via CDN
   ============================================================ */

let socChart = null, energyChart = null, dailyChart = null;
let currentRange = 24;

const CHART_COLORS = {
  soc:    '#34d399',
  pv:     '#fbbf24',
  imp:    '#f87171',
  exp:    '#22d3ee',
  home:   '#a78bfa',
  grid:   '#94a3b8',
};

const CHART_BASE = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 200 },
  interaction: { mode: 'index', intersect: false },
  scales: {
    x: { ticks: { color: '#64748b', maxRotation: 0, autoSkipPadding: 20 },
         grid: { color: 'rgba(100,116,139,0.08)' } },
    y: { ticks: { color: '#64748b' },
         grid: { color: 'rgba(100,116,139,0.12)' } },
  },
  plugins: {
    legend: { labels: { color: '#cbd5e1', font: { size: 11 } } },
    tooltip: { backgroundColor: '#0f1724', borderColor: '#1e2d3d',
               borderWidth: 1, titleColor: '#7dd3fc',
               bodyColor: '#e2e8f0', padding: 8 },
  },
};

function fmtT(iso) {
  // ISO "2026-05-12T18:30:00" -> "Mon 14:30" or "14:30"
  const d = new Date(iso.replace(' ', 'T'));
  if (isNaN(d)) return iso.slice(11, 16);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  }
  return d.toLocaleDateString([], {weekday:'short'}) + ' ' +
         d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function _renderSocSparkline(slots) {
  const svg = document.getElementById('spark-soc');
  if (!svg) return;
  // Always show the latest 24h regardless of currentRange
  const recent = slots.slice(-48);   // 48 half-hourly slots = 24h
  if (!recent.length) {
    svg.innerHTML = '';
    document.getElementById('spark-soc-cap').textContent = '—';
    return;
  }
  const W = 200, H = 36, P = 1;
  const ys = recent.map(s => (s.soc_end == null ? 0 : s.soc_end));
  const lo = 0, hi = 100;
  const xStep = (W - 2*P) / Math.max(1, recent.length - 1);
  const yMap = v => H - P - ((v - lo) / (hi - lo)) * (H - 2*P);
  let line = '';
  recent.forEach((s, i) => {
    const x = P + i * xStep;
    const y = yMap(ys[i]);
    line += (i ? ' L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
  });
  const fill = line + ' L' + (W - P).toFixed(1) + ' ' + (H - P) +
                      ' L' + P + ' ' + (H - P) + ' Z';
  svg.innerHTML =
    '<defs><linearGradient id="spark-grad" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%"  stop-color="rgba(52,211,153,0.45)"/>' +
      '<stop offset="100%" stop-color="rgba(52,211,153,0.0)"/>' +
    '</linearGradient></defs>' +
    '<path class="spark-fill" d="' + fill + '"/>' +
    '<path class="spark-line" d="' + line + '"/>';
  const lo24 = Math.min(...ys).toFixed(0);
  const hi24 = Math.max(...ys).toFixed(0);
  document.getElementById('spark-soc-cap').textContent =
    'last 24h  low ' + lo24 + '%   high ' + hi24 + '%';
}

async function refreshCharts() {
  try {
    const r = await fetch('/api/history?hours=' + currentRange);
    if (!r.ok) return;
    const d = await r.json();
    const slots = d.slots || [];
    _renderSocSparkline(slots);
    const labels = slots.map(s => fmtT(s.t));
    const socEnd = slots.map(s => s.soc_end);
    const pv     = slots.map(s => s.pv_kwh);
    const imp    = slots.map(s => -s.import_kwh);   // negative = inflow
    const exp    = slots.map(s => s.export_kwh);
    const home   = slots.map(s => -s.home_kwh);     // negative = consumption

    if (socChart) socChart.destroy();
    socChart = new Chart(document.getElementById('chart-soc'), {
      type: 'line',
      data: { labels, datasets: [{
        label: 'Battery SOC %', data: socEnd,
        borderColor: CHART_COLORS.soc, backgroundColor: 'rgba(52,211,153,0.15)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
      }] },
      options: { ...CHART_BASE,
        scales: { ...CHART_BASE.scales,
          y: { ...CHART_BASE.scales.y, min: 0, max: 100,
               title: { display: true, text: 'SOC %', color: '#94a3b8' } },
        } }
    });

    if (energyChart) energyChart.destroy();
    energyChart = new Chart(document.getElementById('chart-energy'), {
      type: 'bar',
      data: { labels, datasets: [
        { label: 'Solar',  data: pv,   backgroundColor: CHART_COLORS.pv },
        { label: 'Export', data: exp,  backgroundColor: CHART_COLORS.exp },
        { label: 'Import (in)', data: imp,  backgroundColor: CHART_COLORS.imp },
        { label: 'Home (use)',  data: home, backgroundColor: CHART_COLORS.home },
      ] },
      options: { ...CHART_BASE,
        scales: { ...CHART_BASE.scales,
          x: { ...CHART_BASE.scales.x, stacked: true },
          y: { ...CHART_BASE.scales.y, stacked: true,
               title: { display: true, text: 'kWh per slot', color: '#94a3b8' } },
        } }
    });
  } catch(e) { /* silently ignore — charts will reappear next refresh */ }
}

async function refreshDailyChart() {
  try {
    const r = await fetch('/api/daily?days=30');
    if (!r.ok) return;
    const d = await r.json();
    const records = d.records || [];
    const labels = records.map(r => r.date.slice(5));   // MM-DD
    const pv     = records.map(r => r.pv_kwh);
    const imp    = records.map(r => r.grid_import_kwh);
    const exp    = records.map(r => r.grid_export_kwh);
    const home   = records.map(r => r.home_kwh);

    if (dailyChart) dailyChart.destroy();
    dailyChart = new Chart(document.getElementById('chart-daily'), {
      type: 'bar',
      data: { labels, datasets: [
        { label: 'Solar',  data: pv,   backgroundColor: CHART_COLORS.pv },
        { label: 'Export', data: exp,  backgroundColor: CHART_COLORS.exp },
        { label: 'Import', data: imp,  backgroundColor: CHART_COLORS.imp },
        { label: 'Home',   data: home, backgroundColor: CHART_COLORS.home },
      ] },
      options: { ...CHART_BASE,
        scales: { ...CHART_BASE.scales,
          y: { ...CHART_BASE.scales.y,
               title: { display: true, text: 'kWh per day', color: '#94a3b8' } },
        } }
    });
  } catch(e) { /* silently ignore */ }
}

document.querySelectorAll('.chart-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.chart-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentRange = parseInt(btn.dataset.range, 10);
    refreshCharts();
  });
});

fetchStatus();
startCountdown();
_wireHelpTips();
// Charts refresh independently — every 5 minutes is plenty
refreshCharts();
refreshDailyChart();
setInterval(refreshCharts, 5 * 60 * 1000);
setInterval(refreshDailyChart, 30 * 60 * 1000);
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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _DASHBOARD_BYTES)

        elif path == "/api/status":
            if self._plugin_ref is None:
                body = b'{"error":"plugin not ready"}'
            else:
                try:
                    data = self._plugin_ref.get_dashboard_data()
                    body = json.dumps(data).encode()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            self._send(200, "application/json", body)

        elif path == "/api/history":
            # Half-hourly slots for the last N hours (default 24h, max 168h).
            # Reads from the plugin's energy_timeseries.db SQLite store.
            hours = 24
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in qs.split("&"):
                if kv.startswith("hours="):
                    try:
                        hours = max(1, min(168, int(kv.split("=", 1)[1])))
                    except ValueError:
                        pass
            if self._plugin_ref is None:
                body = b'{"error":"plugin not ready"}'
            else:
                try:
                    data = self._plugin_ref.get_dashboard_history(hours=hours)
                    body = json.dumps(data).encode()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            self._send(200, "application/json", body)

        elif path == "/api/calendar":
            # Calendar-months summary for a specific year (default: current).
            year = ""
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in qs.split("&"):
                if kv.startswith("year="):
                    year = kv.split("=", 1)[1]
            if self._plugin_ref is None:
                body = b'{"error":"plugin not ready"}'
            else:
                try:
                    data = self._plugin_ref.get_dashboard_calendar(year or "")
                    body = json.dumps(data).encode()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            self._send(200, "application/json", body)

        elif path == "/api/years":
            # List of years with at least one daily-history record.
            if self._plugin_ref is None:
                body = b'{"error":"plugin not ready"}'
            else:
                try:
                    data = self._plugin_ref.get_dashboard_years()
                    body = json.dumps(data).encode()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            self._send(200, "application/json", body)

        elif path == "/api/daily":
            # Daily totals for the last N days (default 30, max 365)
            days = 30
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in qs.split("&"):
                if kv.startswith("days="):
                    try:
                        days = max(1, min(365, int(kv.split("=", 1)[1])))
                    except ValueError:
                        pass
            if self._plugin_ref is None:
                body = b'{"error":"plugin not ready"}'
            else:
                try:
                    data = self._plugin_ref.get_dashboard_daily(days=days)
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
