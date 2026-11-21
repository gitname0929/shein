"""Vendored public protocol-1.3.1 validation helpers (来源: 02_协议与评分/校验工具).

Provenance
----------
Every file in this package directory is an exact byte-identical copy of the
corresponding public file in ``02_协议与评分/校验工具`` of the competition
material package. SHA256 of each source file at vendoring time:

- contracts.py            125a3e2df13ff35984e2ef1bb48e8210b32069692366bf691e65f17c5866d391
- normalization.py        0c17dbc7d73fdc0e8c7520273dcbd71940594690c2c0109c76ec8c9bc339e220
- artifact_hash.py        cd2d7b1a3671582a2cc48b2e1e6eeaa2b3977349ecaf36c92433775b52330b18
- quality.py              8b55e99f32ac8c47952dbdcf3ecdad5c10b3f3ee2808f562e563089470ceeba4
- validate_submission.py  0c4bd8343a06c73a8c759fbeee8b6b205ec28edef22406485524c89302c21168
- validate_annotations.py 8cb0d22e2da9d1a7c3bf9a7614cfe08feaf4b23283129bf7a8ae91070ba4668b
- README.md               d6e633abb453c0fa9fd13cf6ab0b2dfaf0a2d6ae568b55f1d82d0e1ca58a494c

No rule or scoring logic was modified. ``quality.py``, ``validate_annotations.py``
and ``validate_submission.py`` use flat sibling imports (``from contracts import
*``). To resolve those local imports without editing the vendored files, this
``__init__.py`` first imports the sibling modules as package submodules and then
aliases them in ``sys.modules`` under their original flat names, so the flat
imports inside the vendored files bind to the same vendored code.

``validate_submission.py`` executes ``argparse`` at module import time and is
therefore NOT imported by this package ``__init__``; import it explicitly only
when running it as a script (its own ``__main__`` guard).
"""
from __future__ import annotations

import sys

from o2_public_contracts import artifact_hash as _artifact_hash
from o2_public_contracts import contracts as _contracts
from o2_public_contracts import normalization as _normalization

__all__ = ["contracts", "normalization", "artifact_hash", "quality", "validate_annotations"]

# Aliases so vendored flat imports (`from contracts import *`,
# `from normalization import annotation_values_equal`) resolve to these modules.
sys.modules.setdefault("contracts", _contracts)
sys.modules.setdefault("normalization", _normalization)
sys.modules.setdefault("artifact_hash", _artifact_hash)

from o2_public_contracts import contracts, normalization, artifact_hash  # noqa: E402,F401
from o2_public_contracts import quality  # noqa: E402,F401  (imports via the aliases above)

sys.modules.setdefault("quality", quality)
from o2_public_contracts import validate_annotations  # noqa: E402,F401
