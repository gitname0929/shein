"""Declarative evaluation plugin manifest & configuration loader.

Supports:
- Registered plugin IDs (e.g. ``o2.conclusion``) and short aliases
  (e.g. ``conclusion``, ``evidence``, ``version_compare``).
- Direct ``module:function`` entry specifications as defined in protocol 1.3.1.
- Persistent workspace overrides so enabling/disabling plugins works across
  CLI runs and Web UI restarts without modifying source code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from o2_audit.protocol.jsonio import read_json_strict, write_json_atomic

SCHEMA_VERSION = "1.3.1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "plugins_config.json"


@dataclass(frozen=True)
class PluginSpec:
    """Declarative specification for one registered evaluation plugin."""

    plugin_id: str
    entry: str
    plugin_version: str = "1.0.0"
    enabled: bool = True
    timeout_seconds: float | None = 30.0
    description: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "aliases": list(self.aliases),
            "entry": self.entry,
            "plugin_version": self.plugin_version,
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginSpec:
        if not isinstance(data, dict):
            raise ValueError("plugin spec must be a JSON object")
        pid = data.get("plugin_id")
        entry = data.get("entry")
        if not isinstance(pid, str) or not pid.strip():
            raise ValueError("plugin_id must be a non-empty string")
        if not isinstance(entry, str) or ":" not in entry:
            raise ValueError(f"plugin '{pid}': entry must be 'module:function'")
        ver = data.get("plugin_version", "1.0.0")
        if not isinstance(ver, str) or not ver.strip():
            raise ValueError(f"plugin '{pid}': plugin_version must be a non-empty string")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"plugin '{pid}': enabled must be a boolean")
        timeout = data.get("timeout_seconds", 30.0)
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError(f"plugin '{pid}': timeout_seconds must be positive number or null")
            timeout = float(timeout)
        aliases_raw = data.get("aliases", [])
        if not isinstance(aliases_raw, list) or any(not isinstance(a, str) for a in aliases_raw):
            raise ValueError(f"plugin '{pid}': aliases must be a list of strings")
        desc = data.get("description", "")
        if not isinstance(desc, str):
            raise ValueError(f"plugin '{pid}': description must be a string")
        return cls(
            plugin_id=pid.strip(),
            entry=entry.strip(),
            plugin_version=ver.strip(),
            enabled=enabled,
            timeout_seconds=timeout,
            description=desc,
            aliases=tuple(a.strip() for a in aliases_raw if a.strip()),
        )


class PluginRegistry:
    """In-memory view of registered evaluation plugins."""

    def __init__(self, specs: list[PluginSpec]) -> None:
        self._specs: list[PluginSpec] = []
        self._by_key: dict[str, PluginSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: PluginSpec) -> None:
        if spec.plugin_id in self._by_key:
            raise ValueError(f"duplicate plugin_id: {spec.plugin_id}")
        self._specs.append(spec)
        self._by_key[spec.plugin_id] = spec
        for alias in spec.aliases:
            if alias in self._by_key:
                raise ValueError(f"duplicate plugin alias: {alias}")
            self._by_key[alias] = spec

    def list_specs(self) -> list[PluginSpec]:
        return list(self._specs)

    def get(self, name_or_id: str) -> PluginSpec | None:
        return self._by_key.get(name_or_id)

    def resolve(self, plugin_ref: str) -> PluginSpec:
        """Resolve either a registered plugin ID/alias or a direct ``module:func``."""
        found = self._by_key.get(plugin_ref)
        if found is not None:
            return found
        if ":" in plugin_ref:
            mod, fn = plugin_ref.rsplit(":", 1)
            if mod.strip() and fn.strip():
                return PluginSpec(
                    plugin_id=plugin_ref,
                    entry=plugin_ref,
                    plugin_version="custom",
                    enabled=True,
                    timeout_seconds=env_timeout_seconds(),
                    description="Direct module:function plugin entry",
                )
        raise KeyError(f"unknown evaluation plugin: {plugin_ref!r}")

    def with_enabled(self, plugin_id: str, enabled: bool) -> PluginRegistry:
        """Return a new registry toggling ``enabled`` on one plugin."""
        target = self.get(plugin_id)
        if target is None:
            raise KeyError(f"unknown evaluation plugin: {plugin_id!r}")
        updated: list[PluginSpec] = []
        for spec in self._specs:
            if spec.plugin_id == target.plugin_id:
                updated.append(PluginSpec(
                    plugin_id=spec.plugin_id,
                    entry=spec.entry,
                    plugin_version=spec.plugin_version,
                    enabled=enabled,
                    timeout_seconds=spec.timeout_seconds,
                    description=spec.description,
                    aliases=spec.aliases,
                ))
            else:
                updated.append(spec)
        return PluginRegistry(updated)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plugins": [s.to_dict() for s in self._specs],
        }


def env_timeout_seconds() -> float | None:
    """Optional ``PLUGIN_TIMEOUT_SECONDS`` env override (protocol convention)."""
    raw = os.environ.get("PLUGIN_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return None
    val = float(raw)
    if val <= 0:
        raise ValueError("PLUGIN_TIMEOUT_SECONDS must be positive")
    return val


def load_plugin_registry(config_path: str | Path | None = None) -> PluginRegistry:
    """Load plugin registry from JSON manifest (defaults to bundled config)."""
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    data = read_json_strict(path)
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ValueError(f"invalid plugin manifest: {path}")
    specs = [PluginSpec.from_dict(item) for item in data["plugins"]]
    return PluginRegistry(specs)


def save_plugin_registry(registry: PluginRegistry, config_path: str | Path) -> Path:
    """Atomically persist plugin registry config JSON."""
    path = Path(config_path)
    write_json_atomic(path, registry.to_manifest_dict())
    return path
