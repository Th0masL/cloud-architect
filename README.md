# cloud-architect

A **concept-first, provider-aware schema** of cloud resource categories. It
describes cloud architecture in generic terms (a `network`, a
`kubernetes_cluster`, an `object_storage` bucket) and maps each concept onto the
concrete Terraform resource types for **AWS**, **GCP**, and **Azure** — together
with the **deploy-order** edges between concepts.

The schema is the single source of truth in
[`schema/categories.yaml`](schema/categories.yaml). The Python package
[`cloud_architect/`](cloud_architect/) loads and validates it.

## Why concept-first?

Most multi-cloud tooling is *provider-first*: it models `aws_vpc`,
`google_compute_network`, and `azurerm_virtual_network` as three unrelated
things. That fragments the model and makes cross-provider reasoning impossible.

Here we invert it. The **concept** (`network`) is primary; provider resource
types are just how that concept is realized on a given cloud:

```yaml
- id: network
  group: network
  layer: 0
  description: Isolated virtual network (VPC / VNet).
  terraformTypes:
    aws: [aws_vpc]
    gcp: [google_compute_network]
    azure: [azurerm_virtual_network]
  deployAfter: []
```

Two principles fall out of this:

1. **Never force a false equivalence.** When a provider has no clean equivalent
   for a concept, its list is **empty** — we do not bolt on a near-miss resource.
   For example, GCP has no security-group *container*, so `firewall` is
   `gcp: []` and the rules live under `firewall_rule` (`google_compute_firewall`).
   Likewise `container_instance` is essentially Azure Container Instances, so AWS
   and GCP are empty there.
2. **Do not merge dissimilar runtime models** just to shrink the category count.
   `container_service` (ECS service / Cloud Run / Container Apps),
   `kubernetes_cluster` (EKS / GKE / AKS), `container_task_definition` (ECS task
   defs), and `container_job` (Batch / Cloud Run Jobs / Container Apps Jobs) stay
   separate because they are genuinely different runtime concepts.

## Schema structure

The file has four top-level keys:

| Key          | Meaning                                                              |
| ------------ | ------------------------------------------------------------------- |
| `providers`  | The supported clouds. Every category must map all of them.          |
| `groups`     | The nice-named categories (`network`, `data`, `compute`, …).         |
| `layers`     | The ordered deploy tiers — each a sparse `number`, `name`, `description`. |
| `categories` | The list of concepts. Each follows the shape below.                 |

Each category:

