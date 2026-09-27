---
title: The plugin menu
nav_order: 7
---

# The plugin menu

These are under **Plugins → Sigenergy Manager**. Most of them write their answer to the Indigo Event Log.

| Menu item | What it does |
|---|---|
| **Refresh All Data Now** | Fetches a new solar forecast and your Octopus prices now, and has the plugin decide again straight away. |
| **Show Manager Status** | A summary of everything now — the battery, the solar, the grid, the house, the forecast, your tariff and what the plugin has decided. |
| **Show Daily History (Last 7 Days)** | A line for each of the last seven days — solar made against the forecast, bought, sold, used, and whether the battery charged from the grid, sold, or ran an Axle event. |
| **Show Bank-First Export Report** | The last three weeks of holding back daytime selling on smaller days — each day's forecast, how long selling was held, how much it kept back, how full the battery got, and whether any solar was lost as a result. [Selling to the grid](selling.md) explains the hold. |
| **Show 90% vs 95% / Agile Shadow Comparison** | The last three weeks of the log-only comparison between your daytime charge target and the one you are thinking of, with the evening's imports priced on Tracker and Agile. Only filled in while **Log-only daytime-target comparison** is ticked. |
| **Show Current Tariff Rates** | Your tariff and its prices for today and tomorrow, buying and selling. |
| **Show Saving Sessions** | Whether you have joined Octopus Saving Sessions, how many Happy Hour tokens you hold, and each session still to come, with whether you are in it. It only reports — it never joins a session. |
| **Show VPP Status** | Whether an Axle event is due, and when. |
| **Show VPP Export Summary** | Today's Axle event, if there was one, and what it sold. |
| **Show VPP Earnings Ledger** | What Axle have paid you — in total, what you can draw now, and each event, with the energy Axle counted beside the energy the plugin measured. |
| **Import Axle Account Data...** | For filing Axle's figures by hand. Save the data from your Axle account page as a file named `vpp_axle_import.json` in the plugin's data folder, then choose this. If the file is not there, the Event Log says where to put it. |
| **Show Today's Energy Summary** | Today's solar, buying, selling and use, the battery's highest and lowest, and what the plugin is doing now. |
| **Toggle Debug Logging** | Turns detailed logging on or off, without opening the settings. |
| **Toggle Timestamps in Log (on/off)** | Every line the plugin writes to the log starts with the time to the thousandth of a second, which helps when lining events up. This turns that on or off. It stays as you leave it. |
| **Run Self-Test (Check All Subsystems)** | Checks each part in turn — which details it found in the shared secrets file, the inverter, Octopus, the solar forecast, Axle and Pushover — and says for each whether it works. Changes nothing. |
| **Show Power Cut Log** | The last 20 times the grid went and came back. |
| **Show Web Dashboard Access** | Where the plugin's own web page is listening, and the link to open it — with the access token in it, if you have opened it to the whole network. |
| **Open Web Dashboard** | Opens a dashboard in a web browser on the Mac that runs Indigo — the plugin's own page, or my Dashboards plugin's if your shared secrets file names your Indigo web server. The link also goes in the Event Log, to click from another Mac. |
| **Show Plugin Info** | Writes the plugin's version and details of your Mac and Indigo to the log, which is useful to include if you ask for help on the Indigo forum. |
