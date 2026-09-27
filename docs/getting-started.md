---
title: Getting started
nav_order: 2
---

# Getting started

This takes about twenty minutes, most of it finding the numbers the plugin needs.

## What you need

- **Indigo 2025.2 or later**, on a Mac on the same home network as your Sigenergy inverter.
- **A Sigenergy inverter with its local network connection switched on.** Sigenergy call this setting **ModBus TCP Server Enable**, and it sits in the inverter's settings in the installer part of the mySigen app, so you may need to ask your installer to turn it on.
- **The inverter's network address** — the four numbers separated by dots, such as `192.168.1.49`, that your router's list of connected devices shows for it. Ask your router to keep giving the inverter the same address, which most routers call a **reserved address** or **DHCP reservation**.
- **Your roof's position** as latitude and longitude, which you can read off any online map by clicking on your house. The solar forecast needs it.
- **Your Octopus Energy details**, if you are with Octopus: your account number (it starts `A-`), your electricity meter's MPAN and serial number, and an API key, which you can create in the developer settings of your Octopus account online. Without these the plugin still reads the inverter and the forecast, but it cannot know your prices.
- **Optional:** the [Pushover plugin](https://www.indigodomo.com/pluginstore/) for alerts on your phone, an Email+ mail account in Indigo for power-cut emails, and an Axle account if you take part in Axle events.

The plugin fetches the solar forecast from [Open-Meteo](https://open-meteo.com), which is free and needs no account.

## 1. Install the plugin

1. Go to the [Releases page](https://github.com/Highsteads/SigenEnergyManager/releases/latest) and download `SigenEnergyManager.indigoPlugin.zip`
2. Unzip the downloaded file — you will get `SigenEnergyManager.indigoPlugin`
3. Double-click `SigenEnergyManager.indigoPlugin` — Indigo will install it automatically

Indigo asks whether to enable the plugin. Say yes. It installs the two extra Python packages it needs by itself the first time it starts.

## 2. Fill in the settings

Open **Plugins → Sigenergy Manager → Configure**. The dialog is long, but only a few boxes need filling to start with:

1. **Inverter IP address** — the inverter's network address.
2. **Site latitude** and **Site longitude** — your roof's position. Longitude is negative west of Greenwich, so most of Britain has a minus sign.
3. **PV array specs** — leave this blank for now. The [Settings](settings.md) page shows how to describe your own roof, which makes the forecast much better.
4. **Battery capacity (kWh)** — the total size of your battery.
5. **Inverter max charge power (kW)** — your inverter's rating.
6. Under **OCTOPUS ENERGY**, your **Octopus API key**, **Account number**, **Electricity MPAN** and **Electricity meter serial**, and your **Grid region**.
7. If you sell to the grid, tick **Enable grid export** and set **DNO export limit** to the export limit your installer set up for your house.

Leave everything else as it is and click **Save**. Every setting is explained on the [Settings](settings.md) page, including how to keep your Octopus details in one shared file instead of this dialog.

## 3. Add the devices

The plugin does its work whether or not you add any devices, but the devices are where you see what it is doing, and its actions and triggers need them. Add one of each:

1. In Indigo, choose **New Device**.
2. Set **Type** to **Sigenergy Manager**, then pick the model.
3. Click **Save**. None of them has any settings of its own.

| Add this | To see |
|---|---|
| **Sigenergy Inverter** | The battery level and the power going in and out, live |
| **Battery Manager** | What the plugin has decided and why |
| **Solar Forecast** | Today's and tomorrow's solar forecast |
| **Tariff Monitor** | Your tariff and today's prices |
| **Axle VPP Monitor** | Axle events and earnings — only if you use Axle |

The [Your devices](devices.md) page explains everything each one shows.

## 4. Check it works

Within a minute the **Sigenergy Inverter** device shows the battery level, and **Modbus Connected** in its states reads true. The **Battery Manager** device shows **Running**, and its **Decision Reason** state says in words what it is doing, for example that the battery will last until tomorrow's sun.

For a full check, choose **Plugins → Sigenergy Manager → Run Self-Test (Check All Subsystems)**. It writes a block to the Event Log with a line for each part — the inverter, Octopus, the solar forecast, Axle and Pushover — each saying whether it works.

If something is not right, the [When something goes wrong](troubleshooting.md) page goes through the usual causes.

## What happens next

At first the plugin plans from the daily usage figures in its settings, while it learns what your house really uses at each half hour of the day. From then on it plans from the last nine weeks of your own figures, so its plans follow the seasons. [How it works](how-it-works.md) explains the rest.
