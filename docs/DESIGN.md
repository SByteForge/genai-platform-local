# Design Document: genai-platform-local

**Status:** dev/qa live and verified; prod pending one combined release.
**Repo:** https://github.com/SByteForge/genai-platform-local

## Summary

A local, single-machine Kubernetes platform that reproduces the shape of a
real production environment — isolated dev/qa/prod, shared backing
services, least-privilege governance, and a working CI/CD pipeline — for
developing RAG/LLM/agentic AI applications without a cloud account. The
platform is the deliverable; the reference application is deliberately
small.

## Problem

Most local AI project scaffolding is a single script hitting an API key.
That's fine for a demo, but it doesn't exercise or demonstrate the parts of
system design that actually matter in production: environment isolation,
least-privilege access control, deployment pipelines, and the operational
judgment to know what's actually enforced versus merely configured. I
wanted a project where every architectural claim could be tested against a
real, running system — not diagrammed and left there.

## Goals

- Real Kubernetes (not docker-compose) with isolated dev/qa/prod environments
- Shared backing services (AWS emulation, LLM inference) reused across
  environments, the way real apps consume managed services rather than
  running their own
- Least-privilege governance: each app/environment gets a scoped identity,
  not cluster-admin — and that claim gets verified against the live API
  server, not just asserted
- A CI/CD pipeline that actually builds, pushes, and deploys an immutable,
  multi-platform image per commit
- Zero cost, zero cloud account required

## Non-goals

