---
title: Home
nav_order: 1
---

# Sigenergy Manager for Indigo

This plugin lets [Indigo](https://www.indigodomo.com) run a Sigenergy solar and battery system for you. It reads the inverter over your home network every few seconds, and once a minute it decides what the battery should do next — run the house as normal, charge from the grid while the price is low, or sell spare energy — with one aim above all others: buy as little from the grid as it can.

It knows your Octopus Energy tariff, so it charges in the cheap hours and not the dear ones, and it works with Octopus Tracker, Flexible, Go, Intelligent Go, Flux, Intelligent Flux and Agile. It finds out which one you are on from your Octopus account, so there is nothing to choose.

I run it on my own house, with 14.25 kWp of solar in four arrays, a 10 kW Sigenergy inverter and 35.04 kWh of battery, on Octopus Flux.

## What it does for you

- **Works out every minute whether the battery will last until the sun comes back**, from the battery's level, a solar forecast for your own roof and what your house really uses at each time of day.
- **Charges from the grid only when it has to,** and then only in the cheapest hours your tariff offers. On a flat-rate tariff it lets the house draw straight from the grid instead, because charging the battery first would waste energy for no saving.
- **Keeps a reserve in the battery overnight** — 15% from April to September and 20% from October to March, as you set — so a power cut never finds it empty.
- **Sells spare solar in the day** without letting the battery miss its target, and on a smaller day waits until the battery is nearly full before it sells anything.
- **Makes room in the battery overnight** before a very sunny day, so the morning sun is not wasted.
- **On Octopus Flux**, fills the battery in the cheap 2am to 5am window with what the house will need, and sells what is truly spare between 4pm and 7pm, when Octopus pays the most for it.
- **Watches for power cuts and storm warnings.** It tells you by Pushover and email when the power goes and comes back, keeps more in the battery while a Met Office warning is in force, and holds off selling after a power cut until the battery has refilled.
- **Takes part in Octopus Saving Sessions** if you want it to — it can opt you in, sell from the battery during the session, book your Weekend Happy Hours, fill the battery with the free electricity, and check that Octopus pays back what it owes.
- **Runs Axle events** for you, if you are signed up with Axle, and keeps a record of what each one paid.
- **Shows it all in Indigo** — five devices with the battery, solar, prices and decisions, triggers for the moments that matter, and running totals and costs in Indigo variables.

## Where to go next

| If you want to... | Read |
|---|---|
| Install the plugin and get it running | [Getting started](getting-started.md) |
| Know what each device shows in Indigo | [Your devices](devices.md) |
| Understand how it decides what the battery does | [How it works](how-it-works.md) |
| Use its actions and triggers | [Actions and triggers](actions-and-triggers.md) |
| Know what every setting does | [Settings](settings.md) |
| Know what each item in the Plugins menu does | [The plugin menu](plugin-menu.md) |
| Sort out a problem | [When something goes wrong](troubleshooting.md) |
| See what changed in each version | [Version history](changelog.md) |

## Download

The latest version is always on the [Releases page](https://github.com/Highsteads/SigenEnergyManager/releases/latest).
