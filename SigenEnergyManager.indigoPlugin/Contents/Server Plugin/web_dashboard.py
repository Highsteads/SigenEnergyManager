#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    web_dashboard.py
# Description: Lightweight HTTP server serving live Sigenergy battery dashboard.
#              Runs on port 8179. Exposes / (HTML) and /api/status (JSON).
#              Started from plugin.startup(), stopped on plugin.shutdown().
# Author:      CliveS & Claude Opus 4.8
# Date:        26-06-2026
# Version:     1.4 (same-origin CORS, real HTTP error codes, JS null-safety)
# 1.3 — NaN/Infinity-safe JSON (one non-finite float no longer breaks the whole live
#       update); calendar view state (_calCurrentYear/_calYearsLoaded) hoisted to
#       <script> scope so the selected year tab survives the 5s refresh; Back link
#       host-relative (was a hardcoded LAN IP, Clive-only).

import http.server
import json
import logging
import math
import socketserver
import threading

DASHBOARD_PORT = 8179


def _json_safe(obj):
    """Recursively replace NaN/Infinity floats with None so the emitted JSON is
    valid. Default json.dumps writes bare NaN/Infinity tokens, which the browser's
    JSON.parse rejects — one non-finite float would take down the whole live update."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj

# ============================================================
# Embedded self-contained dashboard HTML
# ============================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<title>Sigenergy Monitor</title>
<style>
/* Sigenergy dashboard — iOS-style theme to match Dashboards plugin pages.
   v5.20: light/dark via prefers-color-scheme, flat cards, soft shadows,
   semantic energy colours preserved (solar amber, battery green, etc.).
   Class/ID names unchanged so existing HTML/JS keeps working. */
:root{
  --bg:           #f5f5f7;
  --card-bg:      #ffffff;
  --card-bg-alt:  #f2f2f7;
  --text:         #1d1d1f;
  --text-secondary:#86868b;
  --text-muted:   #98989d;
  --border:       #d2d2d7;
  --border-soft:  #e5e5ea;
  --accent:       #5856d6;
  --on-color:     #34c759;
  --off-color:    #d2d2d7;
  --bad:          #ff453a;
  --warn:         #ff9500;
  --shadow:       0 1px 3px rgba(0,0,0,0.08);
  --shadow-hover: 0 6px 18px rgba(0,0,0,0.10);

  /* Semantic energy colours — kept distinct from the iOS palette so the
     flow diagram is still readable. Each has a light + dark variant. */
  --solar:       #f5a623;
  --bat-charge:  #34c759;
  --bat-disch:   #af52de;
  --grid-imp:    #ff3b30;
  --grid-exp:    #007aff;
  --home-load:   #af52de;
  --info:        #007aff;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:           #000000;
    --card-bg:      #1c1c1e;
    --card-bg-alt:  #2c2c2e;
    --text:         #f5f5f7;
    --text-secondary:#98989d;
    --text-muted:   #6e6e72;
    --border:       #38383a;
    --border-soft:  #2c2c2e;
    --accent:       #7d7aff;
    --on-color:     #30d158;
    --off-color:    #48484a;
    --bad:          #ff453a;
    --warn:         #ff9f0a;
    --shadow:       0 1px 3px rgba(0,0,0,0.3);
    --shadow-hover: 0 6px 18px rgba(0,0,0,0.45);

    --solar:       #ffb340;
    --bat-charge:  #30d158;
    --bat-disch:   #bf5af2;
    --grid-imp:    #ff453a;
    --grid-exp:    #0a84ff;
    --home-load:   #bf5af2;
    --info:        #0a84ff;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{
  overflow-x:hidden;       /* clip any horizontal overflow at the viewport */
  width:100%;max-width:100vw;
}
body{
  background:var(--bg);
  color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif;
  font-size:14px;
  min-height:100vh;
  -webkit-overflow-scrolling:touch;
  /* Reserve space for the fixed header (title + status, ≈ 72 px + notch). */
  padding-top:max(80px, calc(72px + env(safe-area-inset-top)));
}
/* Defensive: grid/flex items can shrink below their content width on phones,
   preventing any one card from forcing the whole page wider than the viewport. */
.main > *, .right-panel > *, .bottom-row > * { min-width: 0; }
svg { max-width: 100%; }
header{
  display:grid;
  grid-template-columns:1fr 2fr 1fr;
  align-items:center;
  gap:8px;
  padding:10px max(16px,env(safe-area-inset-right)) 10px max(16px,env(safe-area-inset-left));
  padding-top:max(10px,env(safe-area-inset-top));
  background:var(--card-bg);
  border-bottom:1px solid var(--border-soft);
  /* Fixed (not sticky) so the header is pinned regardless of any parent
     overflow/scrolling-context quirks — matches the Dashboards plugin pages. */
  position:fixed;top:0;left:0;right:0;z-index:10;
  box-shadow:var(--shadow);
}
.hdr-left  { justify-self:start; display:flex; align-items:center; min-width:0; }
.hdr-right { justify-self:end;   display:flex; align-items:center; }
.hdr-center{
  justify-self:stretch;
  text-align:center;
  min-width:0;
  display:flex;
  flex-direction:column;
  align-items:center;
  gap:1px;
  line-height:1.15;
}
header h1{
  font-size:20px;font-weight:700;
  margin:0;letter-spacing:-.02em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:100%;color:var(--text);
}
.hdr-center .topbar-status{
  font-size:11px;color:var(--text-secondary);
  font-variant-numeric:tabular-nums;
  white-space:nowrap;
  max-width:100%;
  overflow:hidden;text-overflow:ellipsis;
}
.hdr-center .topbar-status .ts::before{
  content:'●';
  color:var(--on-color);
  margin-right:4px;
  animation:live-pulse 2.4s ease-in-out infinite;
}
@keyframes live-pulse{
  0%,100% { opacity:1; }
  50%     { opacity:0.4; }
}
.nav-btn{
  background:var(--card-bg);
  border:1px solid var(--border-soft);
  color:var(--accent);
  padding:8px 14px;
  border-radius:10px;
  font-size:14px;font-weight:600;
  cursor:pointer;
  font-family:inherit;
  text-decoration:none;
  display:inline-flex;align-items:center;gap:4px;
  min-height:36px;
  transition:transform .08s ease-out, background .15s, box-shadow .08s, border-color .15s;
  box-shadow:0 1px 2px rgba(0,0,0,0.08);
  user-select:none;
  -webkit-tap-highlight-color:transparent;
  white-space:nowrap;
}
.nav-btn:hover  { background:var(--card-bg-alt); border-color:var(--accent); }
.nav-btn:active {
  transform:translateY(2px) scale(0.96);
  background:var(--accent); color:#fff;
  box-shadow:0 0 0 rgba(0,0,0,0);
}
#alert-bar{
  display:none;padding:10px 16px;font-size:13px;font-weight:500;
  background:rgba(255,69,58,0.12);
  border-bottom:1px solid rgba(255,69,58,0.30);
  color:var(--bad);
}
#alert-bar.warn{
  background:rgba(255,149,0,0.12);
  border-color:rgba(255,149,0,0.30);
  color:var(--warn);
}
.main{
  padding:16px;display:grid;gap:12px;
  grid-template-columns:1fr 1fr;grid-template-rows:auto;
  max-width:1200px;margin:0 auto;
}
.card{
  background:var(--card-bg);
  border:1px solid var(--border-soft);
  border-radius:14px;padding:16px;
  box-shadow:var(--shadow);
  transition:box-shadow .25s ease, transform .25s ease;
}
.card:hover{box-shadow:var(--shadow-hover)}
.card h2{
  font-size:11px;font-weight:600;
  color:var(--text-secondary);
  text-transform:uppercase;letter-spacing:.6px;
  margin-bottom:12px;
}
/* --- flow card --- */
.flow-card{
  grid-column:1;grid-row:1;
  position:relative;overflow:hidden;
}
.flow-chips{
  position:absolute;top:14px;right:16px;
  display:flex;gap:6px;
  font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;
  z-index:2;
}
.flow-chip{
  padding:3px 10px;border-radius:999px;
  background:var(--card-bg-alt);
  border:1px solid var(--border-soft);
  color:var(--text-secondary);
}
.flow-chip.ok    {color:var(--on-color);border-color:rgba(52,199,89,0.30);background:rgba(52,199,89,0.10)}
.flow-chip.warn  {color:var(--warn);border-color:rgba(255,149,0,0.30);background:rgba(255,149,0,0.10)}
.flow-chip.alert {color:var(--bad);border-color:rgba(255,69,58,0.30);background:rgba(255,69,58,0.10)}
#flow-svg{width:100%;height:auto;position:relative;z-index:1}
/* --- right panel --- */
.right-panel{grid-column:2;grid-row:1;display:flex;flex-direction:column;gap:12px}
.bt-row{display:flex;gap:12px;flex-wrap:wrap}
.bt-row>*{flex:1 1 240px}
.soc-wrap{display:flex;align-items:center;gap:16px}
.soc-ring-wrap{flex-shrink:0;width:96px;height:96px}
.soc-ring-wrap svg{width:100%;height:100%}
.soc-info .soc-pct{font-size:32px;font-weight:700;color:var(--on-color);line-height:1}
.soc-info .soc-label{font-size:11px;color:var(--text-secondary);margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.soc-info .bat-pw{font-size:13px;margin-top:8px;color:var(--text)}
/* Battery tile direction colours — match the Grid tile palette:
   charging = export-blue, discharging = import-red. */
.soc-info .bat-pw.charging    { color: var(--grid-exp); }
.soc-info .bat-pw.discharging { color: var(--grid-imp); }
.soc-info .bat-pw.idle        { color: var(--text-muted); }
.stat-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat-box{
  background:var(--card-bg-alt);
  border-radius:10px;padding:12px 10px;text-align:center;
}
.stat-box .sb-label{font-size:10px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.6px}
.stat-box .sb-val{font-size:18px;font-weight:700;margin:6px 0 2px;color:var(--text)}
.stat-box .sb-sub{font-size:10px;color:var(--text-muted)}
/* --- forecast --- */
.forecast-card{grid-column:1 / -1}
.fc-meta{display:flex;gap:24px;margin-bottom:10px;font-size:12px;color:var(--text-secondary);flex-wrap:wrap}
.fc-meta strong{color:var(--text)}
#fc-svg{width:100%;height:auto}
/* --- bottom row --- */
.bottom-row{grid-column:1 / -1;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.dl{display:flex;flex-direction:column;gap:8px}
.dl-item{display:flex;justify-content:space-between;align-items:baseline;font-size:13px}
.dl-item .dk{color:var(--text-secondary)}
.dl-item .dv{font-weight:600;color:var(--text)}
.action-badge{
  display:inline-block;padding:4px 12px;border-radius:999px;
  font-size:11px;font-weight:600;margin-bottom:10px;text-transform:capitalize;
}
.action-self     {background:rgba(52,199,89,0.15);color:var(--on-color);border:1px solid rgba(52,199,89,0.30)}
.action-overflow {background:rgba(163,230,53,0.18);color:#7fad14;border:1px solid rgba(163,230,53,0.40)}
.action-export   {background:rgba(0,122,255,0.15);color:var(--grid-exp);border:1px solid rgba(0,122,255,0.30)}
.action-import   {background:rgba(255,69,58,0.15);color:var(--bad);border:1px solid rgba(255,69,58,0.30)}
.action-schedule {background:rgba(255,149,0,0.15);color:var(--warn);border:1px solid rgba(255,149,0,0.30)}
.action-unknown  {background:var(--card-bg-alt);color:var(--text-secondary);border:1px solid var(--border-soft)}
@media (prefers-color-scheme: dark){
  .action-overflow{color:#a3e635}
}
.reason{font-size:11px;color:var(--text-secondary);line-height:1.5;margin-top:6px;word-break:break-word}
.dawn-ok{color:var(--on-color)}
.dawn-warn{color:var(--bad)}
.tariff-rate{font-size:26px;font-weight:700;color:var(--warn)}
.tariff-sub{font-size:11px;color:var(--text-secondary);margin-top:2px}
.tariff-tmrw{margin-top:10px;font-size:13px;color:var(--text-secondary)}
.tariff-tmrw span{color:var(--text);font-weight:600}
.self-suff-bar{height:8px;background:var(--off-color);border-radius:4px;margin-top:6px;overflow:hidden}
.self-suff-fill{height:100%;background:var(--on-color);border-radius:4px;transition:width .4s}
/* --- semantic colours used by JS (energy flow / charts) --- */
.solar         {color:var(--solar)}
.bat-charge    {color:var(--bat-charge)}
.bat-discharge {color:var(--bat-disch)}
.grid-import   {color:var(--grid-imp)}
.grid-export   {color:var(--grid-exp)}
.home-load     {color:var(--home-load)}
.muted         {color:var(--text-muted)}
/* --- SVG flow direction arrows ---
   Two sets of 3 chevrons per line. Default both sets are hidden; the active
   set is revealed by the .flow-fwd / .flow-rev class set on the parent <g>
   by JS. Animation duration 1.2s, staggered by .4s per arrow so the 3 form
   a continuous stream. Travel range ±26px covers the visible part of each
   ~50px line and the fade-in/out hides the over-travel at the ends. */
.flow-arrows { transition: opacity .25s; }
.flow-arrows .fa-fwd,
.flow-arrows .fa-rev { display: none; }
.flow-arrows.flow-fwd .fa-fwd { display: inline; }
.flow-arrows.flow-rev .fa-rev { display: inline; }
.fa-arrow {
  transform-box: fill-box;
  transform-origin: center;
  opacity: 0;
  animation-iteration-count: infinite;
  animation-timing-function: linear;
  animation-duration: 1.2s;
}
.fa-arrow.s2 { animation-delay: -0.4s; }
.fa-arrow.s3 { animation-delay: -0.8s; }

/* CSS-animated flow dots (v5.25.2) — replaces SMIL animateMotion, which Safari
   freezes under macOS Reduce Motion / window occlusion (the Indigo Server Mac
   showed static dots while iPhone/MacBook animated). Non-opted-in CSS animations
   keep running, matching the old chevron mechanism that worked everywhere.
   All 4 legs are axis-aligned, so a per-leg translate vector (--tx/--ty) is enough. */
@keyframes flowdot {
  0%   { transform: translate(0px, 0px);                     opacity: 0; }
  15%  { opacity: 1; }
  85%  { opacity: 1; }
  100% { transform: translate(var(--tx,0px), var(--ty,0px)); opacity: 0; }
}
.fdot { transform-box: fill-box; transform-origin: center; animation: flowdot 1.4s linear infinite; }
.fdot.d2 { animation-delay: -0.47s; }
.fdot.d3 { animation-delay: -0.93s; }

@keyframes fa-y-fwd {
  0%   { transform: translateY(-26px); opacity: 0; }
  18%  { opacity: 1; }
  82%  { opacity: 1; }
  100% { transform: translateY(26px);  opacity: 0; }
}
@keyframes fa-y-rev {
  0%   { transform: translateY(26px);  opacity: 0; }
  18%  { opacity: 1; }
  82%  { opacity: 1; }
  100% { transform: translateY(-26px); opacity: 0; }
}
@keyframes fa-x-fwd {
  0%   { transform: translateX(-26px); opacity: 0; }
  18%  { opacity: 1; }
  82%  { opacity: 1; }
  100% { transform: translateX(26px);  opacity: 0; }
}
@keyframes fa-x-rev {
  0%   { transform: translateX(26px);  opacity: 0; }
  18%  { opacity: 1; }
  82%  { opacity: 1; }
  100% { transform: translateX(-26px); opacity: 0; }
}

/* Vertical lines (solar, grid) use the Y-axis keyframes */
#fl-solar.flow-fwd .fa-fwd .fa-arrow,
#fl-grid.flow-fwd  .fa-fwd .fa-arrow { animation-name: fa-y-fwd; }
#fl-solar.flow-rev .fa-rev .fa-arrow,
#fl-grid.flow-rev  .fa-rev .fa-arrow { animation-name: fa-y-rev; }

/* Horizontal lines (battery, home) use the X-axis keyframes */
#fl-bat.flow-fwd  .fa-fwd .fa-arrow,
#fl-home.flow-fwd .fa-fwd .fa-arrow { animation-name: fa-x-fwd; }
#fl-bat.flow-rev  .fa-rev .fa-arrow,
#fl-home.flow-rev .fa-rev .fa-arrow { animation-name: fa-x-rev; }
/* --- economics card --- */
.eco-rate{font-size:22px;font-weight:700;color:var(--on-color);font-variant-numeric:tabular-nums}
.eco-rate.eco-neg{color:var(--bad)}
.eco-sub{font-size:11px;color:var(--text-secondary);margin-top:2px}
.eco-divider{height:1px;background:var(--border-soft);margin:10px 0}
/* --- period totals table --- */
.period-card{grid-column:1 / -1}
.period-table{width:100%;border-collapse:collapse;font-size:12px}
.period-table th{
  text-align:right;color:var(--text-secondary);font-weight:600;
  padding:8px;border-bottom:1px solid var(--border-soft);
  font-size:11px;text-transform:uppercase;letter-spacing:.4px;
}
.period-table th:first-child{text-align:left}
.period-table td{
  text-align:right;padding:10px 8px;
  border-bottom:1px solid var(--border-soft);color:var(--text);
}
.period-table td:first-child{text-align:left;color:var(--text);font-weight:600}
.period-table td.tdays{color:var(--text-secondary);font-size:11px}
.period-table tr:last-child td{border-bottom:none}
.period-pos{color:var(--on-color)}
.period-neg{color:var(--bad)}
.period-table .avg{color:var(--text-muted);font-size:11px;display:block}
/* --- charts --- */
.chart-card{grid-column:1 / -1}
.chart-tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.chart-tab{
  background:var(--card-bg-alt);color:var(--text-secondary);
  border:1px solid var(--border-soft);border-radius:8px;
  padding:6px 14px;font-size:12px;cursor:pointer;font-family:inherit;
  min-height:32px;
  transition:background .15s, color .15s, border-color .15s;
}
.chart-tab:hover{color:var(--text)}
.chart-tab.active{
  background:var(--accent);color:#fff;border-color:var(--accent);
}
.chart-wrap{position:relative;height:220px;margin-bottom:16px}
.chart-wrap:last-child{margin-bottom:0}
/* Tabular numbers everywhere — stops digit jitter */
.soc-pct, .eco-rate, .tariff-rate, .sb-val, .dl-item .dv,
.period-table td{ font-variant-numeric:tabular-nums; }
/* Subtle fade-in on load */
.card{ animation:card-in .35s ease-out both }
.card:nth-child(2){ animation-delay:.04s }
.card:nth-child(3){ animation-delay:.08s }
.card:nth-child(4){ animation-delay:.12s }
.card:nth-child(5){ animation-delay:.16s }
@keyframes card-in{
  from{ opacity:0; transform:translateY(6px) }
  to  { opacity:1; transform:translateY(0) }
}
/* Skeleton shimmer */
.skel{
  display:inline-block;
  background:linear-gradient(90deg,var(--border-soft) 0%, var(--card-bg-alt) 50%, var(--border-soft) 100%);
  background-size:200% 100%;
  animation:skel-shim 1.4s linear infinite;
  border-radius:4px;color:transparent;
  min-width:3em;
}
@keyframes skel-shim{
  0%   { background-position: 200% 0 }
  100% { background-position: -200% 0 }
}
/* SOC ring — smooth transition, no glow */
#soc-ring{
  transition:stroke-dashoffset .8s cubic-bezier(.2,.7,.3,1), stroke .4s ease;
}
/* Forecast bar tooltip */
.fc-tip{
  position:fixed;pointer-events:none;z-index:50;
  background:var(--card-bg);
  border:1px solid var(--border);
  border-radius:10px;padding:8px 12px;
  box-shadow:0 8px 24px rgba(0,0,0,0.15);
  opacity:0;transform:translateY(4px);
  transition:opacity .15s ease, transform .15s ease;
  font-size:11px;line-height:1.3;
  min-width:90px;
}
@media (prefers-color-scheme: dark){
  .fc-tip{box-shadow:0 8px 24px rgba(0,0,0,0.5)}
}
.fc-tip.fc-tip-show{opacity:1;transform:translateY(0)}
.fc-tip .fc-tip-time{color:var(--text-secondary);font-size:10px;letter-spacing:.4px}
.fc-tip .fc-tip-kwh{
  color:var(--warn);font-size:14px;font-weight:700;
  font-variant-numeric:tabular-nums;
}
.fc-tip .fc-tip-unit{color:var(--text-muted);font-size:10px;font-weight:500;margin-left:1px}
.fc-bar{cursor:pointer}
.fc-bar rect:first-child{transition:opacity .12s ease}
/* Help tooltip on forecast-meta labels */
.fc-meta-item{
  cursor:help;position:relative;
  border-bottom:1px dotted var(--border);
  padding-bottom:1px;
  transition:color .15s, border-color .15s;
}
.fc-meta-item:hover{color:var(--text);border-bottom-color:var(--accent)}
.help-tip{
  position:fixed;pointer-events:none;z-index:60;
  max-width:300px;
  background:var(--card-bg);
  border:1px solid var(--border);
  border-radius:10px;padding:10px 14px;
  box-shadow:0 12px 32px rgba(0,0,0,0.15);
  opacity:0;transform:translateY(4px);
  transition:opacity .18s ease, transform .18s ease;
  font-size:12px;line-height:1.45;color:var(--text);
}
@media (prefers-color-scheme: dark){
  .help-tip{box-shadow:0 12px 32px rgba(0,0,0,0.5)}
}
.help-tip.help-tip-show{opacity:1;transform:translateY(0)}
.help-tip .help-tip-title{
  color:var(--accent);font-weight:600;font-size:11px;
  letter-spacing:.4px;text-transform:uppercase;
  margin-bottom:6px;
}
/* Sparkline (SOC card) — 24h trend */
.spark-wrap{margin-top:12px;height:36px;position:relative}
.spark-wrap svg{width:100%;height:100%;display:block}
.spark-wrap path.spark-fill{fill:url(#spark-grad);opacity:0.25}
.spark-wrap path.spark-line{fill:none;stroke:var(--on-color);stroke-width:1.5}
.spark-cap{font-size:10px;color:var(--text-secondary);margin-top:4px;text-align:right;letter-spacing:.4px;font-variant-numeric:tabular-nums}
/* --- responsive --- */
/* Tablet + narrow desktop: single-column layout.
   IMPORTANT: also reset grid-row to auto, otherwise the desktop CSS's
   `grid-row:1` on .flow-card + .right-panel keeps both in row 1 and they
   render ON TOP OF EACH OTHER. Re-flowing into auto rows fixes the overlap. */
@media(max-width:680px){
  .main{grid-template-columns:1fr;padding:12px}
  .flow-card,.right-panel,.forecast-card{grid-column:1;grid-row:auto}
  .bottom-row{grid-template-columns:1fr}
}
/* Phones (iPhone Pro Max and smaller): compact header, smaller fonts,
   tighter padding, header wraps to two rows if needed. */
@media(max-width:480px){
  header{padding:8px 12px;padding-top:max(8px,env(safe-area-inset-top))}
  header h1{font-size:15px;letter-spacing:-.02em}
  .hdr-right{font-size:11px;gap:4px;width:100%;justify-content:flex-start}
  .hdr-right .ts::before{margin-right:3px}
  .back-link{padding:4px 8px;font-size:13px;min-height:30px}
  .main{padding:8px;gap:8px}
  .card{padding:12px}
  .card h2{font-size:10px;margin-bottom:8px}
  .soc-info .soc-pct{font-size:28px}
  .stat-box .sb-val{font-size:16px}
  .tariff-rate{font-size:22px}
  .eco-rate{font-size:20px}
  /* SVG flow diagram already scales to 100% width via height:auto */
}
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <a class="nav-btn" href="#" onclick="location.href=location.protocol+'//'+location.hostname+':8176/public/dashboards/index.html';return false;">&larr; Back</a>
  </div>
  <div class="hdr-center">
    <h1>&#9889; Sigenergy Monitor</h1>
    <span class="topbar-status"><span class="ts">Updated <span id="ts">&#8212;</span></span> &middot; Next refresh <span id="cdwn">30</span>s</span>
  </div>
  <div class="hdr-right">
    <button class="nav-btn" onclick="window.scrollTo({top:0,behavior:'smooth'})" aria-label="Back to top">&uarr; Top</button>
  </div>
</header>

<div id="alert-bar"></div>

<main class="main">

  <!-- Power Flow -->
  <section class="card flow-card">
    <h2 data-help-title="Live Power Flow" data-help="Real-time animated diagram of where energy is moving right now. Solar panels → inverter → battery, home, and grid. Arrows show direction; thickness/speed scales with watts.">Live Power Flow</h2>
    <div class="flow-chips">
      <span id="chip-grid" class="flow-chip ok" data-help-title="Grid status" data-help="On Grid = AC line healthy. Grid Down = power cut in progress (the inverter is islanding the home from the grid). Lockout = post-event cool-down before re-syncing.">&#8212;</span>
      <span id="chip-mode" class="flow-chip" data-help-title="System mode" data-help="What the manager is doing right now: Self Consumption / Solar Overflow / Night Export / Import / VPP Active. Same source as the Manager Decision card.">&#8212;</span>
    </div>
      <svg id="flow-svg" viewBox="0 0 520 295" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <filter id="glow"><feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
        </defs>
        <!-- dim track lines (always visible) -->
        <line x1="260" y1="80"  x2="260" y2="128" stroke="rgba(128,128,128,0.20)" stroke-width="6" stroke-linecap="round"/>
        <line x1="260" y1="168" x2="260" y2="216" stroke="rgba(128,128,128,0.20)" stroke-width="6" stroke-linecap="round"/>
        <line x1="134" y1="148" x2="240" y2="148" stroke="rgba(128,128,128,0.20)" stroke-width="6" stroke-linecap="round"/>
        <line x1="280" y1="148" x2="386" y2="148" stroke="rgba(128,128,128,0.20)" stroke-width="6" stroke-linecap="round"/>

        <!-- Flowing dots: CSS-animated (.fdot) for cross-browser reliability
             (SMIL animateMotion froze under Safari Reduce-Motion / occlusion).
             4 legs x fwd/rev, pre-coloured, toggled by setDots() via group opacity.
             Diamond: Solar top, Home left, Grid right, Battery bottom. -->
        <g id="dot-solar-fwd" style="opacity:0;--tx:0px;--ty:48px">
          <circle class="fdot" cx="260" cy="80" r="4" fill="#f5a623"/>
          <circle class="fdot d2" cx="260" cy="80" r="4" fill="#f5a623"/>
          <circle class="fdot d3" cx="260" cy="80" r="4" fill="#f5a623"/>
        </g>
        <g id="dot-solar-rev" style="opacity:0;--tx:0px;--ty:-48px">
          <circle class="fdot" cx="260" cy="128" r="4" fill="#f5a623"/>
          <circle class="fdot d2" cx="260" cy="128" r="4" fill="#f5a623"/>
          <circle class="fdot d3" cx="260" cy="128" r="4" fill="#f5a623"/>
        </g>
        <g id="dot-home-fwd" style="opacity:0;--tx:-106px;--ty:0px">
          <circle class="fdot" cx="240" cy="148" r="4" fill="#af52de"/>
          <circle class="fdot d2" cx="240" cy="148" r="4" fill="#af52de"/>
          <circle class="fdot d3" cx="240" cy="148" r="4" fill="#af52de"/>
        </g>
        <g id="dot-home-rev" style="opacity:0;--tx:106px;--ty:0px">
          <circle class="fdot" cx="134" cy="148" r="4" fill="#af52de"/>
          <circle class="fdot d2" cx="134" cy="148" r="4" fill="#af52de"/>
          <circle class="fdot d3" cx="134" cy="148" r="4" fill="#af52de"/>
        </g>
        <g id="dot-bat-fwd" style="opacity:0;--tx:0px;--ty:-44px">
          <circle class="fdot" cx="260" cy="212" r="4" fill="#ff3b30"/>
          <circle class="fdot d2" cx="260" cy="212" r="4" fill="#ff3b30"/>
          <circle class="fdot d3" cx="260" cy="212" r="4" fill="#ff3b30"/>
        </g>
        <g id="dot-bat-rev" style="opacity:0;--tx:0px;--ty:44px">
          <circle class="fdot" cx="260" cy="168" r="4" fill="#007aff"/>
          <circle class="fdot d2" cx="260" cy="168" r="4" fill="#007aff"/>
          <circle class="fdot d3" cx="260" cy="168" r="4" fill="#007aff"/>
        </g>
        <g id="dot-grid-fwd" style="opacity:0;--tx:110px;--ty:0px">
          <circle class="fdot" cx="280" cy="148" r="4" fill="#007aff"/>
          <circle class="fdot d2" cx="280" cy="148" r="4" fill="#007aff"/>
          <circle class="fdot d3" cx="280" cy="148" r="4" fill="#007aff"/>
        </g>
        <g id="dot-grid-rev" style="opacity:0;--tx:-110px;--ty:0px">
          <circle class="fdot" cx="390" cy="148" r="4" fill="#ff3b30"/>
          <circle class="fdot d2" cx="390" cy="148" r="4" fill="#ff3b30"/>
          <circle class="fdot d3" cx="390" cy="148" r="4" fill="#ff3b30"/>
        </g>

        <!-- inverter hub -->
        <circle cx="260" cy="148" r="20" fill="rgba(255,255,255,0.0)" stroke="rgba(134,134,139,0.55)" stroke-width="2"/>
        <text x="260" y="152" text-anchor="middle" fill="#86868b" font-size="9" font-weight="600">INV</text>

        <!-- Solar node (top) -->
        <circle cx="260" cy="46" r="38" fill="rgba(255,255,255,0.0)" stroke="rgba(245,166,35,0.45)" stroke-width="1.5"/>
        <text x="260" y="41" text-anchor="middle" fill="#f5a623" font-size="11">&#9728; Solar</text>
        <text id="n-pv" x="260" y="59" text-anchor="middle" fill="#f5a623" font-size="14" font-weight="700">0 W</text>

        <!-- Home node (left) -->
        <circle cx="96" cy="148" r="38" fill="rgba(255,255,255,0.0)" stroke="rgba(175,82,222,0.45)" stroke-width="1.5"/>
        <text x="96" y="131" text-anchor="middle" fill="#af52de" font-size="11">&#127968; Home</text>
        <text id="n-home" x="96" y="153" text-anchor="middle" fill="#af52de" font-size="14" font-weight="700">0 W</text>

        <!-- Grid node (right) -->
        <circle cx="424" cy="148" r="38" fill="rgba(255,255,255,0.0)" stroke="rgba(0,122,255,0.45)" stroke-width="1.5"/>
        <text x="424" y="131" text-anchor="middle" fill="#007aff" font-size="11">&#9889; Grid</text>
        <text id="n-grid" x="424" y="153" text-anchor="middle" fill="#007aff" font-size="14" font-weight="700">0 W</text>
        <text id="n-grid-dir" x="424" y="171" text-anchor="middle" fill="#86868b" font-size="10">Idle</text>

        <!-- Battery node (bottom) -->
        <circle id="bat-rect" cx="260" cy="250" r="38" fill="rgba(255,255,255,0.0)" stroke="rgba(52,199,89,0.45)" stroke-width="1.5"/>
        <text id="bat-label" x="260" y="233" text-anchor="middle" fill="#34c759" font-size="11">&#128267; Battery</text>
        <text id="n-bat" x="260" y="255" text-anchor="middle" fill="#34c759" font-size="14" font-weight="700">0 W</text>
        <text id="n-soc" x="260" y="273" text-anchor="middle" fill="#86868b" font-size="10">0%</text>
      </svg>
  </section>

  <!-- Right panel: SOC + stats -->
  <div class="right-panel">
    <div class="bt-row">
    <div class="card">
      <h2 data-help-title="Battery State" data-help="Current battery state of charge as a % of usable capacity (35.04 kWh total). Ring colour: green ≥ 60%, amber 30–59%, red < 30%. Below the figure: charging / discharging power and direction.">Battery State</h2>
      <div class="soc-wrap">
        <div class="soc-ring-wrap">
          <svg viewBox="0 0 100 100">
            <circle cx="50" cy="50" r="42" fill="none" stroke="rgba(128,128,128,0.20)" stroke-width="10"/>
            <circle id="soc-ring" cx="50" cy="50" r="42" fill="none" stroke="#34c759" stroke-width="10"
              stroke-dasharray="263.9" stroke-dashoffset="263.9"
              stroke-linecap="round" transform="rotate(-90 50 50)"/>
          </svg>
        </div>
        <div class="soc-info">
          <div class="soc-pct" id="soc-pct">0%</div>
          <div class="soc-label" data-help-title="State of Charge" data-help="The percentage of usable battery capacity currently available. 100% = 35.04 kWh stored. The plugin manages discharge to keep the battery above the resilience floor (10% summer / 20% winter) for power-cut cover.">State of Charge</div>
          <div class="bat-pw" id="soc-pw">&#8212;</div>
        </div>
      </div>
      <div class="spark-wrap"><svg id="spark-soc" viewBox="0 0 200 36" preserveAspectRatio="none"></svg></div>
      <div class="spark-cap" id="spark-soc-cap">&#8212;</div>
    </div>
    <section class="card">
      <h2 data-help-title="Electricity Tariff" data-help="Today's import rate (top, big amber number, in pence per kWh) and tomorrow's. On Octopus Tracker the rate changes every day at midnight UK time and is published the day before. Refreshed every 30 minutes from the Octopus API.">Tariff</h2>
      <div class="tariff-rate"><span id="tar-rate">&#8212;</span>p</div>
      <div class="tariff-sub" id="tar-name">&#8212;</div>
      <div class="tariff-sub muted" id="tar-code">&#8212;</div>
      <div class="tariff-tmrw">Tomorrow: <span id="tar-tmrw">&#8212;</span>p</div>
    </section>
    </div>

    <div class="card">
      <h2 data-help-title="Live Power" data-help="Instantaneous watts at three points: solar production, net grid (positive=import, negative=export), and home consumption. Updates every 10 seconds with the Modbus poll.">Live Power</h2>
      <div class="stat-row">
        <div class="stat-box">
          <div class="sb-label" data-help-title="Solar (PV)" data-help="DC power being generated by all 4 PV strings combined, after MPPT conversion, in watts. Caps at the inverter limit (10 kW). Drops to 0 when in Discharge ESS First mode (e.g. during deliberate overnight export).">Solar</div>
          <div class="sb-val solar" id="s-pv">0</div>
          <div class="sb-sub">W</div>
        </div>
        <div class="stat-box">
          <div class="sb-label" data-help-title="Grid power" data-help="Net flow at the supply meter. Positive = importing from the grid (red); negative = exporting to the grid (cyan); zero = self-sufficient. Capped on export at the 4 kW DNO limit.">Grid</div>
          <div class="sb-val" id="s-grid">0</div>
          <div class="sb-sub" id="s-grid-dir">&#8212;</div>
        </div>
        <div class="stat-box">
          <div class="sb-label" data-help-title="Home load" data-help="Real-time household consumption — everything connected behind the meter (lights, appliances, EV charger, etc.) in watts. Sourced from the inverter's measurement and accumulated into the half-hourly profile that drives consumption forecasts.">Home</div>
          <div class="sb-val home-load" id="s-home">0</div>
          <div class="sb-sub">W</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Forecast chart -->
  <section class="card forecast-card">
    <h2 data-help-title="Solar Forecast — Today" data-help="Hour-by-hour expected solar production for the next 24 hours, in kWh per hour. Bars are amber for the current hour and green elsewhere; past hours are dimmed. Hover any bar for the exact value.">Solar Forecast &#8212; Today</h2>
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
      <h2 data-help-title="Manager Decision" data-help="What the battery manager is doing right now and why. Re-evaluated every 60 seconds based on SOC, time of day, tariff, and tomorrow's solar forecast.">Manager Decision</h2>
      <div id="action-badge" class="action-badge action-unknown" data-help-title="Current action" data-help="Self Consumption = standard mode. Solar overflow = exporting daytime PV surplus. Night Export = flood-prevention pre-drain before a sunny day. Starting Import = grid charging now (rare on Tracker). Import Scheduled = will charge at the next cheap window.">&#8212;</div>
      <div class="dl">
        <div class="dl-item">
          <span class="dk" data-help-title="Dawn viable" data-help="Will the battery still hold above the resilience floor (10%/20%) at next sunrise without grid help? True = no overnight import needed. False = battery would fall too low, so an import is scheduled or already running.">Dawn viable</span>
          <span class="dv" id="dec-dawn">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk" data-help-title="SOC at dawn" data-help="Projected battery state at next sunrise, in kWh. Calculated by stepping through tonight's hourly consumption profile from the current SOC. Compared to the dawn target to decide whether to import.">SOC at dawn</span>
          <span class="dv" id="dec-soc-dawn">&#8212;</span>
        </div>
      </div>
      <div class="reason" id="dec-reason">&#8212;</div>
    </section>

    <!-- Today summary -->
    <section class="card">
      <h2 data-help-title="Today's Summary" data-help="Running daily totals since local midnight. Resets at 00:00 UK time. Sourced from Modbus lifetime delta registers (PV/import/export) and the inverter's own daily home counter.">Today&#8217;s Summary</h2>
      <div class="dl">
        <div class="dl-item"><span class="dk" data-help-title="PV generated" data-help="Total solar production since local midnight, in kWh. From the inverter's lifetime PV register (delta from midnight snapshot), so accurate even after a plugin restart.">PV generated</span><span class="dv solar" id="sum-pv">&#8212;</span></div>
        <div class="dl-item"><span class="dk" data-help-title="Home used" data-help="Total household consumption since local midnight, in kWh. From the inverter's daily-home register. Note: was buggy until v5.4 — old daily history records were back-filled from Sigenergy cloud on 12-May-2026.">Home used</span><span class="dv home-load" id="sum-home">&#8212;</span></div>
        <div class="dl-item"><span class="dk" data-help-title="Grid import" data-help="Total energy bought from the grid since local midnight, in kWh. Should be near-zero on sunny days when self-sufficient. Multiplied by today's Tracker rate to give the import-cost figure on the Today's Cost card.">Grid import</span><span class="dv grid-import" id="sum-imp">&#8212;</span></div>
        <div class="dl-item"><span class="dk" data-help-title="Grid export" data-help="Total energy sold to the grid since local midnight, in kWh. Caps at the 4 kW DNO limit per hour. Multiplied by the Octopus Outgoing rate (12p) to give export revenue.">Grid export</span><span class="dv grid-export" id="sum-exp">&#8212;</span></div>
        <div class="dl-item"><span class="dk" data-help-title="Peak SOC today" data-help="Highest battery state of charge reached since local midnight. On a sunny day this should approach 100% (or the flood-prevention cap). On a poor day it may stay near the dawn level.">Peak SOC</span><span class="dv" id="sum-peak">&#8212;</span></div>
        <div class="dl-item"><span class="dk" data-help-title="Minimum SOC today" data-help="Lowest battery state of charge since local midnight. Should always stay above the resilience floor (10% summer / 20% winter). If it dipped below, the manager would have triggered an emergency import.">Min SOC</span><span class="dv" id="sum-min">&#8212;</span></div>
        <div class="dl-item"><span class="dk" data-help-title="Self-sufficiency" data-help="Percentage of today's home consumption met without grid import. Calculated as (home_kWh − import_kWh) / home_kWh × 100. 100% = grid drew zero. Bar fills from green proportionally.">Self-sufficiency</span><span class="dv" id="sum-ss">&#8212;</span></div>
      </div>
      <div class="self-suff-bar"><div class="self-suff-fill" id="ss-bar" style="width:0%"></div></div>
    </section>

    <!-- Today's Cost / Economics (v5.3) -->
    <section class="card">
      <h2 data-help-title="Today's Cost (so far)" data-help="Live financial picture for today using actual energy figures × today's rates. Headline figure is the 'solar benefit' — what the day would have cost without solar minus what it actually netted.">Today&#8217;s Cost (so far)</h2>
      <div class="eco-rate" id="eco-benefit" data-help-title="Solar benefit today" data-help="Total financial benefit of having solar today: the bill you'd have paid without solar minus the bill you actually paid (or plus the export revenue). = (home_kWh − import_kWh) × import_rate + export_kWh × export_rate. Goes red if it's net-negative (rare).">&#8212;</div>
      <div class="eco-sub">benefit from solar today</div>
      <div class="eco-divider"></div>
      <div class="dl">
        <div class="dl-item">
          <span class="dk grid-import" data-help-title="Import paid (today)" data-help="Money paid to the supplier for grid import so far today. = grid_import_kWh × today's Tracker rate (currently shown in the rates line below). Should be near £0.00 on sunny days.">Import paid</span>
          <span class="dv" id="eco-import">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk grid-export" data-help-title="Export earned (today)" data-help="Revenue from selling surplus to the grid so far today. = grid_export_kWh × Octopus Outgoing rate (12p/kWh flat). Capped at the 4 kW DNO export limit per hour.">Export earned</span>
          <span class="dv" id="eco-export">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted" data-help-title="Net grid today" data-help="Export earned minus import paid. Positive (green) = the grid paid you on net. Negative (red) = you paid the grid on net. Different from 'solar benefit' which also accounts for the home consumption you didn't have to buy.">Net grid today</span>
          <span class="dv" id="eco-net">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted" data-help-title="Without solar" data-help="What today's home consumption would have cost on grid alone. = home_kWh × today's import rate. The counterfactual baseline used to compute the headline solar benefit.">Without solar</span>
          <span class="dv" id="eco-nosolar">&#8212;</span>
        </div>
      </div>
      <div class="eco-sub muted" id="eco-rates" data-help-title="Today's rates" data-help="Import: Octopus Tracker rate that applies all of today (changes at midnight). Export: Octopus Outgoing flat rate (12p/kWh, live since 26-Mar-2026). Both rates are saved into the daily history record at midnight so historical economics use the actual rate that was in effect on each day.">&#8212;</div>
    </section>

    <!-- Yesterday's Cost (v5.4) -->
    <section class="card">
      <h2 data-help-title="Yesterday's Cost" data-help="Yesterday's full-day financial picture, using yesterday's actual rates (saved at midnight in the daily history). More meaningful than 'today so far' because it covers a full 24h cycle.">Yesterday <span class="eco-sub" id="eco-y-date" style="display:inline">&nbsp;</span></h2>
      <div class="eco-rate" id="eco-y-benefit" data-help-title="Solar benefit yesterday" data-help="Total financial benefit yesterday — same calculation as today's card but on a complete day. = (home − import) × yesterday's import rate + export × yesterday's export rate.">&#8212;</div>
      <div class="eco-sub">benefit from solar yesterday</div>
      <div class="eco-divider"></div>
      <div class="dl">
        <div class="dl-item">
          <span class="dk grid-import" data-help-title="Import paid (yesterday)" data-help="What yesterday's grid import cost at yesterday's Tracker rate.">Import paid</span>
          <span class="dv" id="eco-y-import">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk grid-export" data-help-title="Export earned (yesterday)" data-help="What yesterday's grid export earned at yesterday's Outgoing rate (12p flat).">Export earned</span>
          <span class="dv" id="eco-y-export">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted" data-help-title="Net grid (yesterday)" data-help="Yesterday's export earned minus import paid.">Net grid</span>
          <span class="dv" id="eco-y-net">&#8212;</span>
        </div>
        <div class="dl-item">
          <span class="dk muted" data-help-title="Without solar (yesterday)" data-help="What yesterday's home consumption would have cost on grid alone (home_kWh × yesterday's import rate).">Without solar</span>
          <span class="dv" id="eco-y-nosolar">&#8212;</span>
        </div>
      </div>
      <div class="eco-sub muted" id="eco-y-rates" data-help-title="Yesterday's rates" data-help="The actual import + export rates in force yesterday. Saved at midnight when the daily-history record was written, so they don't change retrospectively even if rates move tomorrow.">&#8212;</div>
    </section>

  </div>

  <!-- Period totals (v5.5) -->
  <section class="card period-card">
    <h2 data-help-title="Period totals" data-help="Roll-ups of the same five financial figures over three windows: rolling last 7 days, current calendar month so far, and rolling last 365 days. Each cell shows the period total with the per-day average underneath. Each historical day is valued at its own saved rate, not today's.">Period totals (and per-day average)</h2>
    <table class="period-table">
      <thead>
        <tr>
          <th data-help-title="Period window" data-help="Week = rolling last 7 calendar days. Month = current calendar month so far (variable day count). Year = rolling last 365 days (or however many days of history exist).">Period</th>
          <th data-help-title="Days included" data-help="Count of records actually averaged. Will be less than the window size for newer installs without enough history yet.">Days</th>
          <th data-help-title="Solar benefit" data-help="Total counterfactual saving + export revenue across the window. The headline number for 'is the system paying for itself'.">Solar benefit</th>
          <th data-help-title="Net grid" data-help="Export revenue minus import cost for the period. Positive = the grid paid you on net.">Net grid</th>
          <th data-help-title="Without solar" data-help="What the home consumption would have cost on grid alone over the window.">Without solar</th>
          <th data-help-title="Import paid" data-help="Total spent on grid import across the window.">Import paid</th>
          <th data-help-title="Export earned" data-help="Total earned from selling export across the window.">Export earned</th>
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
      <h2 style="margin-bottom:0" data-help-title="Calendar months" data-help="Per-month breakdown for the selected calendar year (Jan–Dec). Each row shows the month total with the per-day average underneath. Year tabs on the right switch between historical years; the current month is flagged 'partial'.">​<span id="cal-year">&#8212;</span> calendar months</h2>
      <div class="chart-tabs" id="cal-year-tabs"></div>
    </div>
    <table class="period-table">
      <thead>
        <tr>
          <th data-help-title="Calendar month" data-help="Jan–Dec for the selected year. Months with no data are shown but blank.">Month</th>
          <th data-help-title="Days with data" data-help="Number of complete daily records present for that month. Less than the days-in-month means the system was offline or wasn't installed for part of it.">Days</th>
          <th data-help-title="Solar benefit (month)" data-help="Total financial benefit of having solar for that month — saving + export revenue.">Solar benefit</th>
          <th data-help-title="Net grid (month)" data-help="Export revenue minus import cost for the month. Positive = grid paid you net.">Net grid</th>
          <th data-help-title="Without solar (month)" data-help="What the month's home consumption would have cost on grid alone.">Without solar</th>
          <th data-help-title="Import paid (month)" data-help="Total spent on grid import for the month.">Import paid</th>
          <th data-help-title="Export earned (month)" data-help="Total earned from grid export for the month.">Export earned</th>
        </tr>
      </thead>
      <tbody id="cal-tbody">
        <tr><td colspan="7" class="muted">&#8212;</td></tr>
      </tbody>
      <tfoot>
        <tr id="cal-total-row" style="border-top:2px solid rgba(128,128,128,0.20);font-weight:600">
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

  <!-- Export sync check — Sigenergy vs Octopus (v5.19) -->
  <section class="card period-card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h2 style="margin-bottom:0" data-help-title="Export sync — Sigenergy vs Octopus" data-help="Compares the daily export kWh measured by the inverter against the half-hourly readings settled by Octopus, for the last 7 fully-settled days (today, yesterday and the day before are skipped because Octopus typically takes 24–48 h to settle). A drift of more than ±5% on any day usually means missing slots on the Octopus side or a clock-skew issue.">Export sync &mdash; Sigenergy vs Octopus</h2>
      <div class="muted" style="font-size:11px" id="exp-sync-meta">&#8212;</div>
    </div>
    <table class="period-table">
      <thead>
        <tr>
          <th>Date</th>
          <th data-help-title="Sigenergy" data-help="Daily export kWh from the inverter (daily_history.json).">Sigenergy</th>
          <th data-help-title="Octopus" data-help="Sum of half-hourly export readings for that local day from the Octopus consumption API for the export MPAN.">Octopus</th>
          <th data-help-title="Diff (kWh)" data-help="Sigenergy minus Octopus, kWh. Positive = inverter measured more than Octopus has settled.">Diff (kWh)</th>
          <th data-help-title="Diff (%)" data-help="Diff as a percentage of the larger of the two values. ✓ if within ±5%, ⚠ otherwise.">Diff %</th>
          <th data-help-title="Slots" data-help="Number of half-hourly readings Octopus returned for that day. Should be 48 on a fully-settled day.">Slots</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="exp-sync-tbody">
        <tr><td colspan="7" class="muted">&#8212;</td></tr>
      </tbody>
    </table>
  </section>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>

  <!-- Charts row (v5.2) -->
  <section class="card chart-card">
    <h2 data-help-title="Recent history charts" data-help="Top: SOC over the selected window (24h / 48h / 7d) — line chart showing how the battery rose and fell. Bottom: stacked half-hourly energy bars (PV up, export up, import down as inflow, home down as use). Backed by the half-hourly SQLite log.">Last 24 hours</h2>
    <div class="chart-tabs">
      <button class="chart-tab active" data-range="24">24h</button>
      <button class="chart-tab" data-range="48">48h</button>
      <button class="chart-tab" data-range="168">7d</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-soc"></canvas></div>
    <div class="chart-wrap"><canvas id="chart-energy"></canvas></div>
  </section>

  <section class="card chart-card">
    <h2 data-help-title="Daily totals (last 30 days)" data-help="Per-day stacked bars for the last 30 days: solar production, export, import, home use. Read off daily_history.json. Useful for spotting cloudy stretches and verifying the system is performing across weeks.">Daily totals (last 30 days)</h2>
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
// Live Power Flow card: always kW, 2 decimals (e.g. 980 W -> 0.98 kW). v5.19.1
function fmtKw(w) {
  if (w === null || w === undefined) return '\u2014';
  return (w / 1000).toFixed(2) + ' kW';
}
function fmtKwh(v) { return v !== null && v !== undefined ? v.toFixed(1) + ' kWh' : '\u2014'; }

function setDots(leg, watts, forwardPositive) {
  const fwd = document.getElementById('dot-' + leg + '-fwd');
  const rev = document.getElementById('dot-' + leg + '-rev');
  if (!fwd || !rev) return;
  if (Math.abs(watts) < 30) { fwd.style.opacity = '0'; rev.style.opacity = '0'; return; }
  const goFwd = forwardPositive ? (watts > 0) : (watts < 0);
  fwd.style.opacity = goFwd ? '1' : '0';
  rev.style.opacity = goFwd ? '0' : '1';
}

function renderForecast(hourly) {
  const svg = document.getElementById('fc-svg');
  // Coerce every value to a finite kWh number up front — a null/non-numeric value
  // (e.g. a non-finite float serialised as JSON null) would otherwise make
  // kwh.toFixed throw and maxK become NaN, blanking the whole chart.
  const entries = Object.entries(hourly)
    .map(([k, v]) => [k, Number.isFinite(Number(v)) ? Number(v) : 0])
    .sort((a,b)=>a[0].localeCompare(b[0]));
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
    if (!Number.isFinite(hr)) return;   // skip a malformed hour key
    const bh  = Math.max(1, Math.round((kwh/maxK)*chartH));
    const x   = 8 + i*(bw+2);
    const y   = chartH - bh;
    const past   = hr < curHr;
    const curr   = hr === curHr;
    const col    = curr ? '#f5a623' : '#34c759';
    const opac   = past ? '0.32' : '1';
    out +=
      `<g class="fc-bar" data-hr="${hr}" data-kwh="${kwh.toFixed(2)}" data-curr="${curr ? '1':'0'}">`
      + `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="${col}" opacity="${opac}" rx="2"/>`
      + `<rect x="${x-1}" y="0" width="${bw+2}" height="${chartH}" fill="transparent"/>`
      + `<title>${hr.toString().padStart(2,'0')}:00 — ${kwh.toFixed(2)} kWh</title>`
      + `</g>`;
    if (hr % 2 === 0) {
      out += `<text x="${x+bw/2}" y="${labelY}" text-anchor="middle" fill="#86868b" font-size="9">${hr}</text>`;
    }
  });
  // x-axis line
  out += `<line x1="6" y1="${chartH}" x2="750" y2="${chartH}" stroke="rgba(128,128,128,0.20)" stroke-width="1"/>`;
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

// Calendar view state — hoisted to <script> scope so it persists across the 5s
// update() calls. It was redeclared inside update(), which reset the user's
// selected year tab and re-fetched /api/years on every refresh.
let _calCurrentYear = null;
let _calYearsLoaded = false;
function update(d) {
  if (d.error) { document.getElementById('ts').textContent = 'Error: ' + d.error; return; }
  document.getElementById('ts').textContent = d.timestamp || '\u2014';

  // Render alerts FIRST so a throw further down in this function can never suppress
  // the alert banner (the soc_at_dawn_kwh null bug used to abort the whole render).
  try { updateAlerts(d); } catch (e) {}

  // SOC ring
  const soc = d.battery ? d.battery.soc_pct : 0;
  const circ = 263.9;
  const offset = circ - (soc/100)*circ;
  const ring = document.getElementById('soc-ring');
  ring.style.strokeDashoffset = offset;
  ring.style.stroke = soc >= 60 ? '#34c759' : soc >= 30 ? '#f5a623' : '#ff3b30';
  tweenNumber(document.getElementById('soc-pct'), soc, {decimals:1, suffix:'%', duration:700});
  document.getElementById('soc-pct').style.color  = ring.style.stroke;
  const batW = d.battery ? d.battery.power_w : 0;
  const batDir = batW > 30 ? 'Charging ' : batW < -30 ? 'Discharging ' : 'Idle';
  const batCls = batW > 30 ? 'charging'  : batW < -30 ? 'discharging' : 'idle';
  const pwEl = document.getElementById('soc-pw');
  pwEl.textContent = batDir + (Math.abs(batW) > 30 ? fmtW(batW) : '');
  // Use CSS classes so dark mode and theming follow the Grid tile colours.
  pwEl.classList.remove('charging', 'discharging', 'idle');
  pwEl.classList.add(batCls);
  pwEl.style.color = '';   // clear any previously inlined colour

  // Flow SVG nodes
  const pvW   = d.solar ? d.solar.power_w : 0;
  const gridW = d.grid  ? d.grid.power_w  : 0;
  const homeW = d.home  ? d.home.load_w   : 0;
  // Live Power Flow card: kW with 2 decimals + state labels (v5.19.2 polish)
  document.getElementById('n-pv').textContent   = fmtKw(pvW);
  document.getElementById('n-soc').textContent  = soc.toFixed(1) + '%';
  // Battery: 'Charging' / 'Discharging' / 'Idle' next to the power figure.
  // Colour follows the Grid tile palette: charging = blue, discharging = red.
  let batState = 'Idle';
  if (batW >  30) batState = 'Charging';
  if (batW < -30) batState = 'Discharging';
  const batAbs = Math.abs(batW);
  const nBat = document.getElementById('n-bat');
  nBat.textContent =
    batState === 'Idle' ? 'Idle' : fmtKw(batAbs);
  // n-bat fill is set below by the batBoxCol logic (alongside rect, label, SoC%).
  document.getElementById('n-home').textContent = fmtKw(homeW);
  // Grid: big kW value (matches Home) with a separate Imp/Exp/Nil line below.
  let gridDir;
  if      (gridW >  30) gridDir = 'Import';
  else if (gridW < -30) gridDir = 'Export';
  else                  gridDir = 'Idle';
  document.getElementById('n-grid').textContent     = fmtKw(Math.abs(gridW));
  document.getElementById('n-grid-dir').textContent = gridDir;

  // Status chips (top-right of flow card)
  const chipGrid = document.getElementById('chip-grid');
  const chipMode = document.getElementById('chip-mode');
  if (chipGrid) {
    const pcOngoing    = d.power_cut && d.power_cut.ongoing;
    // Show "Lockout" only when export is ACTUALLY held off. During the post-cut
    // window a battery at/above the SOC floor keeps exporting (flood-prevention),
    // which is not a lockout from the user's point of view — show "On Grid".
    const pcSuppressed = d.power_cut && d.power_cut.export_suppressed;
    if (pcOngoing) {
      chipGrid.textContent = 'Grid Down';
      chipGrid.className   = 'flow-chip alert';
    } else if (pcSuppressed) {
      chipGrid.textContent = 'Lockout';
      chipGrid.className   = 'flow-chip warn';
    } else {
      chipGrid.textContent = 'On Grid';
      chipGrid.className   = 'flow-chip ok';
    }
  }
  if (chipMode) {
    const vppState = d.vpp && d.vpp.state;
    const action   = (d.decision && d.decision.action) || 'unknown';
    let modeLabel, modeClass;
    if (vppState && vppState !== 'idle' && vppState !== 'IDLE') {
      modeLabel = 'VPP ' + vppState.toLowerCase().replace(/_/g, ' ');
      modeClass = (vppState === 'VPP_ACTIVE' || vppState === 'vpp_active') ? 'warn' : '';
    } else {
      modeLabel = ACTION_LABELS[action] || action;
      modeClass = (action === 'self_consumption' || action === 'solar_overflow' || action === 'start_export')
                  ? 'ok'
                  : (action.indexOf('import') !== -1 ? 'warn' : '');
    }
    chipMode.textContent = modeLabel;
    chipMode.className   = 'flow-chip ' + modeClass;
  }
  // Resolve the iOS palette colours from CSS variables so dark mode follows.
  const _r       = getComputedStyle(document.documentElement);
  const _blue    = _r.getPropertyValue('--grid-exp').trim()    || '#007aff';
  const _red     = _r.getPropertyValue('--grid-imp').trim()    || '#ff3b30';
  const _green   = _r.getPropertyValue('--bat-charge').trim()  || '#34c759';
  const _muted   = _r.getPropertyValue('--text-muted').trim()  || '#86868b';

  // Flow-dot colours are baked into the dot sets (export blue / import red).

  // Battery arrows + box tint: blue when charging, red when discharging, green at idle.
  const batBoxCol     = batW > 30 ? _blue : batW < -30 ? _red : _green;
  const batBoxStroke  = batW > 30 ? 'rgba(0,122,255,0.45)'
                      : batW < -30 ? 'rgba(255,59,48,0.45)'
                      : 'rgba(52,199,89,0.45)';
  // (battery dot colour is baked into the dot sets: charge blue / discharge red)
  document.getElementById('bat-rect').setAttribute('stroke', batBoxStroke);
  document.getElementById('bat-label').style.fill = batBoxCol;
  document.getElementById('n-soc').style.fill     = batBoxCol;
  document.getElementById('n-bat').style.fill     = batBoxCol;

  // Animated flow lines
  // Solar: always flows toward inverter (down) when generating
  setDots('solar', pvW,   true);
  // Battery: >0 charging (flows toward inverter from battery side), <0 discharging (reversed)
  // Line goes battery→inverter; positive batW = charging = energy going FROM grid/PV INTO battery
  // So when charging (batW>0), flow goes from right to left (inverter→battery), i.e. reversed
  setDots('bat',   batW,  false);
  // Home: always flows away from inverter (always positive load)
  setDots('home',  homeW, true);
  // Grid: positive = import (flows toward inverter = forward), negative = export (reversed)
  setDots('grid',  gridW, false);

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
    // soc_at_dawn_kwh can arrive null (non-finite -> JSON null via _json_safe);
    // fmtKwh is null-safe, so this no longer throws and aborts the whole render.
    document.getElementById('dec-soc-dawn').textContent = fmtKwh(d.decision.soc_at_dawn_kwh);
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
    // self_suff may be null/non-numeric — coerce so the bar width is a real % and
    // the cell shows a dash rather than "null%".
    const ssNum = Number(s.self_suff);
    const ssOk  = Number.isFinite(ssNum);
    document.getElementById('sum-ss').textContent   = ssOk ? ssNum + '%' : '—';
    document.getElementById('ss-bar').style.width   = (ssOk ? Math.max(0, Math.min(100, ssNum)) : 0) + '%';
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
      ? '#ff3b30' : '#34c759';
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

  // (calendar state _calCurrentYear / _calYearsLoaded hoisted to <script> scope
  //  above so it survives the 5s refresh)
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
  // (updateAlerts is called at the top of update() so it can't be suppressed by a
  // render error further down.)
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
  soc:    '#34c759',
  pv:     '#f5a623',
  imp:    '#ff3b30',
  exp:    '#007aff',
  home:   '#af52de',
  grid:   '#86868b',
};

const CHART_BASE = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 200 },
  interaction: { mode: 'index', intersect: false },
  scales: {
    x: { ticks: { color: '#86868b', maxRotation: 0, autoSkipPadding: 20 },
         grid: { color: 'rgba(100,116,139,0.08)' } },
    y: { ticks: { color: '#86868b' },
         grid: { color: 'rgba(100,116,139,0.12)' } },
  },
  plugins: {
    legend: { labels: { color: '#1d1d1f', font: { size: 11 } } },
    tooltip: { backgroundColor: 'rgba(255,255,255,0.0)', borderColor: 'rgba(128,128,128,0.20)',
               borderWidth: 1, titleColor: '#5856d6',
               bodyColor: '#1d1d1f', padding: 8 },
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
               title: { display: true, text: 'SOC %', color: '#86868b' } },
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
               title: { display: true, text: 'kWh per slot', color: '#86868b' } },
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
               title: { display: true, text: 'kWh per day', color: '#86868b' } },
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

// Export sync (v5.19): pull /api/export-sync once on load and every hour.
async function refreshExportSync() {
  const tbody = document.getElementById('exp-sync-tbody');
  const meta  = document.getElementById('exp-sync-meta');
  if (!tbody) return;
  try {
    const r = await fetch('/api/export-sync');
    if (!r.ok) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">Could not load</td></tr>';
      meta.textContent = '';
      return;
    }
    const d = await r.json();
    if (!d.available) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">'
        + (d.reason || 'Disabled — set OCTOPUS_EXPORT_MPAN / OCTOPUS_EXPORT_SERIAL')
        + '</td></tr>';
      meta.textContent = '';
      return;
    }
    const rows = d.rows || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">No settled days available yet</td></tr>';
    } else {
      tbody.innerHTML = rows.map(row => {
        const sigen = (row.sigen_kwh   !== null && row.sigen_kwh   !== undefined)
                      ? row.sigen_kwh.toFixed(2)   : '—';
        const octo  = (row.octopus_kwh !== null && row.octopus_kwh !== undefined)
                      ? row.octopus_kwh.toFixed(2) : '—';
        const dkwh  = (row.diff_kwh    !== null && row.diff_kwh    !== undefined)
                      ? (row.diff_kwh >= 0 ? '+' : '') + row.diff_kwh.toFixed(2) : '—';
        const dpct  = (row.diff_pct    !== null && row.diff_pct    !== undefined)
                      ? (row.diff_pct >= 0 ? '+' : '') + row.diff_pct.toFixed(1) + '%' : '—';
        let badge = '<span class="muted">' + row.status + '</span>';
        if (row.status === 'ok')      badge = '<span style="color:#34c759">✓ in sync</span>';
        if (row.status === 'drift')   badge = '<span style="color:#ff3b30">⚠ drift</span>';
        if (row.status === 'unsettled') badge = '<span class="muted">unsettled</span>';
        if (row.status === 'fetch_error') badge = '<span style="color:#f5a623">fetch error</span>';
        if (row.status === 'no_sigen_record') badge = '<span class="muted">no record</span>';
        return '<tr>'
          + '<td>' + row.date + '</td>'
          + '<td>' + sigen + ' kWh</td>'
          + '<td>' + octo  + ' kWh</td>'
          + '<td>' + dkwh  + '</td>'
          + '<td>' + dpct  + '</td>'
          + '<td class="tdays">' + (row.slots || 0) + '</td>'
          + '<td>' + badge + '</td>'
        + '</tr>';
      }).join('');
    }
    const s = d.summary || {};
    const parts = [];
    if (s.days_compared)  parts.push(s.days_compared + 'd compared');
    if (s.days_unsettled) parts.push(s.days_unsettled + ' unsettled');
    if (s.avg_diff_pct !== null && s.avg_diff_pct !== undefined) {
      const sign = s.avg_diff_pct >= 0 ? '+' : '';
      parts.push('avg ' + sign + s.avg_diff_pct.toFixed(2) + '%');
    }
    if (s.worst) parts.push('worst ' + s.worst.date + ' ' +
                            (s.worst.diff_pct >= 0 ? '+' : '') +
                            s.worst.diff_pct.toFixed(1) + '%');
    if (parts.length === 0) parts.push('no data');
    meta.textContent = parts.join(' · ');
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">Error: ' + e + '</td></tr>';
  }
}

fetchStatus();
startCountdown();
_wireHelpTips();
// Charts refresh independently — every 5 minutes is plenty
refreshCharts();
refreshDailyChart();
refreshExportSync();
setInterval(refreshCharts, 5 * 60 * 1000);
setInterval(refreshDailyChart, 30 * 60 * 1000);
setInterval(refreshExportSync, 60 * 60 * 1000);   // hourly
</script>

<style>
/* Floating back-to-top button — mirrors the Dashboards plugin pages so
   long-scroll behaviour is consistent across both servers (Sigen 8179
   + Dashboards 8176/public). Hidden until scrolled > 200 px. */
.back-to-top {
    position: fixed;
    bottom: max(80px, calc(72px + env(safe-area-inset-bottom)));
    right: max(16px, env(safe-area-inset-right));
    width: 44px; height: 44px;
    border-radius: 50%;
    background: var(--accent, #5856d6); color: #fff;
    border: none; cursor: pointer;
    font-size: 22px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 14px rgba(0,0,0,0.25);
    opacity: 0; pointer-events: none;
    transition: opacity 0.25s, transform 0.15s;
    z-index: 9;
    font-family: inherit;
}
.back-to-top.visible { opacity: 0.9; pointer-events: auto; }
.back-to-top:hover   { opacity: 1; transform: translateY(-2px); }
.back-to-top:active  { transform: translateY(0); opacity: 0.85; }
</style>
<button class="back-to-top" onclick="window.scrollTo({top:0,behavior:'smooth'})" aria-label="Back to top">&uarr;</button>
<script>
(function() {
    const btn = document.querySelector(".back-to-top");
    if (!btn) return;
    const onScroll = () => {
        if (window.scrollY > 200) btn.classList.add("visible");
        else btn.classList.remove("visible");
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
})();
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

    def _query(self):
        return self.path.split("?", 1)[1] if "?" in self.path else ""

    def _int_param(self, name, default, lo, hi):
        for kv in self._query().split("&"):
            if kv.startswith(name + "="):
                try:
                    return max(lo, min(hi, int(kv.split("=", 1)[1])))
                except ValueError:
                    return default
        return default

    def _send_api(self, producer):
        """Run a data producer and send JSON with a meaningful HTTP STATUS, so the
        browser consumers' `!r.ok` guards actually fire on failure. Errors used to
        be returned as HTTP 200 with an {error} body, which those guards never caught
        (they then tried to render the error object as data)."""
        if self._plugin_ref is None:
            self._send(503, "application/json", b'{"error":"plugin not ready"}')
            return
        try:
            data = producer()
            self._send(200, "application/json", json.dumps(_json_safe(data)).encode())
        except Exception as exc:
            self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _DASHBOARD_BYTES)

        elif path == "/api/status":
            self._send_api(lambda: self._plugin_ref.get_dashboard_data())

        elif path == "/api/history":
            # Half-hourly slots for the last N hours (default 24h, max 168h).
            hours = self._int_param("hours", 24, 1, 168)
            self._send_api(lambda: self._plugin_ref.get_dashboard_history(hours=hours))

        elif path == "/api/calendar":
            # Calendar-months summary for a specific year. Validate the param to a
            # plain 4-digit year (URL-decoded) before passing it through, rather than
            # forwarding arbitrary query text to the plugin.
            import re as _re
            from urllib.parse import unquote as _unquote
            year = ""
            for kv in self._query().split("&"):
                if kv.startswith("year="):
                    year = _unquote(kv.split("=", 1)[1])
            if not _re.fullmatch(r"\d{4}", year or ""):
                year = ""
            self._send_api(lambda: self._plugin_ref.get_dashboard_calendar(year))

        elif path == "/api/years":
            self._send_api(lambda: self._plugin_ref.get_dashboard_years())

        elif path == "/api/daily":
            days = self._int_param("days", 30, 1, 365)
            self._send_api(lambda: self._plugin_ref.get_dashboard_daily(days=days))

        elif path == "/api/export-sync":
            self._send_api(lambda: self._plugin_ref.get_dashboard_export_sync())

        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Same-origin only: the dashboard page and its /api endpoints are served from
        # this same host:port (the Dashboards plugin iframes it), so no cross-origin
        # header is needed. The previous wildcard `*` let ANY browser page read this
        # server's energy/cost/SOC data — note the bind exposure is documented in the
        # README; this drops the browser-CORS surface to same-origin.
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
