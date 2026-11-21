"""Model gateway configuration.

Security invariant: configuration NEVER carries credential VALUES. It stores
only the environment variable NAMES to resolve at runtime plus non-secret
call parameters. Serialization/logging of this class therefore cannot leak
tokens. Missing or blank environment variables raise
``ExecutionError("model_not_configured", ...)`` at resolution time — the
gateway never silently falls back to fake/stub observations.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from o2_audit.domain.models import ExecutionError
from o2_audit.protocol.jsonio import ProtocolError, read_json_strict


class ModelConfig(BaseModel):
    """Endpoint/token env-var NAMES + non-secret call parameters.

    ``base_url`` and API key are resolved from ``os.environ`` at call time via
    :meth:`resolve_credentials` (primary env first, then the documented
    fallback). Values never live in this object or its dumps.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="zcode/GLM-5.3-Flash", min_length=1)
    #: Primary environment variable holding the gateway base URL.
    base_url_env: str = Field(default="O2_BASE_URL", min_length=1)
    #: Fallback env var (Anthropic-compatible gateways).
    base_url_fallback_env: str = Field(default="ANTHROPIC_BASE_URL", min_length=1)
    #: Primary environment variable holding the API token.
    api_key_env: str = Field(default="O2_API_KEY", min_length=1)
    #: Fallback env var (Anthropic-compatible auth token).
    api_key_fallback_env: str = Field(default="ANTHROPIC_AUTH_TOKEN", min_length=1)
    anthropic_version: str = "2023-06-01"
    max_tokens: int = Field(default=4096, ge=1)
    timeout_seconds: float = Field(default=90.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    temperature: float = Field(default=0.0, ge=0, le=2)
    #: Exponential backoff base for transient retries (kept configurable so
    #: tests run fast; production default is 0.5 s).
    retry_backoff_seconds: float = Field(default=0.5, ge=0)

    def _resolve_env(self, primary: str, fallback: str, what: str) -> str:
        for name in (primary, fallback):
            value = os.environ.get(name)
            if value is not None and value.strip():
                return value.strip()
        raise ExecutionError(
            "model_not_configured",
            f"model {what} not configured: set the {primary} (or {fallback}) "
            "environment variable; no stub/fallback observations are produced",
        )

    def resolve_credentials(self) -> tuple[str, str]:
        """Resolve ``(base_url, api_key)`` from the environment.

        Missing or blank values raise ``ExecutionError("model_not_configured")``;
        this is an execution failure (label stays null), never an ambiguous
        verdict and never a stub response.
        """
        base_url = self._resolve_env(self.base_url_env, self.base_url_fallback_env, "base URL")
        api_key = self._resolve_env(self.api_key_env, self.api_key_fallback_env, "API key")
        return base_url, api_key

    def safe_dump(self) -> dict[str, Any]:
        """Credential-free dump (env var NAMES only, never values)."""
        return self.model_dump(mode="json")


def load_model_config(path: str | os.PathLike[str]) -> ModelConfig:
    """Load a strict JSON model config file (duplicate keys/NaN rejected)."""
    try:
        data = read_json_strict(path)
    except ProtocolError as e:
        raise ExecutionError("model_config_invalid", str(e)) from e
    try:
        return ModelConfig.model_validate(data)
    except Exception as e:
        raise ExecutionError("model_config_invalid", f"invalid model config: {e}") from e


__all__ = ["ModelConfig", "load_model_config"]
