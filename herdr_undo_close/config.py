"""Optional config.json loading, with typed defaults.

Read from $HERDR_PLUGIN_CONFIG_DIR/config.json. Every key is optional.
Unknown keys are ignored; a key with the wrong type falls back to its
default and a one-line stderr note (a corrupt config must never crash the
plugin).
"""

import json
import os
import sys
from typing import Any, Dict, Optional

DEFAULTS = {
    "max_entries": 25,
    "focus": True,
    "notify": True,
}  # type: Dict[str, Any]


def _validate(key, value, default):
    # type: (str, Any, Any) -> Optional[Any]
    """Return value if it matches the type/shape of default, else None."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int):
        # bool is a subclass of int in Python; reject it explicitly.
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        return None
    return value


def load_config(config_dir=None):
    # type: (Optional[str]) -> Dict[str, Any]
    if config_dir is None:
        config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    result = dict(DEFAULTS)
    if not config_dir:
        return result

    path = os.path.join(config_dir, "config.json")
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as exc:
        sys.stderr.write("herdr-undo-close: cannot read config.json (%s), using defaults\n" % exc)
        return result

    if not isinstance(raw, dict):
        sys.stderr.write("herdr-undo-close: config.json is not an object, using defaults\n")
        return result

    for key, default in DEFAULTS.items():
        if key not in raw:
            continue
        validated = _validate(key, raw[key], default)
        if validated is None:
            sys.stderr.write(
                "herdr-undo-close: config key %r has an invalid value, using default %r\n"
                % (key, default)
            )
            continue
        result[key] = validated

    return result
