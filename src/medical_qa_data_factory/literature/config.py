"""Literature configuration constants.

The configuration file lives at ``config/literature.json`` by default. It
stores the environment variable name that holds the API key, never the key.
"""
from __future__ import annotations


#: Environment variable that points at the configuration file.
LITERATURE_CONFIG_ENV_VAR = "MEDICAL_QA_LITERATURE_CONFIG"

#: Default configuration path, relative to the project root.
LITERATURE_CONFIG_PATH = "config/literature.json"