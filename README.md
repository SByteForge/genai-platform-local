# genai-platform-local

A local, production-shaped platform for building and deploying RAG, LLM,
agentic, and voice AI applications — without a cloud account or a cloud bill.

One `kind` Kubernetes cluster on a single machine reproduces the shape of a
real platform: isolated `dev`/`qa`/`prod` environments, shared backing
services, least-privilege RBAC, and a GitHub Actions CI/CD pipeline that
builds, pushes, and deploys an immutable image per commit.

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
              ┌─────────────┼─────────────┐
              │             │             │
         CI workflow   CD workflow    (future app repos
        (hosted runner) (dispatches    register here too)
              │          to runner)
              │             │
              ▼             ▼
            GHCR      self-hosted runner
        (image store)   (this machine)
                            │
                            ▼
              kind cluster: genai-platform-local
              │
              ├── platform namespace (raw manifests, not Helm)
              │     ├── LocalStack   — S3, DynamoDB (emulated AWS)
              │     └── Ollama       — passthrough to host, real local LLM
              │
              ├── dev namespace   (Helm, 1 replica)
              ├── qa namespace    (Helm, 2 replicas, HPA 2-4)
              └── prod namespace  (Helm, 3 replicas, HPA 3-8)
```

Every environment talks to the shared platform services over plain
Kubernetes DNS (`localstack.platform.svc.cluster.local`,
`ollama.platform.svc.cluster.local`) — never `localhost`, so the same app
code runs unmodified in any environment.

## Components

| Layer | Technology | Why |
|---|---|---|
| Orchestration | `kind` (Kubernetes-in-Docker) | Real Kubernetes API, zero cloud dependency |
| AWS emulation | LocalStack (S3, DynamoDB) | Free-tier AWS-compatible services, in-cluster |
| LLM inference | Ollama (host passthrough) | Reuses locally-installed models, no duplicate downloads |
| Packaging | Helm | One chart, three environments, values-driven |
| Governance | Kubernetes RBAC | Per-app, per-environment least privilege — see below |
| CI/CD | GitHub Actions | Hosted runner builds/tests/pushes; self-hosted runner deploys locally |

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

Every application gets a scoped identity, not cluster-admin:

```
Repository → ServiceAccount → Role (namespace-scoped, resourceNames-restricted) → RoleBinding (per environment)
```

- `infra/registry/<app>.yaml` is the source of truth for what an app is
  approved to touch (environments, DynamoDB tables, Ollama access).
- `infra/k8s/rbac/<app>/<env>/` holds that app's `ServiceAccount`/`Role`/
  `RoleBinding` for that one environment — scoped by `resourceNames` to only
  the objects that app's own Helm release creates.
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
location): the CD workflow currently deploys with the runner's own
kubeconfig rather than each app's scoped token; Helm's own release-tracking
Secrets can't be `resourceNames`-scoped because their names are
revision-numbered; the `NetworkPolicy` for LocalStack is written correctly
but inert under `kind`'s default CNI, which doesn't enforce `NetworkPolicy`.

## CI/CD

```
push to dev/qa/main
  → CI: lint, test, build (GitHub-hosted runner)
  → CD: build immutable image (ghcr.io/.../<env>-<sha>, never `latest`) → push to GHCR
  → CD: self-hosted runner (this machine) → helm upgrade → kind cluster
```

Branch → environment mapping: `dev`→`dev`, `qa`→`qa`, `main`→`prod`. A
self-hosted runner is required because GitHub-hosted runners cannot reach a
cluster that only exists on this machine.

## Quickstart

```bash
brew install kind helm kubectl

# 1. Stand up the cluster + shared platform services
python3 infra/scripts/setup-cluster.py

# 2. Build the app image and load it into the cluster
docker build -t rag-platform-api:local .
kind load docker-image rag-platform-api:local --name genai-platform-local

# 3. Apply this app's RBAC, then deploy it to dev
kubectl apply -f infra/k8s/rbac/rag-platform-api/dev/
helm upgrade --install rag-platform-api-dev infra/helm/app-chart \
  -n dev --values deploy/environments/dev/values.yaml --wait

# 4. Try it
kubectl port-forward -n dev svc/rag-platform-api-dev 8000:80
open http://localhost:8000/docs
```

## Repository layout

```
app/                          reference application (FastAPI + boto3 + Ollama)
infra/
  kind/                       kind cluster definition
  k8s/                        namespaces, platform services, RBAC, NetworkPolicy
  helm/app-chart/             reusable chart — any stateless HTTP app can use it
  registry/                   governance source of truth, one file per app
  scripts/                    cluster bootstrap
deploy/environments/          per-environment Helm values (dev/qa/prod)
.github/workflows/            CI, CD, and reusable workflows other app repos can call
```

## Status

Phases 1–4 (cluster, platform services, Helm chart, RBAC governance) are
built and verified live. CI/CD workflows are written but not yet exercised
against a real GitHub remote; a self-hosted runner is not yet registered.
See open items in the project history before treating this as a finished
enterprise platform — it's a working foundation, not a claim of completeness.

## License

MIT — see [LICENSE](LICENSE).
