#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    sigenergy_modbus.py
# Description: Sigenergy inverter Modbus TCP client - reads all registers
#              and controls battery via Remote EMS
# Author:      CliveS & Claude Sonnet 4.6
# Date:        26-03-2026 15:30 GMT
# Version:     1.0
#
# Register map verified against Sigenergy Modbus Protocol V2.8 (2025-11-28)
# Adapted from SigenergySolar v3.1 sigenergy_modbus.py
# Changes from SigenergySolar version:
#   - Added set_export_limit(watts) wrapper for register 40038-39
#   - Fixed read_discharge_cutoff() bugs (throttle, address reference, register offset)
#   - Updated logger name to SigenEnergyManager

import logging
import time
from datetime import datetime

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException, ConnectionException
    PYMODBUS_AVAILABLE = True
except ImportError:
    PYMODBUS_AVAILABLE = False


# ============================================================
# Constants - Sigenergy Modbus Register Map V2.8
# Reference: Sigenergy Modbus Protocol V2.8 (2025-11-28)
# ============================================================

# --- Plant registers (slave address 247, read-only, function 0x03) ---

PLANT_EMS_WORK_MODE        = 30003    # U16: 0=Max self consumption, 1=AI, 2=TOU, 7=Remote EMS
PLANT_GRID_SENSOR_STATUS   = 30004    # U16: 0=not connected, 1=connected
PLANT_GRID_ACTIVE_POWER    = 30005    # S32 (2 regs), gain 1000, kW. >0=import, <0=export
PLANT_ON_OFF_GRID_STATUS   = 30009    # U16: 0=on-grid, 1=off-grid(auto), 2=off-grid(manual)
PLANT_BATTERY_SOC          = 30014    # U16, gain 10, %
PLANT_PV_POWER             = 30035    # S32 (2 regs), gain 1000, kW
PLANT_ESS_POWER            = 30037    # S32 (2 regs), gain 1000, kW. >0=charge, <0=discharge
PLANT_RUNNING_STATE        = 30051    # U16: 0=Standby, 1=Running, 2=Fault, 3=Shutdown
PLANT_ESS_DISCHARGE_CUTOFF = 30086    # U16, gain 10, %
PLANT_ESS_SOH              = 30087    # U16, gain 10, % (weighted average)
PLANT_PV_TOTAL_KWH         = 30088    # U64 (4 regs), gain 100, kWh — LIFETIME PV generation
PLANT_LOAD_DAILY_KWH       = 30092    # U32 (2 regs), gain 100, kWh — daily reset at midnight
PLANT_TOTAL_IMPORT_KWH     = 30216    # U64 (4 regs), gain 100, kWh — LIFETIME grid import
PLANT_TOTAL_EXPORT_KWH     = 30220    # U64 (4 regs), gain 100, kWh — LIFETIME grid export

# --- Inverter registers (slave address 1-246, read-only, function 0x03) ---

INV_DAILY_CHARGE_ENERGY    = 30566    # U32 (2 regs), gain 100, kWh
INV_DAILY_DISCHARGE_ENERGY = 30572    # U32 (2 regs), gain 100, kWh
INV_BATTERY_AVG_TEMP       = 30603    # S16, gain 10, degC
INV_BATTERY_AVG_VOLTAGE    = 30604    # U16, gain 1000, V
INV_BATTERY_MAX_TEMP       = 30620    # S16, gain 10, degC
INV_BATTERY_MIN_TEMP       = 30621    # S16, gain 10, degC

# --- Plant holding registers (slave address 247, read/write) ---
# Read with function 0x04, write single with 0x06, write multiple with 0x10

HOLD_REMOTE_EMS_ENABLE     = 40029    # U16 RW: 0=disabled, 1=enabled
HOLD_REMOTE_EMS_MODE       = 40031    # U16 RW: Remote EMS control mode (Appendix 6)
HOLD_ESS_MAX_CHARGE        = 40032    # U32 RW (2 regs), gain 1000, kW. Mode 3 or 4
HOLD_ESS_MAX_DISCHARGE     = 40034    # U32 RW (2 regs), gain 1000, kW. Mode 5 or 6
HOLD_GRID_MAX_EXPORT_LIMIT = 40038    # U32 RW (2 regs), gain 1000, kW. Requires grid sensor.
HOLD_GRID_MAX_IMPORT_LIMIT = 40040    # U32 RW (2 regs), gain 1000, kW.
HOLD_ESS_BACKUP_SOC        = 40046    # U16 RW, gain 10, % - backup reserve SOC
HOLD_ESS_CHARGE_CUTOFF     = 40047    # U16 RW, gain 10, % - max charge SOC
HOLD_ESS_DISCHARGE_CUTOFF  = 40048    # U16 RW, gain 10, % - min discharge SOC (reserve protection)

