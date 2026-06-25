"""Vendor-neutral cloud resource schema: loading, validation, querying.

Public API is exposed lazily (PEP 562) so that running a submodule directly with
``python -m cloud_architect.validate`` does not trigger an import-order warning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "Resource",
    "Layer",
    "Schema",
    "load_schema",
    "ValidationError",
    "validate_schema",
]

if TYPE_CHECKING:
    from cloud_architect.schema import Resource, Layer, Schema, load_schema
    from cloud_architect.validate import ValidationError, validate_schema

_SCHEMA_EXPORTS = {"Resource", "Layer", "Schema", "load_schema"}
_VALIDATE_EXPORTS = {"ValidationError", "validate_schema"}


def __getattr__(name: str):
    if name in _SCHEMA_EXPORTS:
        from cloud_architect import schema

        return getattr(schema, name)
    if name in _VALIDATE_EXPORTS:
        from cloud_architect import validate

        return getattr(validate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
