"""Tests for the static graph-viewer data generation."""

from __future__ import annotations

import json

from cloud_architect.schema import load_schema
from cloud_architect.site import DATA_FILE, render_data


def test_site_data_is_in_sync() -> None:
    """site/data.js must match the schema — regenerate with `python -m cloud_architect.site`."""
    expected = render_data(load_schema())
    actual = DATA_FILE.read_text(encoding="utf-8")
    assert actual == expected, "site/data.js is stale; run `python -m cloud_architect.site`"


def test_rendered_data_is_valid_js_payload() -> None:
    """The generated file must embed parseable JSON for every resource."""
    schema = load_schema()
    text = render_data(schema)
    start = text.index("{")
    end = text.rindex("}") + 1
    payload = json.loads(text[start:end])
    assert payload["providers"] == schema.providers
    assert [layer["number"] for layer in payload["layers"]] == [layer.number for layer in schema.layers]
    assert {c["id"] for c in payload["resources"]} == {c.id for c in schema.resources}
    for resource in payload["resources"]:
        assert set(resource) == {"id", "category", "layer", "description", "terraformResources", "deployAfter"}
