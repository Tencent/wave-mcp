"""Runtime infrastructure for the MCP server.

Kept apart from the analysis code on purpose: the data plane (waveform and
netlist readers) should not grow deployment concerns, and the deployment layer
should not reach into the readers. This package is where request identity and
resource lifetime live.
"""
from __future__ import annotations

from .context import LOCAL_OWNER, Principal, local_principal
from .identity import (cache_key, dataset_identity, dataset_version, file_version,
                       question_digest, request_signature)
from .request import API_SCHEMA_VERSION, RequestSnapshot, current, digest_of
from .resources import ResourceLease, ResourceRegistry

__all__ = ["LOCAL_OWNER", "Principal", "local_principal",
           "cache_key", "dataset_identity", "dataset_version", "file_version",
           "question_digest", "request_signature",
           "API_SCHEMA_VERSION", "RequestSnapshot", "current", "digest_of",
           "ResourceLease", "ResourceRegistry"]
