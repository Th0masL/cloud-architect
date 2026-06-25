"""Load the cloud resource schema from its YAML source of truth.

The schema is vendor-neutral: each :class:`Resource` is a generic, cloud-agnostic
architectural resource that maps onto provider-specific Terraform resource types
and declares deploy-ordering edges to other resources. See ``README.md`` for the
``deployAfter`` model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# The packaged YAML lives at <repo>/schema/resources.yaml, one directory up from
# this package. Resolve it relative to this file so it works regardless of CWD.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "resources.yaml"


@dataclass(frozen=True)
class Layer:
    """A deploy-order tier: a sparse number plus a short description.

    There is no semantic name; the display label is derived as ``L`` + the
    zero-padded number (``L00``, ``L10``, … ``L80``).
    """

    number: int
    description: str = ""

    @property
    def label(self) -> str:
        """Derived display label, e.g. ``L30`` for number 30."""
        return f"L{self.number:02d}"


@dataclass(frozen=True)
class Resource:
    """A single generic cloud resource.

    Attributes:
        id: Unique snake_case resource name (e.g. ``"kubernetes_cluster"``).
        category: The resource's domain (one of :attr:`Schema.categories`).
        layer: Deploy-tier number (one of :attr:`Schema.layers`); the folder-prefix
            apply-order contract. Invariant: ``layer`` exceeds every dependency's.
        description: One-line human summary.
        terraform_resources: Provider key -> list of Terraform resource type names.
            Only the providers that apply are listed (keys are a subset of
            :attr:`Schema.providers`); a present-but-empty list flags a provider
            with no clean direct equivalent.
        deploy_after: Ids of resources that must be deployed before this one
            (because this resource's config references them at deploy time).
    """

    id: str
    category: str
    layer: int
    description: str
    terraform_resources: dict[str, list[str]]
    deploy_after: list[str] = field(default_factory=list)

    def providers_with_support(self) -> list[str]:
        """Providers that have at least one mapped Terraform resource type."""
        return [p for p, types in self.terraform_resources.items() if types]


@dataclass(frozen=True)
class Schema:
    """The full schema: declared providers, categories, deploy-order layers, resources."""

    providers: list[str]
    categories: list[str]
    layers: list[Layer]
    resources: list[Resource]
    universes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def by_id(self) -> dict[str, Resource]:
        """Resources indexed by id (last wins on duplicates; validation catches those)."""
        return {c.id: c for c in self.resources}

    @property
    def layer_numbers(self) -> set[int]:
        """The set of declared layer numbers."""
        return {layer.number for layer in self.layers}

    def get(self, resource_id: str) -> Resource | None:
        """Return the resource with ``resource_id`` or ``None`` if absent."""
        return self.by_id.get(resource_id)

    @property
    def provider_universe(self) -> dict[str, str]:
        """Provider name -> the universe it belongs to (empty if none declared)."""
        return {p: name for name, provs in self.universes.items() for p in provs}

    def universe_of(self, resource: Resource) -> str | None:
        """The deployment universe of a resource, or None if undeclared/ambiguous.

        A resource's universe is the one its provider keys map into — cloud
        resources land in the cloud universe; kubernetes/helm/kubectl in the
        cluster universe. Returns None if universes aren't declared or the keys
        span more than one (which the validator flags).
        """
        lookup = self.provider_universe
        names = {lookup[p] for p in resource.terraform_resources if p in lookup}
        return next(iter(names)) if len(names) == 1 else None

    def is_hard_edge(self, dependent_id: str, dependency_id: str) -> bool:
        """True when a deployAfter edge crosses a universe boundary.

        A hard edge must be a separate, earlier terraform stack — the dependent's
        provider has to be configured from the already-applied dependency. A soft
        (same-universe) edge may be co-located in one stack and ordered by
        terraform itself.
        """
        a, b = self.get(dependent_id), self.get(dependency_id)
        if a is None or b is None:
            return False
        ua, ub = self.universe_of(a), self.universe_of(b)
        return ua is not None and ub is not None and ua != ub

    def hard_edges(self) -> list[tuple[str, str]]:
        """All (dependent, dependency) deployAfter edges that cross a universe boundary."""
        return [
            (c.id, dep)
            for c in self.resources
            for dep in c.deploy_after
            if self.is_hard_edge(c.id, dep)
        ]


def load_schema(path: str | Path | None = None) -> Schema:
    """Parse the YAML schema file into a :class:`Schema`.

    Args:
        path: Schema file to load. Defaults to the packaged
            ``schema/resources.yaml``.

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
        raise ValueError("Schema root must be a mapping with providers/categories/resources.")

    providers = _require_str_list(raw, "providers")
    categories = _require_str_list(raw, "categories")
    layers = _parse_layers(raw)
    universes = _parse_universes(raw)

    raw_resources = raw.get("resources")
    if not isinstance(raw_resources, list):
        raise ValueError("`resources` must be a list.")

    resources = [_parse_resource(entry, index) for index, entry in enumerate(raw_resources)]
    return Schema(
        providers=providers, categories=categories, layers=layers,
        resources=resources, universes=universes,
    )


def _require_str_list(raw: dict, key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"`{key}` must be a list of strings.")
    return list(value)


def _parse_universes(raw: dict) -> dict[str, list[str]]:
    value = raw.get("universes", {}) or {}
    if not isinstance(value, dict):
        raise ValueError("`universes` must be a mapping of universe name -> [providers].")
    result: dict[str, list[str]] = {}
    for name, provs in value.items():
        if not isinstance(provs, list) or not all(isinstance(p, str) for p in provs):
            raise ValueError(f"universes[{name!r}] must be a list of provider names.")
        result[str(name)] = list(provs)
    return result


def _parse_layers(raw: dict) -> list[Layer]:
    raw_layers = raw.get("layers")
    if not isinstance(raw_layers, list):
        raise ValueError("`layers` must be a list.")
    layers: list[Layer] = []
    for index, entry in enumerate(raw_layers):
        if not isinstance(entry, dict) or "number" not in entry:
            raise ValueError(f"layers[{index}] must be a mapping with a `number`.")
        if not isinstance(entry["number"], int):
            raise ValueError(f"layers[{index}] `number` must be an integer.")
        layers.append(
            Layer(number=entry["number"], description=entry.get("description", ""))
        )
    return layers


def _parse_resource(entry: object, index: int) -> Resource:
    if not isinstance(entry, dict):
        raise ValueError(f"resources[{index}] must be a mapping.")

    missing = {"id", "category", "layer", "description", "terraformResources"} - entry.keys()
    if missing:
        where = entry.get("id", f"index {index}")
        raise ValueError(f"Resource {where!r} is missing required keys: {sorted(missing)}")

    if not isinstance(entry["layer"], int):
        raise ValueError(f"Resource {entry['id']!r}: `layer` must be an integer.")

    terraform_resources = entry["terraformResources"]
    if not isinstance(terraform_resources, dict):
        raise ValueError(f"Resource {entry['id']!r}: `terraformResources` must be a mapping.")
    # Normalize a missing/None provider value to an empty list for ergonomics.
    normalized_types = {
        provider: list(types) if types is not None else []
        for provider, types in terraform_resources.items()
    }

    deploy_after = entry.get("deployAfter", []) or []
    if not isinstance(deploy_after, list):
        raise ValueError(f"Resource {entry['id']!r}: `deployAfter` must be a list.")

    return Resource(
        id=entry["id"],
        category=entry["category"],
        layer=entry["layer"],
        description=entry["description"],
        terraform_resources=normalized_types,
        deploy_after=list(deploy_after),
    )
