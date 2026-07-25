"""Connector registry and the per-type send/test implementations."""

from .base import ConnectorError, ConnectorType, FieldSpec, Message, sanitize_detail

__all__ = ["ConnectorError", "ConnectorType", "FieldSpec", "Message", "sanitize_detail"]
