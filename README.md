# genai-platform-local

A local, production-shaped platform for building and deploying RAG, LLM,
agentic, and voice AI applications — without a cloud account or a cloud bill.

One `kind` Kubernetes cluster on a single machine reproduces the shape of a
real platform: isolated `dev`/`qa`/`prod` environments, shared backing
services, least-privilege RBAC, and a real GitOps pipeline — GitHub Actions
builds and pushes an image, ArgoCD is what actually applies it to the
cluster.

📄 **[Read the design doc](docs/DESIGN.md)** — architecture decisions,
alternatives considered, and real bugs found by testing this against a live
cluster (not just written and assumed correct).

## Why this exists

Most "local AI demo" repos are a single script that calls an API. This is
the opposite bet: a platform an application deploys *onto*, so the
application code stays simple while the platform absorbs the parts that
actually make software production-shaped — environment isolation, governed
access, and a real deploy pipeline. The reference application included
(`app/`) is deliberately small; the platform is the point.

## Architecture

```
GitHub
   │
   ├── CI workflow (hosted runner): lint, test, build
   └── CD workflow (hosted runner): build image → push to GHCR
              → bump the image tag in this repo's git (the actual trigger)
                            │
                            ▼
              kind cluster: genai-platform-local
              │
              ├── argocd namespace
              │     watches dev/qa/main branches, applies any change
              │     automatically — this is what actually touches the
              │     cluster now, not CI/CD directly
              │
              ├── platform namespace (raw manifests, not Helm)
              │     ├── LocalStack   — S3, DynamoDB (emulated AWS)
              │     └── Ollama       — in-cluster, real local LLM inference
              │
              ├── dev namespace   (Helm, 1 replica)
              ├── qa namespace    (Helm, 2 replicas, HPA 2-4)
              └── prod namespace  (Helm, 3 replicas, HPA 3-8)
```

Every environment talks to the shared platform services over plain
Kubernetes DNS (`localstack.platform.svc.cluster.local`,
`ollama.platform.svc.cluster.local`) — never `localhost`, so the same app
code runs unmodified in any environment.

Each environment is also reachable through `ingress-nginx` at its own
hostname, routed by the `kind-config.yaml` port mapping (host `:8080` →
node `:80`). Add these to `/etc/hosts` to browse them directly:

```
127.0.0.1 dev.api.local
127.0.0.1 qa.api.local
127.0.0.1 prod.api.local
```

Then, e.g., `curl http://dev.api.local:8080/health`. Without the
`/etc/hosts` entries, the same routing can be exercised with an explicit
Host header: `curl -H "Host: dev.api.local" http://localhost:8080/health`.

## Components

