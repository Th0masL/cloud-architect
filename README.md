# cloud-architect

A **vendor-neutral, provider-aware schema** of cloud resources. It
describes cloud architecture in generic terms (a `virtual_network`, a
`kubernetes_cluster`, an `object_storage` bucket) and maps each resource onto the
concrete Terraform resource types for **AWS**, **GCP**, and **Azure** — together
with the **deploy-order** edges between resources.

The schema is the single source of truth in
[`schema/resources.yaml`](schema/resources.yaml). The Python package
[`cloud_architect/`](cloud_architect/) loads and validates it.

## Why vendor-neutral?

Most multi-cloud tooling is *provider-first*: it models `aws_vpc`,
`google_compute_network`, and `azurerm_virtual_network` as three unrelated
things. That fragments the model and makes cross-provider reasoning impossible.

Here we invert it. The **resource** (`virtual_network`) is primary; provider resource
types are just how that resource is realized on a given cloud:

```yaml
- id: virtual_network
  category: networking
  layer: 0
  description: Isolated virtual network (VPC / VNet).
  terraformResources:
    aws: [aws_vpc]
    gcp: [google_compute_network]
    azure: [azurerm_virtual_network]
  deployAfter: []
```

Two principles fall out of this:

1. **Never force a false equivalence.** When a provider has no clean equivalent
   for a resource, its list is **empty** — we do not bolt on a near-miss resource.
   For example, GCP has no security-group *container*, so `firewall` is
   `gcp: []` and the rules live under `firewall_rule` (`google_compute_firewall`).
   Likewise `container_instance` is essentially Azure Container Instances, so AWS
   and GCP are empty there.
2. **Do not merge dissimilar runtime models** just to shrink the resource count.
   `container_service` (ECS service / Cloud Run / Container Apps),
   `kubernetes_cluster` (EKS / GKE / AKS), `container_task_definition` (ECS task
   defs), and `container_job` (Batch / Cloud Run Jobs / Container Apps Jobs) stay
   separate because they are genuinely different runtime resources.

## Schema structure

The file has five top-level keys:

| Key          | Meaning                                                              |
| ------------ | ------------------------------------------------------------------- |
| `providers`  | The supported Terraform providers — the 3 clouds plus cross-cutting `kubernetes`/`helm`/`kubectl`. |
| `universes`  | Provider groups sharing a deploy context (`cloud`, `cluster`). A `deployAfter` edge crossing two is a **hard** stack boundary. |
| `categories`     | The categories (`networking`, `data`, `compute`, …).         |
| `layers`     | The ordered deploy tiers — each a sparse `number` + `description`, shown as `L<NN>`. |
| `resources` | The list of resources. Each follows the shape below.                 |

Each resource:

