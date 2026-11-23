"""Reproduce the approved deployment without changing a shared Azure CLI default account."""

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv/bin/python")
SUBSCRIPTION = "a165edb0-c8fb-4569-9e3e-40495d678b10"
GROUP = "rg-gpt-live-python-dev"
NAME = "gpt-live-python-dev"


def command(*args, capture=False):
    return subprocess.run(list(args), cwd=ROOT, check=True, text=True, capture_output=capture)


def az(*args, capture=False):
    return command("az", *args, "--subscription", SUBSCRIPTION, capture=capture)


def outputs():
    values = json.loads(az("deployment", "sub", "show", "--name", NAME,
                           "--query", "properties.outputs", "-o", "json", capture=True).stdout)
    return {key.upper(): value for key, value in values.items()}


def provision():
    # Graph's unscoped "signed-in-user" can select a different cached tenant. Use this ARM token's oid.
    raw = az("account", "get-access-token", "--resource", "https://management.azure.com/",
             "--query", "accessToken", "-o", "tsv", capture=True).stdout.strip()
    segment = raw.split(".")[1]
    principal = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))["oid"]
    params = [
        "environmentName=" + NAME, "location=eastus2", "resourceGroupName=" + GROUP, "principalId=" + principal,
    ]
    command("az", "bicep", "build", "--file", "infra/main.bicep", "--outfile", ".build/main.json")
    for action in ("validate", "what-if", "create"):
        az("deployment", "sub", action, "--location", "eastus2", "--name", NAME,
           "--template-file", ".build/main.json", "--parameters", *params,
           "--query", "properties.provisioningState" if action != "what-if" else "status", "-o", "json")


def deploy_code():
    command(PYTHON, "scripts/package_functions.py")
    deployment = outputs()
    for service in ("faq", "repair", "verification"):
        source = ROOT / ".build/functions" / service
        app = deployment[f"SERVICE_{service.upper()}_NAME"]["value"]
        # Core Tools selects Flex OneDeploy. Pass the correctly scoped token through stdin, not argv or logs.
        token = az("account", "get-access-token", "--resource", "https://management.azure.com/",
                   "-o", "json", capture=True).stdout
        func = ROOT / ".build/tools/node_modules/.bin/func"
        subprocess.run(
            [str(func), "azure", "functionapp", "publish", app, "--python", "--build", "remote",
             "--subscription", SUBSCRIPTION, "--access-token-stdin"],
            cwd=source, input=token, text=True, check=True,
            env={**os.environ, "VIRTUAL_ENV": str(ROOT / ".venv"),
                 "PATH": str(ROOT / ".venv/bin") + os.pathsep + os.environ.get("PATH", "")},
        )
        print(f"Deployed {service}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provision", action="store_true", help="Validate, preview and provision approved resources")
    parser.add_argument("--code", action="store_true", help="Build and remotely deploy three function packages")
    args = parser.parse_args()
    if not (args.provision or args.code):
        parser.error("Choose --provision and/or --code; operations may incur Azure charges")
    if args.provision:
        provision()
    if args.code:
        deploy_code()
