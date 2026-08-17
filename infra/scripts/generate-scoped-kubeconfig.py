#!/usr/bin/env python3
"""Build a kubeconfig scoped to one app's <app>-deployer ServiceAccount in one
environment, using the long-lived token Secret created by
infra/k8s/rbac/<app>/<env>/token-secret.yaml. Used by the self-hosted CD
runner instead of its own admin kubeconfig, so a deploy can only do what
that app's Role actually grants — verified for real against a live cluster,
see infra/k8s/rbac/rag-platform-api/dev/role.yaml for what that took.

Usage: generate-scoped-kubeconfig.py <app_name> <environment> <output_path>
"""
import base64
import json
import subprocess
import sys


def kubectl_json(*args: str):
    result = subprocess.run(
        ["kubectl", *args, "-o", "json"], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <app_name> <environment> <output_path>")
    app_name, environment, output_path = sys.argv[1:4]

    config = kubectl_json("config", "view", "--minify", "--flatten")
    cluster = config["clusters"][0]["cluster"]
    server = cluster["server"]
    ca_data = cluster["certificate-authority-data"]

    secret = kubectl_json(
        "get", "secret", f"{app_name}-deployer-token", "-n", environment
    )
    token = base64.b64decode(secret["data"]["token"]).decode()

    kubeconfig = f"""apiVersion: v1
kind: Config
clusters:
  - name: scoped-cluster
    cluster:
      server: {server}
      certificate-authority-data: {ca_data}
contexts:
  - name: scoped
    context:
      cluster: scoped-cluster
      namespace: {environment}
      user: {app_name}-deployer
current-context: scoped
users:
  - name: {app_name}-deployer
    user:
      token: {token}
"""
    with open(output_path, "w") as f:
        f.write(kubeconfig)
    print(f"Wrote scoped kubeconfig for {app_name}/{environment} to {output_path}")


if __name__ == "__main__":
    main()