| Field            | Rule                                                                       |
| ---------------- | -------------------------------------------------------------------------- |
| `id`             | Unique, `snake_case`, the generic resource name.                            |
| `category`          | One of the declared `categories` (the resource's domain).         |
| `layer`          | One of the declared `layers[].number` — its deploy tier (see below).       |
| `description`    | One-line human summary.                                                     |
| `terraformResources` | Map of the **applicable** providers → Terraform types. Clouds use `aws`/`gcp`/`azure` (`[]` = no equivalent); cross-cutting resources use `kubernetes`/`helm`/`kubectl`. |
| `deployAfter`    | List of other resource `id`s that must be deployed **before** this one.    |

The eight categories are `networking`, `security`, `compute`, `storage`, `data`,
`governance`, `ml`, and `devops`.

### Cross-cutting providers

Besides the three clouds, the schema covers **cloud-agnostic** Terraform providers
whose resources are the same regardless of where the cluster runs: `kubernetes`,
`helm`, and `kubectl`. A resource lists only the providers that apply — a
`k8s_deployment` carries just a `kubernetes:` list, and a cloud resource never
carries an empty `kubernetes: []`. These form their own **provider universe** (a
`kubernetes/…` folder tree) that deploys *after* a `kubernetes_cluster` exists, so
they sit in the upper layers (`L30`+), rooted on the cluster.

### Hard vs soft deploy-order edges

A `deployAfter` edge is **soft** when both ends are in the same universe —
terraform can order them *within one stack*, so the layer gap is only advisory and
you may merge those layers freely. An edge is **hard** when it crosses universes
(every k8s resource's root dependency on `kubernetes_cluster`): the downstream
provider has to be configured from an already-applied resource, so it **must** be a
separate, earlier stack. Hard edges are **derived** from `universes` — no per-edge
annotation — so they can't drift out of sync; query them with
`Schema.hard_edges()`, and the viewer draws them in amber. They are the only cuts
you're *forced* to make when grouping layers into stacks; everything else is an
operational choice.

## How `deployAfter` is modeled

`deployAfter` is a **deploy-ordering** relation, not a hard "cannot exist
without" requirement. The precise, Terraform-shaped test for an edge is:

> **A `deployAfter` B** when standing up A *the way you intend it* means A's own
> resource configuration (or its standard companion association resource) has to
> **reference B** — so B must be deployed first. Assume every resource here is
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
  list `virtual_network` (transitively redundant — `subnet` already `deployAfter`
  `virtual_network`).
- **Structural prerequisites, not routing/messaging targets.** This is the key
  guardrail. Model what a resource is *built on / lives in / attaches into*
  (`subnet`, `tls_certificate`, a service's `target_group` and `task_definition`)
  — **not** what it *forwards or sends to*. A route's next-hops, an event bus's
  targets, or a "notify this SNS topic" wire-up are runtime targets that can point
  at almost anything; treating them as `deployAfter` edges explodes the graph into
  late-deploying hubs and creates cycles. So `route_table` `deployAfter`
  `[virtual_network]` only (not its 9 possible next-hops), and `event_bus` `deployAfter`
  `[identity]` (not its targets).

A read on the arrows: an arrow `A → B` means **deploy B before A**. Reverse the
arrows and you get a valid `terraform apply` order.

## Deploy-order layers

Because the `deployAfter` graph is acyclic, it partitions into ordered **layers**.
Each resource is pinned to a `layer` number, and the invariant
`layer(self) > layer(every dependency)` is enforced, so **applying the layers
low-to-high is always a valid order** — nothing is ever missing.

The numbers are **sparse (step-10)** so a future intermediary tier can slot in at
`05`/`15`/… with no renumbering, and they're a **stable, provider-agnostic
contract** (layer `30` means the same tier for AWS, GCP, and Azure):

| Layer | What's in it |
|---|---|
| `L00` | Roots — depend on nothing. |
| `L10` | Built directly on the L00 roots. |
| `L20` | Managed services and clusters. |
| `L30` | Gateways, load balancers, platform extensions. |
| `L40` | Fleets, endpoints, attachments. |
| `L50` | Where workloads run. |
| `L60` | Running services and jobs. |
| `L70` | Edge delivery. |
| `L80` | Edge protection. |

### Suggested monorepo layout

The intended use is a **stack-per-layer** monorepo, with the cloud as a parent
folder and the layer number as the apply-order contract:

```
<provider>/<NN>-<category>-<your-service-name>/
```
- `<provider>` (`aws/`, `gcp/`, `azure/`) — each cloud is an independent dependency
  universe, applied on its own. Cloud-agnostic resources live in their own universe
  too (`kubernetes/`), applied after the cloud that built the cluster.
- `<NN>` — the layer number. **The only enforced part**; apply low to high.
- `<category>` — the resource's category (suggested), e.g. `aws/30-networking-…`,
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
python -m cloud_architect.validate path/to/resources.yaml

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

The viewer renders the resources as a **layered DAG** (lowest layer `L00` on the
left, later-deploying layers to the right), colored by category. An arrow
`A → B` means *A deploys after B* (deploy B first). Click any node to highlight its full
deploy-order neighbourhood — everything that must deploy before it and everything
that deploys after it — and to inspect its description, per-provider Terraform
types, and direct relations in the side panel. There's a category filter (the legend
chips), a search box, and zoom (`Ctrl`/`Cmd` + scroll, or the buttons).

`site/data.js` is generated from `schema/resources.yaml` and committed; rerun
`python -m cloud_architect.site` after any schema change (a test enforces it stays
in sync).

### Programmatically

```python
from cloud_architect import load_schema, validate_schema

schema = load_schema()
errors = validate_schema(schema)        # [] means valid
assert not errors

virtual_network = schema.get("virtual_network")
print(virtual_network.terraform_resources["gcp"])   # ['google_compute_network']
print(virtual_network.providers_with_support()) # ['aws', 'gcp', 'azure']
```

## Validation guarantees

`python -m cloud_architect.validate` (and the test suite) enforce:

- every resource `id` is unique and `snake_case`;
- every `terraformResources` key is a declared provider, and at least one list is non-empty;
- every Terraform type is a non-empty string (and not duplicated within a provider);
- every `category` is one of the declared categories;
- every `deployAfter` entry resolves to an existing resource, with no self- or
  duplicate references;
- the deploy-order graph is **acyclic**, so a single valid deploy order exists;
- every `layer` is a declared tier and is **strictly greater** than every
  dependency's layer, so applying the folder tiers low-to-high always works.

## Extending the schema

**Add a resource:** append an entry to `resources` in
[`schema/resources.yaml`](schema/resources.yaml). Give it a unique `snake_case`
`id`, pick a `category`, write a one-line `description`, and list Terraform types
under the providers that apply (clouds use `aws`/`gcp`/`azure`, `[]` = no
equivalent; cross-cutting resources use `kubernetes`/`helm`/`kubectl`). Add `deployAfter` edges
using the config-reference test above (does this resource's config reference the
other?), then set `layer` to a declared tier strictly above all its dependencies
(use an existing tier, or insert a new one in `layers` at a gap like `15`). Run
`python -m cloud_architect.validate`.

**Add a provider mapping:** find the resource and add Terraform types under the
relevant provider key. Prefer the resource that *is* the resource; reach for an
empty list rather than a misleading near-equivalent.

**Add a provider:** add it to the top-level `providers` list. Resources that use
it add a `terraformResources` key; those that don't simply omit it (no empty
placeholders needed).

**Add a category:** add it to the top-level `categories` list before assigning
resources to it.

After any change, run `python -m cloud_architect.validate` and `pytest`; both
must pass.
