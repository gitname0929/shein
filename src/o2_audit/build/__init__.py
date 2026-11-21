"""Stage 02b: frozen artifact build, load/verify, and version lifecycle.

Modules:

- ``capabilities``   capability -> implementing modules + dependency graph
- ``importer``       strict per-record rules JSONL importer
- ``compiler``       frozen BUILD_DIR compiler (plans, SOPs, ledger, manifest)
- ``loader``         read-only loader with tamper / capability-hash binding
- ``lifecycle``      version registry, transitive impact, incremental rebuild
"""
from o2_audit.build.capabilities import (
    CAPABILITY_MODULES,
    MODULE_TO_CAPABILITY,
    build_capability_entries,
    capability_dependencies,
    current_capability_hashes,
)
from o2_audit.build.compiler import BuildError, BuildResult, compile_build
from o2_audit.build.importer import ImportResult, RuleRecord, import_rules_jsonl
from o2_audit.build.loader import (
    ArtifactIntegrityError,
    LoadedArtifact,
    FrozenRegistry,
    verify_build,
)

__all__ = [
    "ArtifactIntegrityError",
    "BuildError",
    "BuildResult",
    "CAPABILITY_MODULES",
    "FrozenRegistry",
    "ImportResult",
    "LoadedArtifact",
    "MODULE_TO_CAPABILITY",
    "RuleRecord",
    "build_capability_entries",
    "compile_build",
    "current_capability_hashes",
    "capability_dependencies",
    "import_rules_jsonl",
    "verify_build",
]
