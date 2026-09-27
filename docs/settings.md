---
title: Settings
nav_order: 6
---

# Settings

The plugin has no device settings — all of them are in **Plugins → Sigenergy Manager → Configure**, in the sections below, in the order the dialog shows them. A change takes effect when you click **Save**.

## MODBUS CONNECTION

How the plugin reaches the inverter over your network.

| Setting | What it does |
|---|---|
| **Inverter IP address** | The inverter's network address, such as `192.168.1.49`. Without it the plugin cannot read or control the inverter, and says so in the Event Log. |
| **Modbus TCP port** | The port the inverter listens on. 502 to start with, which is Sigenergy's own. Leave it alone. |
| **Plant slave address** | 247 to start with, Sigenergy's own. Leave it alone. |
| **Inverter slave address** | 1 to start with. Only change it if your installer has set the inverter to a different address. |
| **Modbus poll interval** | How often the plugin reads the inverter — 5, 10, 15, 30, 60 or 120 seconds. 10 seconds to start with. The plugin still decides once a minute whatever this is set to. |

## SOLAR FORECAST

| Setting | What it does |
|---|---|
| **Site latitude** and **Site longitude** | Your roof's position, in degrees. Longitude is negative west of Greenwich. There is no starting value — without these the plugin writes an error to the Event Log and runs without a solar forecast or storm check. |
| **Location name** | Your town, used in storm warning messages. Blank shows "your area". |
| **PV array specs** | A description of each of your solar arrays, as one line of text. Blank uses the arrays on my roof, which will not match yours, so it is worth filling in. The format is below. |
| **Battery module size** | The size of one battery module, 8.76 kWh to start with, which is a SigenStor module. The plugin uses it to work out how many packs you have. |
| **Battery pack count** | How many battery packs you have. Blank works it out from **Battery capacity** and **Battery module size**. The inverter reports the average battery temperature and its hottest and coldest pack, and the pack count lets the plugin tell whether one pack is out of step with the rest. |
| **PV string names** | Names for the inverter's solar inputs, in the inverter's own order, separated by commas — for example `South, East, West, North East`. You can add each one's size after a colon, such as `South:4.275`. Blank calls them PV1, PV2 and so on. These are used by the dashboards. The inverter's input order may not match your arrays, so watch each input's morning and evening output on a clear day before you name them. |

### Describing your solar arrays

**PV array specs** takes a list with one entry for each array, all on one line, like this:

```
[{"name":"South","tilt":34,"azimuth":-13,"kwp":4.275,"shade":0.86},{"name":"West","tilt":32,"azimuth":77,"kwp":2.850,"shade":0.85}]
```

| Item | What it means |
|---|---|
| `name` | Any name you like. |
| `tilt` | The angle of the panels from flat, in degrees — 0 is flat, 90 is upright. |
| `azimuth` | The direction the panels face, in degrees: 0 is south, 90 is west, -90 is east and 180 is north. |
| `kwp` | The array's size in kWp. |
| `shade` | How much of the forecast the array really gets, from 0 to 1, allowing for shade from trees and buildings. 1 means no shade. |

If the line cannot be read, the plugin uses the built-in arrays and writes an error to the Event Log.

## OCTOPUS ENERGY

