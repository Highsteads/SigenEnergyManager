---
title: When something goes wrong
nav_order: 8
---

# When something goes wrong

Each section starts with what you see, then what it means and what to do. For any problem, **Plugins → Sigenergy Manager → Run Self-Test (Check All Subsystems)** is a good first step — it checks each part in turn and says which one is not working.

## The Battery Manager shows "Modbus offline — holding"

The plugin has lost touch with the inverter, so it has stopped changing anything until the readings come back. **Modbus Connected** on the **Sigenergy Inverter** device reads false.

- Check **Inverter IP address** in the settings matches the address your router shows for the inverter. If the router has given it a new address, reserve one for it in the router.
- Check the inverter's local network connection is switched on — Sigenergy's setting is **ModBus TCP Server Enable**, in the installer part of the mySigen app.
- Check the Mac that runs Indigo and the inverter are on the same home network.

When the readings come back, the plugin carries on by itself.

## The Event Log says "No site coordinates configured"

The plugin has no position for your roof, so it has no solar forecast. Fill in **Site latitude** and **Site longitude** in the settings.

## The solar forecast is well out

- Describe your own arrays in **PV array specs**. Left blank, the plugin uses the arrays on my roof — the [Settings](settings.md) page shows the format.
- If you use the shared secrets file, check `LATITUDE` and `LONGITUDE` in it are your own position and not the `0.0` from the example file. The file wins over the settings.
- Give it a few weeks. Each night it compares the forecast with what the panels made and corrects for the difference, so it gets better with time.

## The tariff shows as unknown, or you get "Battery import held tonight"

The plugin cannot read your tariff or prices from Octopus, so it will not buy from the grid at a price it cannot see. The house draws from the grid as it needs to in the meantime.

- Check your **Octopus API key**, **Account number**, **Electricity MPAN** and **Electricity meter serial** in the settings, or the same lines in the shared secrets file.
- Choose **Plugins → Sigenergy Manager → Refresh All Data Now**, then **Show Current Tariff Rates**.
- If the Event Log says the tariff is not recognised, your tariff is one the plugin does not yet know. [Raise an issue on GitHub](https://github.com/Highsteads/SigenEnergyManager/issues) with the tariff's name.

## The tariff shows "(forced)"

**Plan as though the tariff were** is set in the settings. Set it back to **Automatic** unless you are trying out a tariff on purpose.

## The battery ran low overnight and did not charge, on Tracker or Flexible

That is by design. With the same price all day, charging the battery and then using it wastes energy for no saving, so the house draws straight from the grid instead. The plugin still tops the battery up to your overnight reserve. [Charging from the grid](charging.md) explains.

## Spare solar is not being sold

- Check **Enable grid export** is ticked.
- On a day forecast below **Hold export on days forecast below**, selling waits until the battery reaches **Hold export until SOC reaches**. **Banking First** on the **Battery Manager** shows when this is holding.
- During a storm warning, or for up to four hours after a power cut, selling is held off while the battery refills.
- If today's solar will not fill the battery, there is nothing spare to sell.

The **Decision Reason** on the **Battery Manager** says which of these applies.

## Something I started by hand was undone

The plugin decides afresh every minute, and a forced charge or sale that does not fit its plan can be undone. Run **Pause Battery Manager** first, and **Resume Battery Manager** when you are done.

## The Flux strategy does nothing

- Check all three boxes are ticked: **Use the Flux strategy**, **Commissioning is signed off** and **That import limit has been verified**.
- The strategy only runs when your Octopus account shows Flux for both buying and selling. The Event Log says when it cannot prove this, and why.
- It steps aside for Axle events, Saving Sessions, Happy Hours, storm warnings, power cuts, actions you run by hand and pausing, and says so in the Event Log.

## No Pushover messages arrive

- Check the Pushover plugin is installed and switched on in Indigo.
- Check **Pushover user/group key** in the settings, or `PUSHOVER_USER_TOKEN` in the shared secrets file.
- Check your quiet hours — ordinary messages wait until they end.

## The web page will not open from a phone or another computer

To start with, the plugin's own page only answers on the Mac that runs Indigo. Set **Dashboard access** to **Whole network**, click **Save**, then choose **Plugins → Sigenergy Manager → Show Web Dashboard Access** for the link, token and all.

## Axle's figures have stopped coming in

**Ledger Feed Health** on the **Axle VPP Monitor** warns when nothing new has arrived for the number of days you set.

- Check **Read Axle's settlement emails from Apple Mail on this Mac** or **Read your Axle account page directly** is ticked, and that Apple Mail on that Mac is collecting the mailbox Axle write to.
- The plugin reads Mail's own files, which macOS protects. Check Indigo has **Full Disk Access** in **System Settings → Privacy & Security**.
- **Show VPP Earnings Ledger** shows which events are still waiting to be paid.

## Still stuck?

Choose **Plugins → Sigenergy Manager → Show Plugin Info** and **Run Self-Test (Check All Subsystems)**, copy the lines they write to the Event Log, and post them on the [Indigo forum](https://forums.indigodomo.com) with a description of what you see. You can also [raise an issue on GitHub](https://github.com/Highsteads/SigenEnergyManager/issues).
