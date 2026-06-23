"""Validate the cloud resource category schema.

Run as a module to validate the packaged schema::

    python -m cloud_architect.validate
    python -m cloud_architect.validate path/to/categories.yaml

Or call :func:`validate_schema` to get the list of problems programmatically.
The checks enforced are:

* every category ``id`` is unique and snake_case;
* every category declares exactly the supported providers in ``terraformTypes``;
* every Terraform type is a non-empty string;
* every ``group`` is one of the declared groups;
* every ``deployAfter`` entry resolves to an existing category, with no
  self-references or duplicates;
* the deploy-ordering graph is acyclic (so a single deploy order exists).
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
    errors += _check_groups(schema)
    errors += _check_providers(schema)
    errors += _check_terraform_types(schema)
    errors += _check_dependencies(schema)
    errors += _check_acyclic(schema)
    return errors


def assert_valid(schema: Schema) -> None:
    """Raise :class:`ValidationError` if the schema has any validation errors."""
    errors = validate_schema(schema)
    if errors:
        raise ValidationError(errors)


def _check_ids(schema: Schema) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for category in schema.categories:
        if category.id in seen:
            errors.append(f"Duplicate category id: {category.id!r}")
        seen.add(category.id)
        if not _ID_PATTERN.match(category.id):
            errors.append(f"Category id {category.id!r} is not snake_case.")
        if not category.description.strip():
            errors.append(f"Category {category.id!r} has an empty description.")
    return errors


def _check_groups(schema: Schema) -> list[str]:
    valid = set(schema.groups)
    return [
        f"Category {c.id!r} has unknown group {c.group!r} (expected one of {sorted(valid)})."
        for c in schema.categories
        if c.group not in valid
    ]


def _check_providers(schema: Schema) -> list[str]:
    errors: list[str] = []
    expected = set(schema.providers)
    for category in schema.categories:
        present = set(category.terraform_types)
        for unknown in sorted(present - expected):
            errors.append(
                f"Category {category.id!r} maps unknown provider {unknown!r} "
                f"(supported: {sorted(expected)})."
            )
        for missing in sorted(expected - present):
            errors.append(
                f"Category {category.id!r} is missing provider key {missing!r} "
                "(use an empty list if there is no equivalent)."
            )
    return errors


def _check_terraform_types(schema: Schema) -> list[str]:
    errors: list[str] = []
    for category in schema.categories:
        for provider, types in category.terraform_types.items():
            if not isinstance(types, list):
                errors.append(
                    f"Category {category.id!r} provider {provider!r}: terraformTypes must be a list."
                )
                continue
            for tf_type in types:
                if not isinstance(tf_type, str) or not tf_type.strip():
                    errors.append(
                        f"Category {category.id!r} provider {provider!r}: "
                        f"invalid Terraform type {tf_type!r}."
                    )
            if len(types) != len(set(types)):
                errors.append(
                    f"Category {category.id!r} provider {provider!r}: duplicate Terraform types."
                )
    return errors


def _check_dependencies(schema: Schema) -> list[str]:
    errors: list[str] = []
    known = set(schema.by_id)
    for category in schema.categories:
        seen: set[str] = set()
        for dep in category.deploy_after:
            if dep == category.id:
                errors.append(f"Category {category.id!r} deploys after itself.")
            elif dep not in known:
                errors.append(
                    f"Category {category.id!r} deploys after unknown category {dep!r}."
                )
            if dep in seen:
                errors.append(f"Category {category.id!r} lists duplicate deployAfter {dep!r}.")
            seen.add(dep)
    return errors


def _check_acyclic(schema: Schema) -> list[str]:
    """Detect deploy-order cycles via DFS. Skips edges to unknown ids (reported elsewhere)."""
    known = set(schema.by_id)
    graph = {
        c.id: [dep for dep in c.deploy_after if dep in known and dep != c.id]
        for c in schema.categories
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
        f"✓ schema is valid: {len(schema.categories)} categories, "
        f"{len(schema.providers)} providers, {len(schema.groups)} groups."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
