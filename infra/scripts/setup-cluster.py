#!/usr/bin/env python3
"""Phase 1 + 2: local kind cluster, namespaces, LocalStack, and an in-cluster
Ollama (fully self-contained — no local Ollama install required). Idempotent
— safe to re-run."""

import shutil
import subprocess
import sys
from pathlib import Path

CLUSTER_NAME = "genai-platform-local"
SCRIPT_DIR = Path(__file__).resolve().parent
INFRA_DIR = SCRIPT_DIR.parent


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, streaming its output live, like the shell would."""
    print(f"$ {' '.join(args)}")
    return subprocess.run(args, check=check)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"{name} is required: brew install {name}")


def cluster_exists(name: str) -> bool:
    result = subprocess.run(
        ["kind", "get", "clusters"], capture_output=True, text=True, check=True
    )
    return name in result.stdout.split()


def create_cluster() -> None:
    if cluster_exists(CLUSTER_NAME):
        print(f"kind cluster '{CLUSTER_NAME}' already exists, skipping create.")
        return
    run("kind", "create", "cluster", "--config", str(INFRA_DIR / "kind" / "kind-config.yaml"))


def apply_manifests() -> None:
    run("kubectl", "config", "use-context", f"kind-{CLUSTER_NAME}")

    print("Applying namespaces...")
    run("kubectl", "apply", "-f", str(INFRA_DIR / "k8s" / "00-namespaces.yaml"))

    print("Applying platform services (LocalStack, in-cluster Ollama)...")
    run("kubectl", "apply", "-f", str(INFRA_DIR / "k8s" / "platform" / "localstack.yaml"))
    run("kubectl", "apply", "-f", str(INFRA_DIR / "k8s" / "platform" / "ollama.yaml"))
    run("kubectl", "apply", "-f", str(INFRA_DIR / "k8s" / "platform" / "networkpolicy.yaml"))

    print("Applying platform RBAC (platform-deployer only, no app has access here)...")
    run("kubectl", "apply", "-f", str(INFRA_DIR / "k8s" / "rbac" / "platform"))

    print("Waiting for LocalStack to be ready...")
    run(
        "kubectl", "rollout", "status", "deployment/localstack",
        "-n", "platform", "--timeout=420s",
    )

    print("Waiting for Ollama to be ready...")
    run(
        "kubectl", "rollout", "status", "deployment/ollama",
        "-n", "platform", "--timeout=300s",
    )

    print("Pulling default Ollama model (mistral, ~4.4GB — first run only, can take a while)...")
    run("kubectl", "apply", "-f", str(INFRA_DIR / "k8s" / "platform" / "ollama-pull-model.yaml"))
    run(
        "kubectl", "wait", "--for=condition=complete", "job/ollama-pull-model",
        "-n", "platform", "--timeout=1200s", check=False,
    )


def install_metrics_server() -> None:
    print("Installing metrics-server (needed for HPA to see real CPU usage)...")
    run(
        "kubectl", "apply", "-f",
        "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
    )
    # kind's kubelet certs aren't signed for the hostname metrics-server expects
    # by default; --kubelet-insecure-tls is the standard local-cluster workaround.
    run(
        "kubectl", "patch", "deployment", "metrics-server", "-n", "kube-system",
        "--type=json",
        "-p=[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"--kubelet-insecure-tls\"}]",
        check=False,  # no-op (and returns nonzero) if already patched
    )
    run("kubectl", "rollout", "status", "deployment/metrics-server", "-n", "kube-system", "--timeout=90s")


def install_ingress_nginx() -> None:
    print("Installing ingress-nginx (kind-specific manifest, uses the hostPorts from kind-config.yaml)...")
    run(
        "kubectl", "apply", "-f",
        "https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml",
    )
    run(
        "kubectl", "wait", "--namespace", "ingress-nginx",
        "--for=condition=ready", "pod", "--selector=app.kubernetes.io/component=controller",
        "--timeout=180s",
    )


def main() -> None:
    require_tool("kind")
    require_tool("kubectl")

    create_cluster()
    apply_manifests()
    install_metrics_server()
    install_ingress_nginx()

    print(
        f"Done. Cluster '{CLUSTER_NAME}' ready: "
        "namespaces platform/dev/qa/prod, LocalStack + Ollama (in-cluster), "
        "metrics-server, and ingress-nginx up."
    )


if __name__ == "__main__":
    main()