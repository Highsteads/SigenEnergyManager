#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    secrets_example.py
# Description: Template for SigenEnergyManager credentials.
#              Add the keys below to the MASTER secrets.py at:
#                  /Library/Application Support/Perceptive Automation/secrets.py
#              Do NOT rename this file — it is a template only.
#              secrets.py is listed in .gitignore and will never be committed.
#
#              If you do not yet have a secrets.py:
#                Copy this file to the path above, rename it to secrets.py,
#                and fill in your values.
#
#              If you already have a secrets.py:
#                Add just the keys below to your existing file.
#
# Author:      CliveS & Claude Sonnet 4.6
# Date:        03-04-2026
# Version:     1.1

# ============================================================
# Octopus Energy
# Your API key: https://octopus.energy/dashboard/new/accounts/personal-details/api-access
# ============================================================
OCTOPUS_API_KEY = "your-octopus-api-key-here"
OCTOPUS_ACCOUNT = "A-XXXXXXXX"
OCTOPUS_MPAN    = "1012345678901"   # 13-digit electricity MPAN
OCTOPUS_SERIAL  = "XXXXXXXX"       # electricity meter serial

# ============================================================
# Solcast (Hobbyist plan — 2 sites, 10 API calls/day/site)
# Register at: https://toolkit.solcast.com.au/register/hobbyist
# ============================================================
SOLCAST_API_KEY   = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SOLCAST_SITE_1_ID = "xxxx-xxxx-xxxx-xxxx"   # first rooftop site resource ID
SOLCAST_SITE_2_ID = "xxxx-xxxx-xxxx-xxxx"   # second rooftop site resource ID

# ============================================================
# Axle VPP (optional — only needed if participating in Axle Virtual Power Plant)
# ============================================================
AXLE_API_KEY   = ""   # Bearer token from Axle signup
AXLE_CLIENT_ID = ""   # Axle client ID (if required)
