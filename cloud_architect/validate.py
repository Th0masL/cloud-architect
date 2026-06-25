"""Validate the cloud resource schema.

Run as a module to validate the packaged schema::

    python -m cloud_architect.validate
    python -m cloud_architect.validate path/to/resources.yaml

Or call :func:`validate_schema` to get the list of problems programmatically.
The checks enforced are:

* every resource ``id`` is unique and snake_case;
* every ``terraformResources`` key is a declared provider, and at least one is non-empty;
* every Terraform type is a non-empty string;
* every ``category`` is one of the declared categories;
* every ``deployAfter`` entry resolves to an existing resource, with no
  self-references or duplicates;
* the deploy-ordering graph is acyclic (so a single deploy order exists);
* every ``layer`` is a declared tier and exceeds every dependency's layer
  (so applying the folder tiers low-to-high is always a valid order);
* (optional) universes partition the providers, and no resource spans two.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from cloud_architect.schema import Schema, load_schema

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ValidationError(Exception):
    """Raised when the schema fails one or more validation checks."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Schema validation failed with {len(errors)} error(s).")


def validate_schema(schema: Schema) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []
    errors += _check_ids(schema)
    errors += _check_categories(schema)
    errors += _check_providers(schema)
    errors += _check_terraform_resources(schema)
    errors += _check_dependencies(schema)
    errors += _check_acyclic(schema)
    errors += _check_layers(schema)
    errors += _check_universes(schema)
    return errors


def _check_layers(schema: Schema) -> list[str]:
    errors: list[str] = []
    numbers = [layer.number for layer in schema.layers]
    for number in sorted({n for n in numbers if numbers.count(n) > 1}):
        errors.append(f"Duplicate layer number: {number}")
    declared = set(numbers)
    by_id = schema.by_id
    for resource in schema.resources:
        if resource.layer not in declared:
            errors.append(
                f"Resource {resource.id!r} has undeclared layer {resource.layer} "
                f"(declared: {sorted(declared)})."
            )
    # The deploy-order contract: a resource must sit in a strictly higher layer
    # than everything it deploys after, so a layer-by-layer apply always works.
    for resource in schema.resources:
        for dep in resource.deploy_after:
            target = by_id.get(dep)
            if target is None:
                continue  # unknown dependency reported elsewhere
            if resource.layer <= target.layer:
                errors.append(
                    f"Resource {resource.id!r} (layer {resource.layer}) deploys after "
                    f"{dep!r} (layer {target.layer}) — must be a strictly higher layer."
                )
    return errors


def assert_valid(schema: Schema) -> None:
    """Raise :class:`ValidationError` if the schema has any validation errors."""
    errors = validate_schema(schema)
    if errors:
        raise ValidationError(errors)


def _check_ids(schema: Schema) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for resource in schema.resources:
        if resource.id in seen:
            errors.append(f"Duplicate resource id: {resource.id!r}")
        seen.add(resource.id)
        if not _ID_PATTERN.match(resource.id):
            errors.append(f"Resource id {resource.id!r} is not snake_case.")
        if not resource.description.strip():
            errors.append(f"Resource {resource.id!r} has an empty description.")
    return errors


def _check_categories(schema: Schema) -> list[str]:
    valid = set(schema.categories)
    return [
        f"Resource {c.id!r} has unknown category {c.category!r} (expected one of {sorted(valid)})."
        for c in schema.resources
        if c.category not in valid
    ]


def _check_providers(schema: Schema) -> list[str]:
    errors: list[str] = []
    known = set(schema.providers)
    for resource in schema.resources:
        # A resource lists only the providers that apply (cloud resources use
        # aws/gcp/azure; cross-cutting ones use kubernetes/helm/kubectl). Keys
        # must be known providers, and at least one list must be non-empty.
        for unknown in sorted(set(resource.terraform_resources) - known):
            errors.append(
                f"Resource {resource.id!r} maps unknown provider {unknown!r} "
                f"(known: {sorted(known)})."
            )
        if not resource.providers_with_support():
            errors.append(
                f"Resource {resource.id!r} maps to no provider "
                "(at least one provider list must be non-empty)."
            )
    return errors


def _check_terraform_resources(schema: Schema) -> list[str]:
    errors: list[str] = []
    for resource in schema.resources:
        for provider, types in resource.terraform_resources.items():
            if not isinstance(types, list):
                errors.append(
                    f"Resource {resource.id!r} provider {provider!r}: terraformResources must be a list."
                )
                continue
            for tf_type in types:
                if not isinstance(tf_type, str) or not tf_type.strip():
                    errors.append(
                        f"Resource {resource.id!r} provider {provider!r}: "
                        f"invalid Terraform type {tf_type!r}."
                    )
            if len(types) != len(set(types)):
                errors.append(
                    f"Resource {resource.id!r} provider {provider!r}: duplicate Terraform types."
                )
    return errors


def _check_dependencies(schema: Schema) -> list[str]:
    errors: list[str] = []
    known = set(schema.by_id)
    for resource in schema.resources:
        seen: set[str] = set()
        for dep in resource.deploy_after:
            if dep == resource.id:
                errors.append(f"Resource {resource.id!r} deploys after itself.")
            elif dep not in known:
                errors.append(
                    f"Resource {resource.id!r} deploys after unknown resource {dep!r}."
                )
            if dep in seen:
                errors.append(f"Resource {resource.id!r} lists duplicate deployAfter {dep!r}.")
            seen.add(dep)
    return errors


def _check_acyclic(schema: Schema) -> list[str]:
    """Detect deploy-order cycles via DFS. Skips edges to unknown ids (reported elsewhere)."""
    known = set(schema.by_id)
    graph = {
        c.id: [dep for dep in c.deploy_after if dep in known and dep != c.id]
        for c in schema.resources
    }
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    errors: list[str] = []

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GREY
        stack.append(node)
        for neighbor in graph.get(node, []):
            if color[neighbor] == GREY:
                cycle = stack[stack.index(neighbor):] + [neighbor]
                errors.append("Deploy-order cycle: " + " -> ".join(cycle))
            elif color[neighbor] == WHITE:
                visit(neighbor, stack)
        stack.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            visit(node, [])
    # A single cycle is reachable from multiple roots; de-duplicate the messages.
    return sorted(set(errors))


def _check_universes(schema: Schema) -> list[str]:
    """Universes (optional) partition the providers; no resource may span two."""
    if not schema.universes:
        return []
    errors: list[str] = []
    known = set(schema.providers)
    owner: dict[str, str] = {}
    for name, provs in schema.universes.items():
        for p in provs:
            if p not in known:
                errors.append(f"Universe {name!r} lists unknown provider {p!r}.")
            if p in owner:
                errors.append(f"Provider {p!r} is in multiple universes ({owner[p]!r}, {name!r}).")
            owner[p] = name
    for p in sorted(known - set(owner)):
        errors.append(f"Provider {p!r} is not assigned to any universe.")
    for resource in schema.resources:
        spanned = {owner[p] for p in resource.terraform_resources if p in owner}
        if len(spanned) > 1:
            errors.append(
                f"Resource {resource.id!r} spans multiple universes {sorted(spanned)} "
                "(a resource must belong to exactly one)."
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 == valid)."""
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else None

    try:
        schema = load_schema(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: could not load schema: {exc}", file=sys.stderr)
        return 2

    errors = validate_schema(schema)
    if errors:
        print(f"✗ schema is INVALID — {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        f"✓ schema is valid: {len(schema.resources)} resources, "
        f"{len(schema.providers)} providers, {len(schema.categories)} categories."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
