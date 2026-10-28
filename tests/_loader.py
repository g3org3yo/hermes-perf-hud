"""Shared loader so the tests exercise the exact files the plugin ships.

The plugin directory is ``perf-hud`` (a hyphen), so it is not importable as a
package; both halves of the plugin load their modules by path with a stable
module name, and so do the tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    """Load ``<plugin>/<name>.py`` under ``hermes_perf_hud_<name>``, once."""
    module_name = f"hermes_perf_hud_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