| Layer | Technology | Why |
|---|---|---|
| Orchestration | `kind` (Kubernetes-in-Docker) | Real Kubernetes API, zero cloud dependency |
| AWS emulation | LocalStack (S3, DynamoDB) | Free-tier AWS-compatible services, in-cluster |
| LLM inference | Ollama (in-cluster) | Self-contained — works on a fresh clone, no local Ollama install required |
| Packaging | Helm | One chart, three environments, values-driven |
| Governance | Kubernetes RBAC | Per-app, per-environment least privilege — see below |
| CI | GitHub Actions (hosted runner) | Lint, test, build, push an immutable image to GHCR |
| CD → deploy | ArgoCD (in-cluster) | Watches git, applies automatically — CI never touches the cluster directly |
| Ingress | ingress-nginx | Host-based routing (`dev/qa/prod.api.local`) to each environment |
| Autoscaling data | metrics-server | Gives the HPAs real CPU data (`kind` doesn't ship this by default) |

## Environments

| Environment | Namespace | Replicas | Autoscaling | Branch |
|---|---|---|---|---|
| Development | `dev` | 1 | off | `dev` |
| QA | `qa` | 2 | HPA 2–4 | `qa` |
| Production | `prod` | 3 | HPA 3–8 | `main` |

Each environment gets its own DynamoDB table (`documents-dev`,
`documents-qa`, `documents-prod`) inside the one shared LocalStack instance,
so environments can never read or overwrite each other's data.

## Governance model

The pattern below is implemented here, and also written up as a
standalone, platform-agnostic reference in
[k8s-privilege-engine](https://github.com/SByteForge/k8s-privilege-engine)
— useful if you want the governance model on its own, without the rest of
this platform.

Every application gets a scoped identity, not cluster-admin:

```
Repository → ServiceAccount → Role (namespace-scoped, resourceNames-restricted) → RoleBinding (per environment)
```

- `infra/registry/<app>.yaml` is the source of truth for what an app is
  approved to touch (environments, DynamoDB tables, Ollama access).
- `infra/k8s/rbac/<app>/<env>/` holds that app's `ServiceAccount`/`Role`/
  `RoleBinding`/token `Secret` for that one environment — scoped by
  `resourceNames` to only the objects that app's own Helm release creates.
- `infra/argocd/projects.yaml` scopes which ArgoCD `Application` can deploy
  to which namespace — the GitOps-layer equivalent of the RBAC above, since
  ArgoCD is what actually applies changes to the cluster now.
- The `platform` namespace has its own RBAC; no application's identity is
  ever bound there. Verified live:

  ```
  $ kubectl auth can-i update deployment/rag-platform-api-dev -n dev \
      --as=system:serviceaccount:dev:rag-platform-api-deployer
  yes
  $ kubectl auth can-i get deployments -n platform \
      --as=system:serviceaccount:dev:rag-platform-api-deployer
  no
  ```

Known, documented gaps (not oversights — see inline comments at each
location): `create` can't be `resourceNames`-scoped (a Kubernetes RBAC
limitation, not this platform's choice — closing it fully needs an
admission controller); Helm's own release-tracking Secrets can't be
`resourceNames`-scoped either, since their names are revision-numbered; the
`NetworkPolicy` for LocalStack is written correctly but inert under `kind`'s
default CNI, which doesn't enforce `NetworkPolicy`.

## CI/CD (GitOps via ArgoCD)

```
push to dev/qa/main
  → CI: lint, test, build (GitHub-hosted runner)
  → CD: build immutable image (ghcr.io/.../<env>-<sha>, never `latest`) → push to GHCR
  → CD: bump that tag into deploy/environments/<env>/values.yaml, commit, push [skip ci]
  → ArgoCD (in-cluster): detects the change, applies it, self-heals any drift
  → CD: waits for ArgoCD to report Synced at that exact commit + Healthy, then smoke-tests
```

CD does **not** run `helm upgrade` itself — it only ever changes git. ArgoCD
is the only thing that touches the cluster for a deploy. Running `helm
upgrade` directly (e.g. by hand, for local testing) will fight ArgoCD's
`selfHeal` — whichever doesn't match git gets reverted. See
[Local development](#local-development-without-ci) below for the safe way
to test changes before pushing.

Branch → environment mapping: `dev`→`dev`, `qa`→`qa`, `main`→`prod`. A
self-hosted runner is required for CI/CD to run at all, since GitHub-hosted
runners can't reach a cluster or a `git push` target that only exists on
this machine — but the runner never touches the cluster's actual state; it
only builds images and pushes git commits. ArgoCD is the only in-cluster
component with deploy access.

## Quickstart

```bash
brew install kind helm kubectl

# 1. Stand up the cluster + shared platform services + ArgoCD
python3 infra/scripts/setup-cluster.py
kubectl apply -f infra/argocd/projects.yaml
kubectl apply -f infra/argocd/applications.yaml

# 2. Register a self-hosted GitHub Actions runner on this machine
#    (Settings → Actions → Runners → New self-hosted runner)

# 3. Push to dev/qa/main — CI/CD + ArgoCD take it from here
git push origin dev
```

There is no manual deploy step — once ArgoCD's `Application`s exist, every
push to `dev`/`qa`/`main` flows through CI/CD into a real, GitOps-managed
deployment automatically.

### Local development, without CI

To test a change to the app or chart before pushing:

```bash
docker build -t rag-platform-api:local .
kind load docker-image rag-platform-api:local --name genai-platform-local
helm template infra/helm/app-chart -f deploy/environments/dev/values.yaml   # render only, no cluster touched
```

Don't run `helm upgrade` against the cluster directly while ArgoCD is
watching that namespace — it will be reverted. To genuinely bypass ArgoCD
for local iteration, pause the `Application` first:
`kubectl patch application rag-platform-api-dev -n argocd --type merge -p '{"spec":{"syncPolicy":null}}'`,
then resume it (re-apply `infra/argocd/applications.yaml`) when done.

## Onboarding another application

Any app can consume this platform's shared LocalStack/Ollama and its CI/CD
patterns without forking it:

1. **Register it**: add `infra/registry/<app>.yaml` in this repo, declaring
   which environments and platform resources (DynamoDB tables, S3 prefixes,
   Ollama access) it's approved to use.
2. **Provision its identity**: add `infra/k8s/rbac/<app>/<env>/` (copy the
   `rag-platform-api` folders as a template) — `ServiceAccount`, `Role`,
   `RoleBinding`, token `Secret`, scoped the same way.
3. **Give it an ArgoCD `Application`**: add an entry to
   `infra/argocd/applications.yaml` (and an `AppProject` in `projects.yaml`
   if it needs a namespace that doesn't already have one) pointing at the
   new app's own chart/values.
4. **In the new app's own repo**, reuse this platform's CI/CD instead of
   writing new workflows:
   ```yaml
   uses: SByteForge/genai-platform-local/.github/workflows/reusable-build-push.yml@main
   ```
   Its own `values.yaml` sets `AWS_ENDPOINT_URL`/`OLLAMA_URL` to this
   platform's shared services, same as `rag-platform-api` does.
5. The new app's CD only needs to build and push an image and bump its own
   `values.yaml` tag — the same self-hosted runner (registered once, on the
   machine running the cluster) can serve multiple app repos.

## Repository layout

```
app/                          reference application (FastAPI + boto3 + Ollama)
docs/                          design doc — decisions, alternatives, real bugs found
infra/
  kind/                       kind cluster definition
  k8s/                        namespaces, platform services, RBAC, NetworkPolicy
  helm/app-chart/             reusable chart — any stateless HTTP app can use it
  argocd/                     AppProjects + Applications — GitOps deploy config
  registry/                   governance source of truth, one file per app
  scripts/                    cluster bootstrap
deploy/environments/          per-environment Helm values (dev/qa/prod)
.github/workflows/            CI, CD, and reusable workflows other app repos can call
```

## License

MIT — see [LICENSE](LICENSE).