# --- EMS work modes (register 30003) ---

EMS_MODES = {
    0: "Max Self Consumption",
    1: "AI Mode",
    2: "TOU",
    5: "Full Feed-in to Grid",
    7: "Remote EMS",
    9: "Custom",
}

# --- Remote EMS control modes (register 40031, Appendix 6) ---

REMOTE_EMS_MODES = {
    0x00: "PCS Remote Control",
    0x01: "Standby",
    0x02: "Max Self Consumption",
    0x03: "Charge Grid First",
    0x04: "Charge PV First",
    0x05: "Discharge PV First",
    0x06: "Discharge ESS First",
}

PLANT_RUNNING_STATES = {
    0x00: "Standby",
    0x01: "Running",
    0x02: "Fault",
    0x03: "Shutdown",
}

GRID_STATUSES = {
    0: "On-grid",
    1: "Off-grid (auto)",
    2: "Off-grid (manual)",
}

# Protocol timing - 1000ms minimum between requests per Sigenergy V2.8 spec
MIN_REQUEST_INTERVAL = 1.0


class SigenergyModbus:
    """Modbus TCP client for Sigenergy inverter.

    Reads from two slave addresses:
      - Plant address (247): aggregated system data
      - Inverter address (1-246): individual inverter + battery data

    Writes to plant address (247) via Remote EMS control registers.

    Sign conventions:
      gridPowerWatts:    >0 = importing from grid, <0 = exporting to grid
      batteryPowerWatts: >0 = charging, <0 = discharging
      pvPowerWatts:      always >= 0
      homePowerWatts:    always >= 0 (calculated: PV + Grid - Battery)
    """

    def __init__(self, ip, port=502, plant_address=247, inverter_address=1, logger=None):
        self.ip               = ip
        self.port             = port
        self.plant_address    = plant_address
        self.inverter_address = inverter_address
        self.logger           = logger or logging.getLogger("SigenEnergyManager.Modbus")
        self.client           = None
        self._connected       = False
        self._last_connect_attempt = 0
        self._reconnect_delay      = 30
        self._last_request_time    = 0.0

    @property
    def connected(self):
        return self._connected

    # ================================================================
    # Connection Management
    # ================================================================

    def connect(self):
        """Connect to the Sigenergy inverter via Modbus TCP."""
        if not PYMODBUS_AVAILABLE:
            self.logger.error("pymodbus not installed - cannot connect to inverter")
            return False

        now = time.time()
        if now - self._last_connect_attempt < self._reconnect_delay:
            return False

        self._last_connect_attempt = now

        try:
            if self.client:
                self.client.close()

            self.client = ModbusTcpClient(
                host=self.ip,
                port=self.port,
                timeout=10,
                retries=3,
            )

            result = self.client.connect()
            if result:
                self._connected = True
                self.logger.info(
                    f"Connected to Sigenergy at {self.ip}:{self.port} "
                    f"(plant={self.plant_address}, inverter={self.inverter_address})"
                )
                return True
            else:
                self._connected = False
                self.logger.warning(f"Failed to connect to inverter at {self.ip}:{self.port}")
                return False

        except Exception as e:
            self._connected = False
            self.logger.error(f"Modbus connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from the inverter."""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self._connected = False
        self.logger.info("Disconnected from Sigenergy inverter")

    # ================================================================
    # Request Throttling
    # ================================================================

    def _throttle(self):
        """Enforce 1000ms minimum between Modbus requests per protocol spec."""
        elapsed = time.time() - self._last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    # ================================================================
    # Low-Level Read Primitives (function 0x03 - holding registers)
    # ================================================================

    def _read_int16(self, register, slave=None):
        """Read a signed 16-bit register."""
        if slave is None:
            slave = self.plant_address
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=1, device_id=slave
            )
            if result.isError():
                self.logger.debug(f"Error reading reg {register} (slave {slave}): {result}")
                return None
            value = result.registers[0]
            if value >= 32768:
                value -= 65536
            return value
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error reg {register} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(f"Unexpected read error reg {register} (slave {slave}): {e}")
            return None

    def _read_uint16(self, register, slave=None):
        """Read an unsigned 16-bit register."""
        if slave is None:
            slave = self.plant_address
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=1, device_id=slave
            )
            if result.isError():
                self.logger.debug(f"Error reading reg {register} (slave {slave}): {result}")
                return None
            return result.registers[0]
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error reg {register} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(f"Unexpected read error reg {register} (slave {slave}): {e}")
            return None

    def _read_int32(self, register, slave=None):
        """Read a signed 32-bit value from two consecutive registers."""
        if slave is None:
            slave = self.plant_address
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=2, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register+1} (slave {slave}): {result}"
                )
                return None
            value = (result.registers[0] << 16) | result.registers[1]
            if value >= 2147483648:
                value -= 4294967296
            return value
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(f"Unexpected read error regs {register}-{register+1} (slave {slave}): {e}")
            return None

    def _read_uint64(self, register, slave=None):
        """Read an unsigned 64-bit value from four consecutive registers (big-endian)."""
        if slave is None:
            slave = self.plant_address
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=4, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register+3} (slave {slave}): {result}"
                )
                return None
            r = result.registers
            return (r[0] << 48) | (r[1] << 32) | (r[2] << 16) | r[3]
        except (ModbusException, ConnectionException) as e:
            self.logger.error(
                f"Modbus read error regs {register}-{register+3} (slave {slave}): {e}"
            )
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(
                f"Unexpected read error regs {register}-{register+3} (slave {slave}): {e}"
            )
            return None

    def _read_uint32(self, register, slave=None):
        """Read an unsigned 32-bit value from two consecutive registers."""
        if slave is None:
            slave = self.plant_address
        self._throttle()
        try:
            result = self.client.read_holding_registers(
                address=register, count=2, device_id=slave
            )
            if result.isError():
                self.logger.debug(
                    f"Error reading regs {register}-{register+1} (slave {slave}): {result}"
                )
                return None
            return (result.registers[0] << 16) | result.registers[1]
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus read error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return None
        except Exception as e:
            self.logger.error(f"Unexpected read error regs {register}-{register+1} (slave {slave}): {e}")
            return None

    # ================================================================
    # Main Read Function
    # ================================================================

    def read_all(self):
        """Read all key registers and return a data dict.

        Returns None if connection fails (too many errors).

        Data keys returned:
          emsWorkMode, gridSensorConnected, gridPowerWatts, gridStatus,
          batterySoc, pvPowerWatts, batteryPowerWatts, plantRunningState,
          dischargeCutoffSoc, batterySoh, batteryDailyChargeKwh,
          batteryDailyDischargeKwh, batteryTempC, batteryCellVoltage,
          batteryMaxTempC, batteryMinTempC, homePowerWatts,
          modbusConnected, lastUpdate
        """
        if not self._connected:
            if not self.connect():
                return None

        data         = {}
        plant_errors = 0
        inv_errors   = 0

        # --- Phase A: Plant reads (slave 247) ---

        ems_mode = self._read_uint16(PLANT_EMS_WORK_MODE)
        if ems_mode is not None:
            data["emsWorkMode"] = EMS_MODES.get(ems_mode, f"Unknown ({ems_mode})")
        else:
            plant_errors += 1

        grid_sensor = self._read_uint16(PLANT_GRID_SENSOR_STATUS)
        if grid_sensor is not None:
            data["gridSensorConnected"] = (grid_sensor == 1)
        else:
            plant_errors += 1

        grid_power = self._read_int32(PLANT_GRID_ACTIVE_POWER)
        if grid_power is not None:
            data["gridPowerWatts"] = grid_power
        else:
            plant_errors += 1

        grid_status = self._read_uint16(PLANT_ON_OFF_GRID_STATUS)
        if grid_status is not None:
            data["gridStatus"] = GRID_STATUSES.get(grid_status, f"Unknown ({grid_status})")
        else:
            plant_errors += 1

        batt_soc = self._read_uint16(PLANT_BATTERY_SOC)
        if batt_soc is not None:
            data["batterySoc"] = round(batt_soc / 10.0, 1)
        else:
            plant_errors += 1

        pv_power = self._read_int32(PLANT_PV_POWER)
        if pv_power is not None:
            data["pvPowerWatts"] = max(0, pv_power)
        else:
            plant_errors += 1

        batt_power = self._read_int32(PLANT_ESS_POWER)
        if batt_power is not None:
            data["batteryPowerWatts"] = batt_power
        else:
            plant_errors += 1

        running_state = self._read_uint16(PLANT_RUNNING_STATE)
        if running_state is not None:
            data["plantRunningState"] = PLANT_RUNNING_STATES.get(
                running_state, f"Unknown ({running_state})"
            )
        else:
            plant_errors += 1

        cutoff_soc = self._read_uint16(PLANT_ESS_DISCHARGE_CUTOFF)
        if cutoff_soc is not None:
            data["dischargeCutoffSoc"] = round(cutoff_soc / 10.0, 1)
        else:
            plant_errors += 1

        batt_soh = self._read_uint16(PLANT_ESS_SOH)
        if batt_soh is not None:
            data["batterySoh"] = round(batt_soh / 10.0, 1)
        else:
            plant_errors += 1

        # --- Phase B: Inverter reads (configurable slave address) ---

        inv_addr = self.inverter_address

        daily_charge = self._read_uint32(INV_DAILY_CHARGE_ENERGY, slave=inv_addr)
        if daily_charge is not None:
            data["batteryDailyChargeKwh"] = round(daily_charge / 100.0, 2)
        else:
            inv_errors += 1

        daily_discharge = self._read_uint32(INV_DAILY_DISCHARGE_ENERGY, slave=inv_addr)
        if daily_discharge is not None:
            data["batteryDailyDischargeKwh"] = round(daily_discharge / 100.0, 2)
        else:
            inv_errors += 1

        batt_temp = self._read_int16(INV_BATTERY_AVG_TEMP, slave=inv_addr)
        if batt_temp is not None:
            data["batteryTempC"] = round(batt_temp / 10.0, 1)
        else:
            inv_errors += 1

        batt_voltage = self._read_uint16(INV_BATTERY_AVG_VOLTAGE, slave=inv_addr)
        if batt_voltage is not None:
            data["batteryCellVoltage"] = round(batt_voltage / 1000.0, 3)
        else:
            inv_errors += 1

        batt_max_temp = self._read_int16(INV_BATTERY_MAX_TEMP, slave=inv_addr)
        if batt_max_temp is not None:
            data["batteryMaxTempC"] = round(batt_max_temp / 10.0, 1)
        else:
            inv_errors += 1

        batt_min_temp = self._read_int16(INV_BATTERY_MIN_TEMP, slave=inv_addr)
        if batt_min_temp is not None:
            data["batteryMinTempC"] = round(batt_min_temp / 10.0, 1)
        else:
            inv_errors += 1

        # --- Phase C: Calculated values ---

        pv_w   = data.get("pvPowerWatts", 0)
        grid_w = data.get("gridPowerWatts", 0)
        batt_w = data.get("batteryPowerWatts", 0)
        data["homePowerWatts"] = max(0, pv_w + grid_w - batt_w)

        # --- Phase D: Plant daily/lifetime energy registers ---
        # pvLifetimeKwh / gridImportLifetimeKwh / gridExportLifetimeKwh are LIFETIME
        # totals; plugin.py computes daily values as (current - start-of-day snapshot).
        # homeDailyDirectKwh (30092) resets at midnight on the inverter — read directly.

        pv_total = self._read_uint64(PLANT_PV_TOTAL_KWH)
        if pv_total is not None:
            data["pvLifetimeKwh"] = round(pv_total / 100.0, 2)
        else:
            plant_errors += 1

        load_daily = self._read_uint32(PLANT_LOAD_DAILY_KWH)
        if load_daily is not None:
            data["homeDailyDirectKwh"] = round(load_daily / 100.0, 2)
        else:
            plant_errors += 1

        import_total = self._read_uint64(PLANT_TOTAL_IMPORT_KWH)
        if import_total is not None:
            data["gridImportLifetimeKwh"] = round(import_total / 100.0, 2)
        else:
            plant_errors += 1

        export_total = self._read_uint64(PLANT_TOTAL_EXPORT_KWH)
        if export_total is not None:
            data["gridExportLifetimeKwh"] = round(export_total / 100.0, 2)
        else:
            plant_errors += 1

        # --- Connection quality check ---

        total_errors = plant_errors + inv_errors
        if total_errors > 8:  # more than half of 16 reads failed
            self.logger.error(f"Too many Modbus errors ({total_errors}/16) - marking disconnected")
            self._connected = False
            return None

        data["modbusConnected"] = True
        data["lastUpdate"]      = datetime.now().strftime("%H:%M:%S")

        if total_errors > 0:
            self.logger.debug(
                f"Read complete with {total_errors} error(s) "
                f"(plant={plant_errors}, inverter={inv_errors})"
            )

        return data

    # ================================================================
    # Low-Level Write Primitives
    # ================================================================

    def _write_single_register(self, register, value, slave=None):
        """Write a single 16-bit register (function 0x06)."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            self.logger.error("Cannot write - not connected to inverter")
            return False
        self._throttle()
        try:
            result = self.client.write_register(address=register, value=value, device_id=slave)
            if result.isError():
                self.logger.error(f"Failed to write reg {register}={value} (slave {slave}): {result}")
                return False
            return True
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus write error reg {register} (slave {slave}): {e}")
            self._connected = False
            return False
        except Exception as e:
            self.logger.error(f"Unexpected write error reg {register} (slave {slave}): {e}")
            return False

    def _write_uint32_registers(self, register, value, slave=None):
        """Write a 32-bit unsigned value to two consecutive registers (function 0x10)."""
        if slave is None:
            slave = self.plant_address
        if not self._connected:
            self.logger.error("Cannot write - not connected to inverter")
            return False
        self._throttle()
        high_word = (value >> 16) & 0xFFFF
        low_word  = value & 0xFFFF
        try:
            result = self.client.write_registers(
                address=register, values=[high_word, low_word], device_id=slave
            )
            if result.isError():
                self.logger.error(
                    f"Failed to write regs {register}-{register+1}={value} (slave {slave}): {result}"
                )
                return False
            return True
        except (ModbusException, ConnectionException) as e:
            self.logger.error(f"Modbus write error regs {register}-{register+1} (slave {slave}): {e}")
            self._connected = False
            return False
        except Exception as e:
            self.logger.error(f"Unexpected write error regs {register}-{register+1} (slave {slave}): {e}")
            return False

    # ================================================================
    # Remote EMS Control
    # ================================================================

    def enable_remote_ems(self):
        """Enable Remote EMS control (register 40029 = 1)."""
        self.logger.info("Enabling Remote EMS control")
        success = self._write_single_register(HOLD_REMOTE_EMS_ENABLE, 1)
        if not success:
            self.logger.error("Failed to enable Remote EMS")
        return success

    def disable_remote_ems(self):
        """Disable Remote EMS - returns plant to local EMS control."""
        self.logger.info("Disabling Remote EMS - returning to local EMS")
        success = self._write_single_register(HOLD_REMOTE_EMS_ENABLE, 0)
        if not success:
            self.logger.error("Failed to disable Remote EMS")
        return success

    def set_remote_ems_mode(self, mode):
        """Set Remote EMS control mode (register 40031)."""
        mode_name = REMOTE_EMS_MODES.get(mode, f"Unknown ({mode})")
        if mode not in REMOTE_EMS_MODES:
            self.logger.error(f"Invalid Remote EMS mode: {mode}")
            return False
        self.logger.info(f"Setting Remote EMS mode: {mode_name} (0x{mode:02X})")
        success = self._write_single_register(HOLD_REMOTE_EMS_MODE, mode)
        if not success:
            self.logger.error(f"Failed to set Remote EMS mode: {mode_name}")
        return success

    def set_charge_limit(self, watts):
        """Set ESS max charging power (registers 40032-40033, watts)."""
        if watts < 0:
            self.logger.error(f"Invalid charge limit: {watts}W (must be >= 0)")
            return False
        self.logger.debug(f"Setting max charge limit: {watts}W")
        return self._write_uint32_registers(HOLD_ESS_MAX_CHARGE, watts)

    def set_discharge_limit(self, watts):
        """Set ESS max discharging power (registers 40034-40035, watts)."""
        if watts < 0:
            self.logger.error(f"Invalid discharge limit: {watts}W (must be >= 0)")
            return False
        self.logger.debug(f"Setting max discharge limit: {watts}W")
        return self._write_uint32_registers(HOLD_ESS_MAX_DISCHARGE, watts)

    def set_export_limit(self, watts):
        """Set grid max export power limit (registers 40038-40039, watts).

        Global DNO export cap. Takes effect regardless of EMS mode.
        Requires grid sensor connected.

        Args:
            watts: Export limit in watts. Use 4000 for 4kW DNO limit.
        """
        if watts < 0:
            self.logger.error(f"Invalid export limit: {watts}W (must be >= 0)")
            return False
        self.logger.info(f"Setting grid max export limit: {watts}W")
        success = self._write_uint32_registers(HOLD_GRID_MAX_EXPORT_LIMIT, watts)
        if not success:
            self.logger.error(f"Failed to set export limit to {watts}W")
        return success

    # ================================================================
    # Convenience Methods
    # ================================================================

    def force_charge(self, power_watts=8000):
        """Force charge battery from grid at specified power.

        Enables Remote EMS, sets Charge Grid First mode, sets power limit.
        """
        self.logger.info(f"Force charging from grid at {power_watts}W")
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x03):
            return False
        if not self.set_charge_limit(power_watts):
            return False
        self.logger.info(f"Force charge active: {power_watts}W from grid")
        return True

    def force_discharge(self, power_watts=4000):
        """Force discharge battery to grid at specified power.

        Enables Remote EMS, sets Discharge ESS First mode, sets power limit.
        """
        self.logger.info(f"Force discharging to grid at {power_watts}W")
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x06):
            return False
        if not self.set_discharge_limit(power_watts):
            return False
        self.logger.info(f"Force discharge active: {power_watts}W to grid")
        return True

    def set_self_consumption(self):
        """Set Max Self Consumption mode via Remote EMS."""
        self.logger.info("Setting Remote EMS: Max Self Consumption")
        if not self.enable_remote_ems():
            return False
        if not self.set_remote_ems_mode(0x02):
            return False
        self.logger.info("Remote EMS: Max Self Consumption active")
        return True

    def return_to_local(self):
        """Return inverter to its own local EMS control."""
        self.logger.info("Returning to local EMS control")
        return self.disable_remote_ems()

    # ================================================================
    # ESS SOC Limits (V2.6+ registers)
    # ================================================================

    def set_discharge_cutoff(self, soc_pct):
        """Set ESS minimum discharge SOC (register 40048).

        Global hardware limit - battery will not discharge below this SOC
        regardless of EMS mode. Used by VPP to protect post-event reserve.

        Args:
            soc_pct: Minimum SOC % (0.0 - 100.0)
        """
        if not (0.0 <= soc_pct <= 100.0):
            self.logger.error(f"Invalid discharge cutoff: {soc_pct}% (must be 0-100)")
            return False
        raw_value = int(round(soc_pct * 10))
        self.logger.info(f"Setting ESS discharge cutoff: {soc_pct:.1f}% (raw={raw_value})")
        success = self._write_single_register(HOLD_ESS_DISCHARGE_CUTOFF, raw_value)
        if not success:
            self.logger.error(f"Failed to set discharge cutoff to {soc_pct:.1f}%")
        return success

    def read_discharge_cutoff(self):
        """Read current ESS discharge cutoff SOC from register 40048.

        Returns:
            float: Discharge cutoff % or None on error.
        """
        if not self._connected:
            self.logger.warning("Cannot read discharge cutoff - not connected")
            return None
        # 40048 is a holding register - read with function 0x03
        raw = self._read_uint16(HOLD_ESS_DISCHARGE_CUTOFF)
        if raw is None:
            return None
        soc_pct = raw / 10.0
        self.logger.debug(f"Discharge cutoff: {soc_pct:.1f}% (raw={raw})")
        return soc_pct
