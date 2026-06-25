"""Tests for the cloud resource schema and its validator."""

from __future__ import annotations

import dataclasses

import pytest

from cloud_architect.schema import Resource, Layer, Schema, load_schema
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
    assert len(schema.resources) >= 40
    assert schema.providers[:3] == ["aws", "gcp", "azure"]


# --------------------------------------------------------------------------- #
# Structural invariants (mirroring the validator, asserted directly).
# --------------------------------------------------------------------------- #
def test_ids_are_unique(schema: Schema) -> None:
    ids = [c.id for c in schema.resources]
    assert len(ids) == len(set(ids))


def test_resources_only_declare_known_providers(schema: Schema) -> None:
    known = set(schema.providers)
    for resource in schema.resources:
        assert set(resource.terraform_resources) <= known, resource.id


def test_deploy_after_resolves_to_existing_resources(schema: Schema) -> None:
    known = set(schema.by_id)
    for resource in schema.resources:
        for dep in resource.deploy_after:
            assert dep in known, f"{resource.id} -> {dep}"
            assert dep != resource.id


def test_categories_are_declared(schema: Schema) -> None:
    valid = set(schema.categories)
    for resource in schema.resources:
        assert resource.category in valid, resource.id


def test_specific_deploy_after_edges(schema: Schema) -> None:
    """Spot-check deployAfter edges under the config-reference ordering model."""
    by_id = schema.by_id
    # Structural ordering (a resource references its container/base).
    assert "virtual_network" in by_id["subnet"].deploy_after
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
    assert all("encryption_key" not in c.deploy_after for c in schema.resources)
    # Consumer-side identity removed: a bucket config names no role.
    assert "identity" not in by_id["object_storage"].deploy_after


def test_layers_declared_and_strictly_ordered(schema: Schema) -> None:
    """Every resource sits in a declared tier, strictly above all its dependencies."""
    declared = schema.layer_numbers
    for resource in schema.resources:
        assert resource.layer in declared, resource.id
        for dep in resource.deploy_after:
            assert resource.layer > schema.by_id[dep].layer, f"{resource.id} !> {dep}"


def test_at_least_one_provider_per_resource(schema: Schema) -> None:
    """Every resource should map to a real resource on at least one provider."""
    for resource in schema.resources:
        assert resource.providers_with_support(), f"{resource.id} maps to nothing anywhere"


# --------------------------------------------------------------------------- #
# Negative tests: the validator must catch broken schemas.
# --------------------------------------------------------------------------- #
def _resource(**overrides: object) -> Resource:
    base = dict(
        id="thing",
        category="networking",
        layer=0,
        description="A thing.",
        terraform_resources={"aws": ["aws_thing"], "gcp": [], "azure": []},
        deploy_after=[],
    )
    base.update(overrides)
    return Resource(**base)  # type: ignore[arg-type]


def _schema(
    resources: list[Resource],
    categories: list[str] | None = None,
    layers: list[Layer] | None = None,
) -> Schema:
    return Schema(
        providers=["aws", "gcp", "azure"],
        categories=categories or ["networking"],
        layers=layers or [Layer(0), Layer(10)],
        resources=resources,
    )


def test_detects_duplicate_ids() -> None:
    errors = validate_schema(_schema([_resource(), _resource()]))
    assert any("Duplicate resource id" in e for e in errors)


def test_detects_non_snake_case_id() -> None:
    errors = validate_schema(_schema([_resource(id="NotSnake")]))
    assert any("snake_case" in e for e in errors)


def test_detects_unknown_deploy_after() -> None:
    errors = validate_schema(_schema([_resource(deploy_after=["ghost"])]))
    assert any("unknown resource 'ghost'" in e for e in errors)


def test_detects_self_deploy_after() -> None:
    errors = validate_schema(_schema([_resource(deploy_after=["thing"])]))
    assert any("deploys after itself" in e for e in errors)


def test_detects_unknown_category() -> None:
    errors = validate_schema(_schema([_resource(category="bogus")]))
    assert any("unknown category" in e for e in errors)


def test_detects_no_populated_provider() -> None:
    errors = validate_schema(_schema([_resource(terraform_resources={"aws": [], "gcp": [], "azure": []})]))
    assert any("maps to no provider" in e for e in errors)


def test_detects_unknown_provider_key() -> None:
    types = {"aws": ["x"], "gcp": [], "azure": [], "oci": ["x"]}
    errors = validate_schema(_schema([_resource(terraform_resources=types)]))
    assert any("unknown provider 'oci'" in e for e in errors)


def test_detects_empty_terraform_type_string() -> None:
    errors = validate_schema(_schema([_resource(terraform_resources={"aws": [""], "gcp": [], "azure": []})]))
    assert any("invalid Terraform type" in e for e in errors)


def test_detects_deploy_after_cycle() -> None:
    a = _resource(id="a", deploy_after=["b"])
    b = _resource(id="b", deploy_after=["a"])
    errors = validate_schema(_schema([a, b]))
    assert any("Deploy-order cycle" in e for e in errors)


def test_detects_undeclared_layer() -> None:
    errors = validate_schema(_schema([_resource(layer=999)]))
    assert any("undeclared layer 999" in e for e in errors)


def test_detects_layer_order_violation() -> None:
    a = _resource(id="a", layer=0, deploy_after=["b"])
    b = _resource(id="b", layer=10)
    errors = validate_schema(_schema([a, b]))
    assert any("must be a strictly higher layer" in e for e in errors)


def test_assert_valid_raises_on_broken_schema() -> None:
    with pytest.raises(ValidationError) as exc:
        assert_valid(_schema([_resource(deploy_after=["ghost"])]))
    assert exc.value.errors


def test_load_schema_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_schema(tmp_path / "does_not_exist.yaml")


def test_resource_is_frozen() -> None:
    resource = _resource()
    with pytest.raises(dataclasses.FrozenInstanceError):
        resource.id = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Universes / hard deploy-order boundaries.
# --------------------------------------------------------------------------- #
def test_universe_of_classifies_cloud_and_cluster(schema: Schema) -> None:
    assert schema.universe_of(schema.by_id["virtual_network"]) == "cloud"
    assert schema.universe_of(schema.by_id["k8s_deployment"]) == "cluster"
    assert schema.universe_of(schema.by_id["helm_release"]) == "cluster"


def test_hard_edges_are_cross_universe_only(schema: Schema) -> None:
    hard = set(schema.hard_edges())
    # k8s roots -> the cloud-built cluster are the hard boundaries
    assert ("k8s_namespace", "kubernetes_cluster") in hard
    assert ("k8s_storage_class", "kubernetes_cluster") in hard
    # intra-cluster and intra-cloud edges are soft
    assert not schema.is_hard_edge("k8s_deployment", "k8s_namespace")
    assert not schema.is_hard_edge("subnet", "virtual_network")
    # every hard edge genuinely crosses a universe boundary
    for a, b in hard:
        assert schema.universe_of(schema.by_id[a]) != schema.universe_of(schema.by_id[b])


def test_detects_resource_spanning_universes() -> None:
    bad = _resource(terraform_resources={"aws": ["aws_thing"], "kubernetes": ["kubernetes_thing"]})
    sch = Schema(
        providers=["aws", "kubernetes"],
        categories=["networking"],
        layers=[Layer(0), Layer(10)],
        resources=[bad],
        universes={"cloud": ["aws"], "cluster": ["kubernetes"]},
    )
    assert any("spans multiple universes" in e for e in validate_schema(sch))
