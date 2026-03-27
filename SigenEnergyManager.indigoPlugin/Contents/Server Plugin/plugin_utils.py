#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin_utils.py
# Description: Shared utilities for all Indigo plugins (CliveS / Highsteads)
#              Import via sys.path.insert using the same pattern as secrets.py
# Author:      CliveS & Claude Sonnet 4.6
# Date:        27-03-2026
# Version:     1.0

import indigo
import platform


def log_startup_banner(plugin_id, display_name, version, extras=None):
    """
    Print a standardised startup banner to the Indigo event log.

    Args:
        plugin_id       (str)  Plugin bundle identifier (e.g. com.clives.indigoplugin.ecowitt)
        display_name    (str)  Human-readable plugin name (e.g. Ecowitt Weather Station)
        version         (str)  Plugin version string (e.g. 2.0.0)
        extras          (list) Optional list of (label, value) tuples for plugin-specific
                               extra lines appended after the standard block.
                               e.g. [("Compatible Hardware:", "Ecowitt / Fine Offset")]

    Usage in plugin __init__():
        import sys as _sys
        _sys.path.insert(0, "/Library/Application Support/Perceptive Automation")
        try:
            from plugin_utils import log_startup_banner
        except ImportError:
            log_startup_banner = None

        # ... all other __init__ setup ...

        if log_startup_banner:
            log_startup_banner(pluginId, pluginDisplayName, pluginVersion)
        else:
            indigo.server.log(f"{pluginDisplayName} v{pluginVersion} starting")
    """
    title = f"Starting {display_name} Plugin"
    width = 110
    pad   = (width - len(title) - 2) // 2
    mid   = f"{'=' * pad} {title} {'=' * (width - pad - len(title) - 2)}"
    bar   = "=" * width

    indigo.server.log(bar)
    indigo.server.log(mid)
    indigo.server.log(bar)
    indigo.server.log(f"  {'Plugin Name:':<28} {display_name}")
    indigo.server.log(f"  {'Plugin Version:':<28} {version}")
    indigo.server.log(f"  {'Plugin ID:':<28} {plugin_id}")
    indigo.server.log(f"  {'Indigo Version:':<28} {indigo.server.version}")
    indigo.server.log(f"  {'Indigo API Version:':<28} {indigo.server.apiVersion}")
    indigo.server.log(f"  {'Architecture:':<28} {platform.machine()}")
    indigo.server.log(f"  {'Python Version:':<28} {platform.python_version()}")
    indigo.server.log(f"  {'macOS Version:':<28} {platform.mac_ver()[0]}")

    if extras:
        for label, value in extras:
            indigo.server.log(f"  {label:<28} {value}")

    indigo.server.log(bar)
