"""Isolated evaluation plugins, subprocess runner, and version comparison.

Reference/gold labels are ONLY loaded inside ``o2_audit.evaluation`` and
``o2-audit evaluate``. Production ``build``, ``infer``, ``annotate``,
``rules``, and ``runtime`` modules never import this package.
"""
from o2_audit.evaluation.compare import compare_predictions, evaluate_protection_gate
from o2_audit.evaluation.manifest import (
    DEFAULT_CONFIG_PATH,
    PluginRegistry,
    PluginSpec,
    load_plugin_registry,
    save_plugin_registry,
)
from o2_audit.evaluation.runner import (
    ISOLATION_NOTE,
    run_plugin,
    run_plugins,
    validate_plugin_report,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ISOLATION_NOTE",
    "PluginRegistry",
    "PluginSpec",
    "compare_predictions",
    "evaluate_protection_gate",
    "load_plugin_registry",
    "run_plugin",
    "run_plugins",
    "save_plugin_registry",
    "validate_plugin_report",
]