| Field            | Rule                                                                       |
| ---------------- | -------------------------------------------------------------------------- |
| `id`             | Unique, `snake_case`, the generic concept name.                            |
| `group`          | One of the declared `groups` (the resource's nice-named category).         |
| `layer`          | One of the declared `layers[].number` — its deploy tier (see below).       |
| `description`    | One-line human summary.                                                     |
| `terraformTypes` | Map of **every** provider → list of Terraform types (`[]` if no equivalent).|
| `deployAfter`    | List of other category `id`s that must be deployed **before** this one.    |

The eight groups are `network`, `security`, `compute`, `storage`, `data`,
`governance`, `ml`, and `devops`.

## How `deployAfter` is modeled

`deployAfter` is a **deploy-ordering** relation, not a hard "cannot exist
without" requirement. The precise, Terraform-shaped test for an edge is:

> **A `deployAfter` B** when standing up A *the way you intend it* means A's own
> resource configuration (or its standard companion association resource) has to
> **reference B** — so B must be deployed first. Assume every concept here is
> something you provision in your own stack.

This is the **config-reference test**, and it's what keeps you from having to come
back and re-`apply` to finish wiring a resource. A few consequences:

- **Include intended wiring, not just hard prerequisites.** `cdn` `deployAfter`
  `tls_certificate` (the cert ARN goes in the distribution config), `load_balancer`
  `deployAfter` `tls_certificate` (HTTPS listener), `compute_instance` `deployAfter`
  `machine_image` + `ssh_key_pair` (referenced in the launch config).
- **Exclude pure runtime/consumer-side links.** A bucket's config names no IAM
  role (the *policy* references the bucket), so `object_storage` does **not**
  `deployAfter` `identity`. An `identity_provider` is a trust anchor that
  identities reference, so it has no edge to `identity`.
- **Direction keeps it acyclic.** The overlay/attacher `deployAfter` the
  base/attachee, never the reverse — a `load_balancer` never `deployAfter` the
  `waf` that protects it. The whole graph is validated **acyclic**, so a single
  deploy order always exists.
- **Multi-target overlays list every base.** `waf` `deployAfter`
  `[load_balancer, api_gateway]` and `ddos_protection` `deployAfter`
  `[load_balancer, cdn, static_ip]` — multiple entries mean "after whichever of
  these is present in your architecture."
- **Reference the closest node.** If A `deployAfter` `subnet`, it does *not* also
  list `network` (transitively redundant — `subnet` already `deployAfter`
  `network`).
- **Structural prerequisites, not routing/messaging targets.** This is the key
  guardrail. Model what a resource is *built on / lives in / attaches into*
  (`subnet`, `tls_certificate`, a service's `target_group` and `task_definition`)
  — **not** what it *forwards or sends to*. A route's next-hops, an event bus's
  targets, or a "notify this SNS topic" wire-up are runtime targets that can point
  at almost anything; treating them as `deployAfter` edges explodes the graph into
  late-deploying hubs and creates cycles. So `route_table` `deployAfter`
  `[network]` only (not its 9 possible next-hops), and `event_bus` `deployAfter`
  `[identity]` (not its targets).

A read on the arrows: an arrow `A → B` means **deploy B before A**. Reverse the
arrows and you get a valid `terraform apply` order.

## Deploy-order layers

Because the `deployAfter` graph is acyclic, it partitions into ordered **layers**.
Each category is pinned to a `layer` number, and the invariant
`layer(self) > layer(every dependency)` is enforced, so **applying the layers
low-to-high is always a valid order** — nothing is ever missing.

The numbers are **sparse (step-10)** so a future intermediary tier can slot in at
`05`/`15`/… with no renumbering, and they're a **stable, provider-agnostic
contract** (layer `30` means the same tier for AWS, GCP, and Azure):

| # | name | # | name |
|---|---|---|---|
| `00` | foundation | `50` | runtime |
| `10` | core | `60` | workload |
| `20` | platform | `70` | delivery |
| `30` | connectivity | `80` | protection |
| `40` | scaling | | |

### Suggested monorepo layout

The intended use is a **stack-per-layer** monorepo, with the cloud as a parent
folder and the layer number as the apply-order contract:

```
<provider>/<NN>-<group>-<your-service-name>/
```
- `<provider>` (`aws/`, `gcp/`, `azure/`) — each cloud is an independent dependency
  universe, applied on its own.
- `<NN>` — the layer number. **The only enforced part**; apply low to high.
- `<group>` — the resource's nice-named group (suggested), e.g. `aws/30-network-…`,
  `aws/30-ml-…`.
- `<your-service-name>` — free.

A given cloud simply **skips the numbers it has nothing in** (gaps are expected
and portable). Stacks can be split across repos as long as everyone honours the
number ordering. Same-layer stacks never depend on each other, so they apply in
any order within a layer.

## Usage

Requires Python 3.10+ and PyYAML.

```bash
# Validate the packaged schema (exit code 0 = valid).
python -m cloud_architect.validate

# Validate an arbitrary schema file.
python -m cloud_architect.validate path/to/categories.yaml

# Run the test suite.
pip install -e ".[dev]"
pytest
```

### Visualize the deploy-order graph

There is a self-contained static viewer in [`site/`](site/) — no build step, no
server, no internet required. Just open it:

```bash
# (Re)generate the graph data from the schema, then open the page.
python -m cloud_architect.site      # writes site/data.js
open site/index.html                # or double-click it / xdg-open on Linux
```

The viewer renders the categories as a **layered DAG** (foundations on the left,
later-deploying services on the right), colored by group. An arrow `A → B` means
*A deploys after B* (deploy B first). Click any node to highlight its full
deploy-order neighbourhood — everything that must deploy before it and everything
that deploys after it — and to inspect its description, per-provider Terraform
types, and direct relations in the side panel. There's a group filter (the legend
chips), a search box, and zoom (`Ctrl`/`Cmd` + scroll, or the buttons).

`site/data.js` is generated from `schema/categories.yaml` and committed; rerun
`python -m cloud_architect.site` after any schema change (a test enforces it stays
in sync).

### Programmatically

```python
from cloud_architect import load_schema, validate_schema

schema = load_schema()
errors = validate_schema(schema)        # [] means valid
assert not errors

network = schema.get("network")
print(network.terraform_types["gcp"])   # ['google_compute_network']
print(network.providers_with_support()) # ['aws', 'gcp', 'azure']
```

## Validation guarantees

`python -m cloud_architect.validate` (and the test suite) enforce:

- every category `id` is unique and `snake_case`;
- every category declares **exactly** the supported providers in `terraformTypes`;
- every Terraform type is a non-empty string (and not duplicated within a provider);
- every `group` is one of the declared groups;
- every `deployAfter` entry resolves to an existing category, with no self- or
  duplicate references;
- the deploy-order graph is **acyclic**, so a single valid deploy order exists;
- every `layer` is a declared tier and is **strictly greater** than every
  dependency's layer, so applying the folder tiers low-to-high always works.

## Extending the schema

**Add a category:** append an entry to `categories` in
[`schema/categories.yaml`](schema/categories.yaml). Give it a unique `snake_case`
`id`, pick a `group`, write a one-line `description`, and provide a list for every
provider (use `[]` where there is no clean equivalent). Add `deployAfter` edges
using the config-reference test above (does this resource's config reference the
other?), then set `layer` to a declared tier strictly above all its dependencies
(use an existing tier, or insert a new one in `layers` at a gap like `15`). Run
`python -m cloud_architect.validate`.

**Add a provider mapping:** find the category and add Terraform types under the
relevant provider key. Prefer the resource that *is* the concept; reach for an
empty list rather than a misleading near-equivalent.

**Add a provider:** add it to the top-level `providers` list, then add that
provider key (with a list, possibly empty) to **every** category. The validator
will tell you exactly which categories you missed.

**Add a group:** add it to the top-level `groups` list before assigning
categories to it.

After any change, run `python -m cloud_architect.validate` and `pytest`; both
must pass.
