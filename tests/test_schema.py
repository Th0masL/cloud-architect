"""Tests for the cloud resource category schema and its validator."""

from __future__ import annotations

import dataclasses

import pytest

from cloud_architect.schema import Category, Layer, Schema, load_schema
from cloud_architect.validate import (
    ValidationError,
    assert_valid,
    validate_schema,
)


@pytest.fixture(scope="module")
def schema() -> Schema:
    return load_schema()


# --------------------------------------------------------------------------- #
# The packaged schema must be valid.
# --------------------------------------------------------------------------- #
def test_packaged_schema_is_valid(schema: Schema) -> None:
    assert validate_schema(schema) == []
    assert_valid(schema)  # must not raise


def test_schema_is_non_trivial(schema: Schema) -> None:
    assert len(schema.categories) >= 40
    assert schema.providers == ["aws", "gcp", "azure"]


# --------------------------------------------------------------------------- #
# Structural invariants (mirroring the validator, asserted directly).
# --------------------------------------------------------------------------- #
def test_ids_are_unique(schema: Schema) -> None:
    ids = [c.id for c in schema.categories]
    assert len(ids) == len(set(ids))


def test_every_category_declares_every_provider(schema: Schema) -> None:
    expected = set(schema.providers)
    for category in schema.categories:
        assert set(category.terraform_types) == expected, category.id


def test_deploy_after_resolves_to_existing_categories(schema: Schema) -> None:
    known = set(schema.by_id)
    for category in schema.categories:
        for dep in category.deploy_after:
            assert dep in known, f"{category.id} -> {dep}"
            assert dep != category.id


def test_groups_are_declared(schema: Schema) -> None:
    valid = set(schema.groups)
    for category in schema.categories:
        assert category.group in valid, category.id


def test_specific_deploy_after_edges(schema: Schema) -> None:
    """Spot-check deployAfter edges under the config-reference ordering model."""
    by_id = schema.by_id
    # Structural ordering (a resource references its container/base).
    assert "network" in by_id["subnet"].deploy_after
    assert "firewall" in by_id["firewall_rule"].deploy_after
    assert "kubernetes_cluster" in by_id["kubernetes_node_pool"].deploy_after
    # Multi-target overlay: the WAF association references both frontends.
    assert set(by_id["waf"].deploy_after) >= {"load_balancer", "api_gateway"}
    # Launch refs: a VM references its image and key at create time.
    assert set(by_id["compute_instance"].deploy_after) >= {"machine_image", "ssh_key_pair"}
    # HTTPS frontends reference a certificate in their config.
    assert "tls_certificate" in by_id["load_balancer"].deploy_after
    # Reserved IP referenced by allocation id.
    assert "static_ip" in by_id["nat_gateway"].deploy_after
    # Provider-managed keys: encryption_key has no dependents.
    assert all("encryption_key" not in c.deploy_after for c in schema.categories)
    # Consumer-side identity removed: a bucket config names no role.
    assert "identity" not in by_id["object_storage"].deploy_after


def test_layers_declared_and_strictly_ordered(schema: Schema) -> None:
    """Every category sits in a declared tier, strictly above all its dependencies."""
    declared = schema.layer_numbers
    for category in schema.categories:
        assert category.layer in declared, category.id
        for dep in category.deploy_after:
            assert category.layer > schema.by_id[dep].layer, f"{category.id} !> {dep}"


def test_at_least_one_provider_per_category(schema: Schema) -> None:
    """Every concept should map to a real resource on at least one provider."""
    for category in schema.categories:
        assert category.providers_with_support(), f"{category.id} maps to nothing anywhere"


# --------------------------------------------------------------------------- #
# Negative tests: the validator must catch broken schemas.
# --------------------------------------------------------------------------- #
def _category(**overrides: object) -> Category:
    base = dict(
        id="thing",
        group="network",
        layer=0,
        description="A thing.",
        terraform_types={"aws": ["aws_thing"], "gcp": [], "azure": []},
        deploy_after=[],
    )
    base.update(overrides)
    return Category(**base)  # type: ignore[arg-type]


def _schema(
    categories: list[Category],
    groups: list[str] | None = None,
    layers: list[Layer] | None = None,
) -> Schema:
    return Schema(
        providers=["aws", "gcp", "azure"],
        groups=groups or ["network"],
        layers=layers or [Layer(0, "l0"), Layer(10, "l10")],
        categories=categories,
    )


def test_detects_duplicate_ids() -> None:
    errors = validate_schema(_schema([_category(), _category()]))
    assert any("Duplicate category id" in e for e in errors)


def test_detects_non_snake_case_id() -> None:
    errors = validate_schema(_schema([_category(id="NotSnake")]))
    assert any("snake_case" in e for e in errors)


def test_detects_unknown_deploy_after() -> None:
    errors = validate_schema(_schema([_category(deploy_after=["ghost"])]))
    assert any("unknown category 'ghost'" in e for e in errors)


def test_detects_self_deploy_after() -> None:
    errors = validate_schema(_schema([_category(deploy_after=["thing"])]))
    assert any("deploys after itself" in e for e in errors)


def test_detects_unknown_group() -> None:
    errors = validate_schema(_schema([_category(group="bogus")]))
    assert any("unknown group" in e for e in errors)


def test_detects_missing_provider_key() -> None:
    errors = validate_schema(_schema([_category(terraform_types={"aws": ["aws_thing"]})]))
    assert any("missing provider key" in e for e in errors)


def test_detects_unknown_provider_key() -> None:
    types = {"aws": ["x"], "gcp": [], "azure": [], "oci": ["x"]}
    errors = validate_schema(_schema([_category(terraform_types=types)]))
    assert any("unknown provider 'oci'" in e for e in errors)


def test_detects_empty_terraform_type_string() -> None:
    errors = validate_schema(_schema([_category(terraform_types={"aws": [""], "gcp": [], "azure": []})]))
    assert any("invalid Terraform type" in e for e in errors)


def test_detects_deploy_after_cycle() -> None:
    a = _category(id="a", deploy_after=["b"])
    b = _category(id="b", deploy_after=["a"])
    errors = validate_schema(_schema([a, b]))
    assert any("Deploy-order cycle" in e for e in errors)


def test_detects_undeclared_layer() -> None:
    errors = validate_schema(_schema([_category(layer=999)]))
    assert any("undeclared layer 999" in e for e in errors)


def test_detects_layer_order_violation() -> None:
    a = _category(id="a", layer=0, deploy_after=["b"])
    b = _category(id="b", layer=10)
    errors = validate_schema(_schema([a, b]))
    assert any("must be a strictly higher layer" in e for e in errors)


def test_assert_valid_raises_on_broken_schema() -> None:
    with pytest.raises(ValidationError) as exc:
        assert_valid(_schema([_category(deploy_after=["ghost"])]))
    assert exc.value.errors


def test_load_schema_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_schema(tmp_path / "does_not_exist.yaml")


def test_category_is_frozen() -> None:
    category = _category()
    with pytest.raises(dataclasses.FrozenInstanceError):
        category.id = "mutated"  # type: ignore[misc]
