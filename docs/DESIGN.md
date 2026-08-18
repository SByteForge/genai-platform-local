# Design Document: genai-platform-local

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
│     └── Ollama        — in-cluster, real local LLM inference (llama3.2:1b)
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

**Ollama: in-cluster, reversed once, back again.** Originally in-cluster
(Deployment + PVC), then switched to a Kubernetes `ExternalName` Service
forwarding to `host.docker.internal`, once it became clear the developer
already had models pulled locally and a second copy would re-download
several gigabytes for no benefit. Reversed back to in-cluster once the
actual goal was named explicitly: this repo exists partly to be cloned and
run by other people, and requiring them to pre-install Ollama locally
defeats that. Both directions kept the same DNS name
(`ollama.platform.svc.cluster.local`) and required zero app-level changes —
only the platform manifest changed either time, which is exactly what that
DNS indirection is for.

Reversing it surfaced two more real problems, not assumed ones:

- The default model (`mistral`, 7B) `OOMKilled` repeatedly — Docker
  Desktop's whole VM has 7.7GB total, shared with everything else in the
  cluster, and `mistral` needs more than that to actually run inference,
  not just load. Bumping the memory limit would have just pushed the OOM up
  a level. Real fix: a small model (`llama3.2:1b`, 1.3GB) that fits the
  constraint this platform is designed to run under, with the Deployment's
  resources right-sized to match. Stated plainly: `llama3.2:1b` is
  noticeably weaker than `mistral` at following "answer using only this
  context" instructions — that's the real cost of the portability win, not
  a hidden one.
- The pull `Job` reported `Complete` on a download that had actually failed
  partway through (a TLS handshake timeout reaching Ollama's registry,
  visible in the job's own logs) — because `curl -f` only checks the HTTP
  status code, and Ollama's pull API streams `200 OK` the entire time,
  reporting errors *inside* the JSON body instead. Fixed by checking the
  last streamed line for `{"status":"success"}` explicitly and failing the
  Job otherwise — confirmed live, both for a real success and a forced
  failure.

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
- The in-cluster Ollama pod's actual memory usage under a real inference
  request (not just at idle) before and after the model-size fix —
  `OOMKilled`/`Exit Code: 137` on `mistral`, stable at ~1.9GB on
  `llama3.2:1b`
- A forced failure of the pull `Job`'s new success check, to confirm it
  actually fails loudly instead of reporting false positives like the
  version it replaced
