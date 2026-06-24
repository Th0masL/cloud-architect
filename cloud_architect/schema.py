"""Load the cloud resource category schema from its YAML source of truth.

The schema is concept-first: each :class:`Category` is a generic, cloud-agnostic
architectural concept that maps onto provider-specific Terraform resource types
and declares deploy-ordering edges to other concepts. See ``README.md`` for the
``deployAfter`` model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# The packaged YAML lives at <repo>/schema/categories.yaml, one directory up from
# this package. Resolve it relative to this file so it works regardless of CWD.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "categories.yaml"


@dataclass(frozen=True)
class Layer:
    """A deploy-order tier: a sparse number, a name, and a short description."""

    number: int
    name: str
    description: str = ""


@dataclass(frozen=True)
class Category:
    """A single generic cloud resource category.

    Attributes:
        id: Unique snake_case concept name (e.g. ``"kubernetes_cluster"``).
        group: The resource's nice-named category (one of :attr:`Schema.groups`).
        layer: Deploy-tier number (one of :attr:`Schema.layers`); the folder-prefix
            apply-order contract. Invariant: ``layer`` exceeds every dependency's.
        description: One-line human summary.
        terraform_types: Provider key -> list of Terraform resource type names.
            Every provider in :attr:`Schema.providers` is present; an empty list
            means the provider has no clean direct equivalent.
        deploy_after: Ids of categories that must be deployed before this one
            (because this concept's config references them at deploy time).
    """

    id: str
    group: str
    layer: int
    description: str
    terraform_types: dict[str, list[str]]
    deploy_after: list[str] = field(default_factory=list)

    def providers_with_support(self) -> list[str]:
        """Providers that have at least one mapped Terraform resource type."""
        return [p for p, types in self.terraform_types.items() if types]


@dataclass(frozen=True)
class Schema:
    """The full schema: declared providers, groups, deploy-order layers, categories."""

    providers: list[str]
    groups: list[str]
    layers: list[Layer]
    categories: list[Category]

    @property
    def by_id(self) -> dict[str, Category]:
        """Categories indexed by id (last wins on duplicates; validation catches those)."""
        return {c.id: c for c in self.categories}

    @property
    def layer_numbers(self) -> set[int]:
        """The set of declared layer numbers."""
        return {layer.number for layer in self.layers}

    def get(self, category_id: str) -> Category | None:
        """Return the category with ``category_id`` or ``None`` if absent."""
        return self.by_id.get(category_id)


def load_schema(path: str | Path | None = None) -> Schema:
    """Parse the YAML schema file into a :class:`Schema`.

    Args:
        path: Schema file to load. Defaults to the packaged
            ``schema/categories.yaml``.

    Returns:
        The parsed schema. This performs structural parsing only; call
        :func:`cloud_architect.validate.validate_schema` to enforce the
        invariants (unique ids, resolvable deployAfter edges, valid providers, ...).

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the top-level YAML structure is malformed.
    """
    schema_path = Path(path) if path is not None else DEFAULT_SCHEMA_PATH
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    with schema_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        raise ValueError("Schema root must be a mapping with providers/groups/categories.")

    providers = _require_str_list(raw, "providers")
    groups = _require_str_list(raw, "groups")
    layers = _parse_layers(raw)

    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, list):
        raise ValueError("`categories` must be a list.")

    categories = [_parse_category(entry, index) for index, entry in enumerate(raw_categories)]
    return Schema(providers=providers, groups=groups, layers=layers, categories=categories)


def _require_str_list(raw: dict, key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"`{key}` must be a list of strings.")
    return list(value)


def _parse_layers(raw: dict) -> list[Layer]:
    raw_layers = raw.get("layers")
    if not isinstance(raw_layers, list):
        raise ValueError("`layers` must be a list.")
    layers: list[Layer] = []
    for index, entry in enumerate(raw_layers):
        if not isinstance(entry, dict) or "number" not in entry or "name" not in entry:
            raise ValueError(f"layers[{index}] must be a mapping with `number` and `name`.")
        if not isinstance(entry["number"], int):
            raise ValueError(f"layers[{index}] `number` must be an integer.")
        layers.append(
            Layer(number=entry["number"], name=entry["name"], description=entry.get("description", ""))
        )
    return layers


def _parse_category(entry: object, index: int) -> Category:
    if not isinstance(entry, dict):
        raise ValueError(f"categories[{index}] must be a mapping.")

    missing = {"id", "group", "layer", "description", "terraformTypes"} - entry.keys()
    if missing:
        where = entry.get("id", f"index {index}")
        raise ValueError(f"Category {where!r} is missing required keys: {sorted(missing)}")

    if not isinstance(entry["layer"], int):
        raise ValueError(f"Category {entry['id']!r}: `layer` must be an integer.")

    terraform_types = entry["terraformTypes"]
    if not isinstance(terraform_types, dict):
        raise ValueError(f"Category {entry['id']!r}: `terraformTypes` must be a mapping.")
    # Normalize a missing/None provider value to an empty list for ergonomics.
    normalized_types = {
        provider: list(types) if types is not None else []
        for provider, types in terraform_types.items()
    }

    deploy_after = entry.get("deployAfter", []) or []
    if not isinstance(deploy_after, list):
        raise ValueError(f"Category {entry['id']!r}: `deployAfter` must be a list.")

    return Category(
        id=entry["id"],
        group=entry["group"],
        layer=entry["layer"],
        description=entry["description"],
        terraform_types=normalized_types,
        deploy_after=list(deploy_after),
    )
