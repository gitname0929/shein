"""Provider-neutral Anthropic-Messages client for ``{base_url}/v1/messages``.

Compatible with Anthropic Messages gateways and ZCode endpoints: the request
carries both ``x-api-key`` and ``authorization: Bearer`` headers. Credentials
are resolved from the environment at call time and are NEVER included in
error messages, reprs or logs.

Failure taxonomy (all :class:`ExecutionError`, i.e. ``execution_status=error``
and ``label=null`` — never an ambiguous verdict, never a stub response):

- ``model_not_configured``: missing/blank env vars.
- ``gateway_timeout``: request timed out after bounded retries.
- ``gateway_network_error``: other transport errors after bounded retries.
- ``gateway_http_error``: non-transient HTTP status, or transient status that
  persisted through all retries.
- ``gateway_parse_error``: non-JSON HTTP body, malformed response envelope or
  model refusal.
- ``gateway_incomplete_response``: truncated output (``max_tokens``) or empty
  content.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from o2_audit.domain.models import ExecutionError
from o2_audit.model_gateway.config import ModelConfig
from o2_public_contracts import contracts as public_contracts

#: HTTP statuses retried as transient (rate limiting / provider hiccups).
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass
class ModelReply:
    """One successful model round (text content + usage)."""

    text: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


@dataclass
class UsageStats:
    """Aggregate token/call accounting; never contains credentials."""

    calls: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
        }


def build_message_content(
    text: str, images: list[tuple[str, str]] | None = None
) -> list[dict[str, Any]]:
    """``/v1/messages`` user content blocks: text + base64 image blocks.

    ``images`` items are ``(base64_payload, media_type)`` pairs produced by
    :func:`o2_audit.materials.loader.encode_image_base64`.
    """
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for payload, media_type in images or []:
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": payload},
        })
    return blocks


def endpoint_url(base_url: str) -> str:
    """``{base_url}/v1/messages`` — tolerates a trailing slash and a base URL
    that already ends in ``/v1``."""
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


class MessagesClient:
    """Bounded-retry HTTP client posting Anthropic-Messages bodies."""

    def __init__(self, config: ModelConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport
        self.usage = UsageStats()

    @property
    def config(self) -> ModelConfig:
        """The (credential-free) model configuration in use."""
        return self._config

    # -- error helpers -------------------------------------------------------

    @staticmethod
    def _http_error(status: int, body_snippet: str) -> ExecutionError:
        # Body snippets are truncated and never include request headers, so
        # the API token cannot leak through error messages or logs.
        snippet = body_snippet[:300].replace("\n", " ")
        return ExecutionError(
            "gateway_http_error",
            f"model gateway returned HTTP {status}: {snippet}",
        )

    # -- request -------------------------------------------------------------

    def messages(
        self,
        system: str,
        text: str,
        images: list[tuple[str, str]] | None = None,
    ) -> ModelReply:
        """One ``POST {base}/v1/messages`` round with bounded transient retry."""
        cfg = self._config
        base_url, api_key = cfg.resolve_credentials()
        body: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "system": system,
            "messages": [{"role": "user", "content": build_message_content(text, images)}],
        }
        headers = {
            "content-type": "application/json",
            "anthropic-version": cfg.anthropic_version,
            "x-api-key": api_key,
            "authorization": f"Bearer {api_key}",
        }
        url = endpoint_url(base_url)
        max_attempts = cfg.max_retries + 1
        started = time.monotonic()
        last_error: ExecutionError | None = None
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=cfg.timeout_seconds,
            ) as http_client:
                for attempt in range(max_attempts):
                    self.usage.calls += 1
                    if attempt > 0:
                        self.usage.retries += 1
                    try:
                        response = http_client.post(
                            url,
                            json=body,
                            headers=headers,
                        )
                    except httpx.TimeoutException as e:
                        last_error = ExecutionError(
                            "gateway_timeout", f"model gateway request timed out: {type(e).__name__}")
                    except httpx.TransportError as e:
                        last_error = ExecutionError(
                            "gateway_network_error",
                            f"model gateway network error: {type(e).__name__}")
                    else:
                        if response.status_code in _TRANSIENT_STATUSES:
                            last_error = self._http_error(
                                response.status_code, response.text if response else "")
                        elif response.status_code >= 400:
                            raise self._http_error(response.status_code, response.text)
                        else:
                            return self._parse_response(response)
                    if attempt + 1 < max_attempts:
                        time.sleep(cfg.retry_backoff_seconds * (2 ** attempt))
            assert last_error is not None
            raise last_error
        finally:
            self.usage.latency_ms += max(0, int((time.monotonic() - started) * 1000))

    # -- response ------------------------------------------------------------

    def _parse_response(self, response: httpx.Response) -> ModelReply:
        try:
            # Strict JSON: duplicate keys / NaN / Infinity in the response
            # envelope are rejected like everywhere else in this repo.
            data = public_contracts.strict_loads(response.text)
        except (ValueError, public_contracts.ContractError) as e:
            raise ExecutionError(
                "gateway_parse_error", f"model gateway response is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ExecutionError(
                "gateway_parse_error", "model gateway response must be a JSON object")
        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            raise ExecutionError(
                "gateway_parse_error", "model refused the request (stop_reason=refusal)")
        content = data.get("content")
        if not isinstance(content, list):
            raise ExecutionError(
                "gateway_parse_error", "model response content must be a list of blocks")
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "refusal":
                raise ExecutionError(
                    "gateway_parse_error", "model refused the request (refusal content block)")
        if stop_reason == "max_tokens":
            raise ExecutionError(
                "gateway_incomplete_response",
                "model response was truncated (stop_reason=max_tokens)")
        text = "".join(texts).strip()
        if not text:
            raise ExecutionError(
                "gateway_incomplete_response", "model returned no text content")
        usage = data.get("usage") or {}
        raw_in = usage.get("input_tokens") if isinstance(usage, dict) else None
        raw_out = usage.get("output_tokens") if isinstance(usage, dict) else None
        in_tok = raw_in if isinstance(raw_in, int) and not isinstance(raw_in, bool) else 0
        out_tok = raw_out if isinstance(raw_out, int) and not isinstance(raw_out, bool) else 0
        self.usage.input_tokens += in_tok
        self.usage.output_tokens += out_tok
        return ModelReply(
            text=text,
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )


__all__ = [
    "MessagesClient",
    "ModelReply",
    "UsageStats",
    "build_message_content",
    "endpoint_url",
]
