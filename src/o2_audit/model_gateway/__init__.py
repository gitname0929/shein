"""Model gateway package (Stage 03a): ZCode ``/v1/messages`` multimodal
observation extraction with strict parsing, bounded retries, credential-free
configuration and a content-addressed extraction cache.

The gateway reports observations ONLY — never compliance labels or decisions.
"""
from o2_audit.model_gateway.cache import (
    SCHEMA_VERSION,
    ExtractionCache,
    cache_key,
    open_workspace_cache,
)
from o2_audit.model_gateway.client import (
    MessagesClient,
    ModelReply,
    UsageStats,
    build_message_content,
    endpoint_url,
)
from o2_audit.model_gateway.config import ModelConfig, load_model_config
from o2_audit.model_gateway.gateway import ZCodeObservationGateway
from o2_audit.model_gateway.parser import (
    AnnotationResult,
    ParseReport,
    extract_json_text,
    parse_annotation,
    parse_observation_batch,
)
from o2_audit.model_gateway.prompts import (
    build_annotation_prompt,
    build_observation_prompt,
    observation_system_prompt,
)

__all__ = [
    "AnnotationResult",
    "ExtractionCache",
    "MessagesClient",
    "ModelConfig",
    "ModelReply",
    "ParseReport",
    "SCHEMA_VERSION",
    "UsageStats",
    "ZCodeObservationGateway",
    "build_annotation_prompt",
    "build_message_content",
    "build_observation_prompt",
    "cache_key",
    "endpoint_url",
    "extract_json_text",
    "load_model_config",
    "observation_system_prompt",
    "open_workspace_cache",
    "parse_annotation",
    "parse_observation_batch",
]
