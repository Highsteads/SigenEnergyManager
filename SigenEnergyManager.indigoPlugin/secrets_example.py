# secrets_example.py - Template for SigenEnergyManager credentials
# Copy to: /Library/Application Support/Perceptive Automation/secrets.py
# NEVER commit the real secrets.py to git.
#
# This file is a template only - values are illustrative.

# Octopus Energy
OCTOPUS_API_KEY = "your_octopus_api_key_here"
OCTOPUS_ACCOUNT = "A-XXXXXXXX"
OCTOPUS_MPAN    = "1012345678901"   # 13-digit electricity MPAN
OCTOPUS_SERIAL  = "XXXXXXXX"       # electricity meter serial

# Solcast (Hobbyist plan - 2 sites, 10 API calls/day/site)
SOLCAST_API_KEY   = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SOLCAST_SITE_1_ID = "xxxx-xxxx-xxxx-xxxx"   # East + South arrays
SOLCAST_SITE_2_ID = "xxxx-xxxx-xxxx-xxxx"   # West + Garage NE arrays

# Axle VPP (optional - only needed if participating in Axle Virtual Power Plant)
AXLE_API_KEY   = ""   # Bearer token from Axle signup
AXLE_CLIENT_ID = ""   # Axle client ID (if required)