GitOps (ArgoCD/Flux), a service mesh, full enterprise IAM simulation, and a
CNI that enforces `NetworkPolicy` were all explicitly deferred. Each is a
legitimate future extension with a clear seam to add it — none was worth
the added complexity for what this project needed to prove first. See
[Known limitations](#known-limitations--accepted-gaps).

## Architecture

```
GitHub (CI hosted runner, CD dispatches to self-hosted runner)
        │
        ▼
kind cluster: genai-platform-local
├── platform namespace (raw manifests, not Helm)
│     ├── LocalStack   — S3, DynamoDB
│     └── Ollama        — passthrough to host, real local LLM inference
├── dev namespace   (Helm, 1 replica)
├── qa namespace    (Helm, 2 replicas, HPA 2-4)
└── prod namespace  (Helm, 3 replicas, HPA 3-8)
```

Full diagram and component table: [README](../README.md#architecture).

## Key decisions and alternatives considered

**Shared platform services vs. one copy per environment.** Real production
apps call a managed AWS/LLM endpoint, not their own private copy. Running
LocalStack/Ollama once, shared across dev/qa/prod (with per-environment
DynamoDB table names for data isolation), models that relationship
honestly instead of pretending every environment is fully independent.
Cost: a bug in the shared LocalStack instance affects every environment
simultaneously — accepted, since the alternative (three LocalStack copies)
would misrepresent how production actually works.

**Ollama: host passthrough vs. in-cluster.** Originally designed in-cluster
(its own Deployment + PVC), until it became clear the developer already had
models pulled locally — running a second copy would re-download
multi-gigabyte models for no benefit. Switched to a Kubernetes
`ExternalName` Service forwarding to `host.docker.internal`. Apps still
resolve `ollama.platform.svc.cluster.local` exactly as before — nothing
downstream changed — but the cluster now depends on a process running
outside it. Documented tradeoff, not a silent one.

**Helm for the app, raw manifests for the platform.** The app's shape
genuinely differs per environment (replicas, resources, image tag) — that's
Helm's actual use case. The platform services are static, not templated per
environment, so raw `kubectl apply` manifests avoid pretending they need
templating they don't.

**Self-hosted CI/CD runner.** GitHub-hosted runners are cloud VMs with no
network path to a cluster that only exists on one laptop. A self-hosted
runner, registered on that same machine, is the only way CD can actually
reach `kind`. Real cost: this repo's CD workflow can only run on a machine
where that runner is registered and listening — a deliberate, documented
constraint of "local," not an oversight.

**Governance: `resourceNames`-scoped RBAC instead of per-app namespaces.**
The environment model (one shared `dev`/`qa`/`prod` namespace per app) meant
namespace boundaries alone couldn't isolate app-a from app-b. Kubernetes
`Role` objects can restrict specific verbs to specific object names via
`resourceNames` — this rides on the existing Helm release naming convention
(`<app>-<environment>`) with zero architecture change. What this doesn't
fully close is documented in the two debugging stories below, found by
actually testing it, not by assumption.

## War stories: two real bugs found by testing, not review

**1. `resourceNames` silently breaks `list`/`watch`.** The first RBAC Role
scoped every verb — `get, list, watch, create, update, patch, delete` — to
a `resourceNames` list. `helm upgrade` failed immediately: `list` and
`watch` operate on the whole collection endpoint, with no single object
name in the request to check `resourceNames` against, so the API server
denies them outright whenever `resourceNames` is present — this isn't
documented anywhere obvious, it's an emergent property of how the
authorizer evaluates the request. `create` has the mirror problem: the
object doesn't exist yet, so there's no name to check, meaning Kubernetes
does *not* restrict `create` by `resourceNames` at all, regardless of what's
listed. Fix: split every resource's rules into two — `list/watch/create`
unrestricted, `get/update/patch/delete` `resourceNames`-scoped. Net effect,
stated plainly rather than oversold: this genuinely stops an app from
reading or modifying another app's *existing* objects (the realistic
accidental-cross-app-damage case), but core RBAC alone cannot stop it from
*creating* a wrongly-named object in its own namespace — closing that
fully needs an admission controller, out of scope here by design.

**2. Helm v4's status watcher follows ownership chains.** After fixing the
RBAC gap above, `helm upgrade --wait` still hung, reporting
`actualStatus=Unknown` for a Deployment that `kubectl get` showed was
perfectly healthy. Root cause: Helm v4 uses `kstatus`, which determines
real rollout status by following Deployment → ReplicaSet → Pod ownership,
not just reading the Deployment's own status conditions — undocumented
behavior discovered by adding `--debug` and watching it stall on a resource
kind (`ReplicaSet`) the Role had never granted any permission on at all.
Fix: add read-only `list/watch/get` on `replicasets`.

Both fixes were verified by running a full `helm upgrade --wait` using
*only* the scoped `ServiceAccount`'s token — no admin credentials — against
the live cluster, before wiring either into the CI/CD pipeline.

## Known limitations / accepted gaps

| Gap | Why it's accepted |
|---|---|
| `create` can't be `resourceNames`-scoped | Core RBAC limitation; closing fully needs an admission controller (Kyverno/OPA), deliberately deferred |
| Helm's own release Secrets aren't `resourceNames`-scoped | Names are revision-numbered, unpredictable in advance; only matters once 2+ apps share a namespace |
| `NetworkPolicy` for LocalStack is written but inert | `kind`'s default CNI doesn't enforce `NetworkPolicy`; fixing needs Calico, deferred as unnecessary complexity for a local cluster |
| LocalStack community edition enforces no IAM | Any credentials reach any table/bucket; real fix needs LocalStack Pro or a policy proxy, both paid-tier-adjacent, avoided by design |
| Self-hosted runner has full access to the host machine | Inherent to the self-hosted runner model; acceptable for a personal machine, would need sandboxing for anything shared |

## What I'd do differently at real production scale

- Federate CI identity via GitHub OIDC directly into Kubernetes token
  exchange, instead of a stored long-lived `ServiceAccount` token
- Swap `kindnetd` for a policy-enforcing CNI so `NetworkPolicy` is real,
  not aspirational
- Add an admission controller to close the `create`-by-`resourceNames` gap
  properly
- Move the registry (governance source of truth) to its own repo the
  moment a different team owns access approval than owns platform
  engineering — not before, since splitting it earlier adds process
  without a corresponding governance benefit

## What was actually verified live (not just written)

- `kubectl auth can-i` proving RBAC denies cross-namespace and
  cross-`platform` access
- Full CI → GHCR (multi-platform) → self-hosted runner → Helm deploy,
  watched end to end through GitHub Actions logs
- The app answering real questions through Ollama, grounded in documents
  stored in LocalStack's DynamoDB, via the Swagger UI
- `helm upgrade --wait` succeeding using only each app's scoped identity,
  proven before that identity was wired into CI