Needed for the plugin to know your tariff and prices, and for the costs. You can keep them in the shared secrets file instead — see [below](#keeping-your-details-in-one-file).

| Setting | What it does |
|---|---|
| **Octopus API key** | Your key from the developer settings of your Octopus account online. |
| **Account number** | Your Octopus account number, starting `A-`. |
| **Electricity MPAN** and **Electricity meter serial** | Your electricity meter's 13-digit MPAN and serial number, from your bill. |
| **Export MPAN** and **Export meter serial** | Optional. The MPAN of your export meter, which is different from the import one. With these, the plugin compares what the inverter says it sold each day with what Octopus recorded, for the last seven days Octopus have settled, and flags any day more than 5% apart. My [Dashboards plugin](https://github.com/Highsteads/Dashboards) shows this on its Export Sync card. |
| **Gas MPRN** and **Gas meter serial** | Optional. With these, the costs include your gas. Gas figures come through about a day late, so today's gas is an estimate until then. |
| **Gas calorific factor** | Optional. The kWh in each cubic metre of gas, used to turn a metric gas meter's reading into kWh. Blank uses about 11.19. For costs to the penny, use the figure on your Octopus bill. |
| **Gas meter reports in** | **Cubic metres** for most metric meters, or **kWh** for the smart meters that report kWh directly. |
| **Grid region** | Your electricity region, which sets the prices Octopus charge you and the Saving Sessions open to you. North East England to start with. |

## BATTERY MANAGER SETTINGS

| Setting | What it does |
|---|---|
| **Battery capacity (kWh)** | The total size of your battery. 35.04 kWh to start with. |
| **Round-trip efficiency** | How much of the energy put into the battery comes back out, as a percentage. 94% to start with. The plugin uses it to judge whether charging to use later is worth it. |
| **Inverter max charge power (kW)** | Your inverter's rating. 10 kW to start with. Charges and forced actions never go above it. |
| **Summer resilience buffer** | The overnight reserve from April to September, kept for a power cut. 15% to start with, and it cannot go lower. It is also the floor for Axle events and storm preparation. [Charging from the grid](charging.md) explains how each tariff tops it up. |
| **Winter resilience buffer** | The same reserve from October to March. 20% to start with. A storm warning can raise it further. |
| **Battery health cutoff floor** | The lowest level the battery may ever go to. 1% to start with. |
| **Weekday daily consumption** | What the house uses on a Tuesday to Friday, in kWh. 22 to start with. |
| **Monday daily consumption** | The same for a Monday. 22 to start with. |
| **Saturday daily consumption** and **Sunday daily consumption** | The same for each weekend day. 30 to start with. |

Leave the four daily figures alone unless you want to override them. The plugin measures what the house really uses, half hour by half hour, and works them out for itself. A figure you set more than 1 kWh away from its starting value is taken as you meaning it, and the plugin then uses your figure for that day.

## AWAY MODE

| Setting | What it does |
|---|---|
| **Use a separate profile when the house is empty** | Ticked, the plugin keeps a second pattern of use for the days the house is empty. Off to start with. |
| **Indigo variable name** | The Indigo variable your holiday schedules set — `true` means the house is empty. `Away` to start with. If the variable is missing or cannot be read, the house counts as occupied. |
| **Expected daily consumption when away** | What an empty house uses in a day, in kWh, used only until the plugin has learnt the real figure. 12 to start with. |

## GRID EXPORT

| Setting | What it does |
|---|---|
| **Enable grid export** | Ticked, the plugin may sell to the grid. Off to start with. Tick it once your export meter is registered with Octopus. |
| **DNO export limit** | The most your house may sell to the grid, in kW, as your installer set it up. 4.0 kW to start with. |

## AXLE VPP

[Axle events](axle.md) explains all of these.

| Setting | What it does |
|---|---|
| **Enable Axle VPP monitoring** | Ticked, the plugin runs Axle events. Off to start with. |
| **Axle API token** | The token Axle give you. |
| **Axle VPP payment rate** | What Axle pay for each kWh sold in an event, in pounds. £1.00 to start with. Only used for the earnings estimate. |
| **Read Axle's settlement emails from Apple Mail on this Mac** | Ticked, the plugin files what each event paid from Axle's emails. Off to start with. |
| **Read your Axle account page directly** | Ticked, the plugin reads your Axle account page using the sign-in link from Axle's latest email. Off to start with. |
| **Warn if the earnings figures are this many days stale** | 7 days to start with. 0 switches the warning off. |

## OCTOPUS SAVING SESSIONS

[Saving Sessions and Happy Hours](saving-sessions.md) explains all of these. Alerts for new sessions are always sent.

| Setting | What it does |
|---|---|
| **Opt in to Power Down sessions automatically** | Ticked, the plugin joins each Power Down for you. Off to start with. |
| **Export the battery during a Saving Session** | Ticked, the plugin sells from the battery during a session you have joined. Off to start with. |
| **Charge the battery during a Weekend Happy Hour** | Ticked, the plugin charges the battery with the free electricity during an hour you have booked. Off to start with. |
| **Book Weekend Happy Hours automatically** | Ticked, the plugin books your Happy Hours for you. Off to start with, and it needs the charging box ticked too. |
| **Tokens needed to book a Happy Hour** | 2 to start with. 0 stops the messages mentioning tokens at all. |
| **Happy Hours you expect to use** | Blank to start with, which counts every hour left before the offer ends. A smaller number stops the plugin joining sessions for tokens you would never spend. |

## WEB DASHBOARD

The plugin has a small web page of its own, on port 8179 of the Mac that runs Indigo, showing the flow of power, the battery, what the plugin is doing and why, and today's totals. It is useful when the rest of your dashboards are down. My [Dashboards plugin](https://github.com/Highsteads/Dashboards) reads the same figures for its Energy and Cost pages.

| Setting | What it does |
|---|---|
| **Dashboard access** | **This machine only** to start with, which is right for almost everyone, and the Dashboards plugin still works. Choose **Whole network** to open the page from a phone, tablet or another computer. That needs an access token. |
| **Access token** | Blank makes the plugin create a strong token for you, stored where only your user account can read it. **Plugins → Sigenergy Manager → Show Web Dashboard Access** gives you the link with the token in it. |
| **Dashboard host** | Only changes the address the plugin gives out for the page. Blank finds the Mac's own address. |

Never open port 8179 to the internet through your router. To see the page away from home, use Tailscale or a Cloudflare Tunnel.

## PUSHOVER NOTIFICATIONS

Messages go through the Pushover plugin for Indigo, which must be installed and switched on.

| Setting | What it does |
|---|---|
| **Pushover user/group key** | The key messages go to. Without it, no messages are sent, and the Event Log says so. |
| **Notification sound** | The sound for messages. **vibrate** to start with, which buzzes without a sound. |
| **Quiet hours start** and **Quiet hours end** | Times such as `22:00` and `07:00`. Between them, ordinary messages wait. Urgent ones — an amber or red storm warning, or an Axle event that has gone wrong — always go. Blank means no quiet hours. |

## POWER-CUT NOTIFICATIONS

[Power cuts and storms](power-cuts-and-storms.md) explains these.

| Setting | What it does |
|---|---|
| **Notify on grid lost / restored** | Ticked to start with. Sends a message when the power goes and when it comes back. |
| **Email recipient** | An email address for the same messages, sent through your Email+ mail account. Blank sends Pushover only. |
| **Export lockout SOC floor** | After a power cut, selling is held off only while the battery is below this. 85% to start with. |
| **Lockout solar-refill minimum SOC** | Below this, selling stays held off after a power cut however good the solar forecast looks. 50% to start with. |

## DAYTIME SOLAR EXPORT

[Selling to the grid](selling.md) explains these. The two "hold export" settings decide when daytime selling starts. The charge target decides how the solar is split between battery and grid once it has started.

| Setting | What it does |
|---|---|
| **Daytime charge target** | The level the plugin paces the battery to reach by the end of the day while it sells spare solar. 90% to start with. A higher target sells less in the morning and still meets the afternoon with less room to spare, so aiming at 100% does not help. |
| **Daytime charge target floor** | A safety net, so a target set too low never leaves less than this for a power cut. 80% to start with. |
| **Hold export on days forecast below** | On a day forecast to make less than this, in kWh, daytime selling waits until the battery reaches the level below. 40 to start with. 0 switches the hold off. Clearing the box does not switch it off, it goes back to 40. |
| **Hold export until SOC reaches** | The battery level daytime selling waits for on those smaller days. 95% to start with, and never more than 97%, since the battery reaches a true 100% on only a handful of days a year. |
| **Log-only daytime-target comparison** | Ticked to start with. Records each day what a different charge target would have done. Nothing on the inverter changes. |
| **Compare the charge target against** | The target to compare with. 90% to start with. It must differ from **Daytime charge target**, or there is nothing to compare. |
| **Storm export release SOC** | During a storm warning, selling is held off until the battery reaches this. 85% to start with, and never below the storm reserve. |

## TARIFF OVERRIDE

| Setting | What it does |
|---|---|
| **Plan as though the tariff were** | **Automatic** to start with, which reads your tariff from your Octopus account. Choosing a tariff here makes the plugin plan as if you were already on it, against its real published prices — for trying one out before you switch. It does not change what Octopus charge you. The battery then makes decisions for a tariff you are not on, so use it when the battery is not charging from the grid anyway, and set it back to **Automatic** afterwards. While it is set, the tariff shows as **(forced)** and the Event Log warns at every start and every save. |

## OCTOPUS FLUX STRATEGY

[Octopus Flux](flux.md) explains all of these.

| Setting | What it does |
|---|---|
| **Use the Flux strategy** | Off to start with. |
| **Commissioning is signed off** | Off to start with. Both this and the box above must be ticked before the strategy does anything. |
| **Keep this much battery back at all times** | 20% to start with. |
| **Battery wear to charge each traded kWh** | 5p to start with. |
| **Most the site can safely import** | 10 kW to start with. |
| **That import limit has been verified** | Off to start with. Until it is ticked, the strategy will not plan a grid charge. |

## The last box

| Setting | What it does |
|---|---|
| **Enable debug logging** | Writes far more detail to the Event Log. Only useful when chasing a problem. **Plugins → Sigenergy Manager → Toggle Debug Logging** does the same without opening this dialog. |

## Keeping your details in one file

If you run several of my plugins, you can keep your private details — keys, account numbers, addresses — in one shared file instead of typing them into each plugin. The file is called `IndigoSecrets.py` and lives in `/Library/Application Support/Perceptive Automation/`.

To set it up, find `IndigoSecrets_example.py` inside the plugin — right-click `SigenEnergyManager.indigoPlugin`, choose **Show Package Contents**, and it is in the top folder. Copy it into `/Library/Application Support/Perceptive Automation/`, rename the copy `IndigoSecrets.py`, and fill in the lines you use. **When the file holds a value, the plugin uses it, whatever the Configure dialog says.**

This plugin reads these lines from the file:

| Line | Same as |
|---|---|
| `SIGENERGY_IP` | **Inverter IP address** |
| `LATITUDE` and `LONGITUDE` | **Site latitude** and **Site longitude** |
| `OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT` | **Octopus API key**, **Account number** |
| `OCTOPUS_MPAN`, `OCTOPUS_SERIAL` | **Electricity MPAN**, **Electricity meter serial** |
| `OCTOPUS_EXPORT_MPAN`, `OCTOPUS_EXPORT_SERIAL` | **Export MPAN**, **Export meter serial** |
| `OCTOPUS_GAS_MPRN`, `OCTOPUS_GAS_SERIAL` | **Gas MPRN**, **Gas meter serial** |
| `AXLE_API_KEY` | **Axle API token** |
| `PUSHOVER_USER_TOKEN` | **Pushover user/group key** |
| `POWERCUT_EMAIL` | the power-cut **Email recipient** |
| `SIGEN_DASHBOARD_TOKEN` | **Access token** |
| `DASHBOARD_HOST` | **Dashboard host** |

The example file leaves `LATITUDE` and `LONGITUDE` empty. Left empty, or both at `0.0`, they count as not set and the plugin uses **Site latitude** and **Site longitude** from the dialog.

The **Run Self-Test** menu item lists which of these the plugin found in the file.
