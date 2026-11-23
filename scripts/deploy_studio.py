"""Dedicated authenticated ACA rollout; never provisions or republishes the MCP apps."""

import argparse
import base64
import json
import os
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from gpt_live_python.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "a165edb0-c8fb-4569-9e3e-40495d678b10"
GROUP = "rg-gpt-live-python-dev"
DEPLOYMENT = "gpt-live-python-studio"
REGISTRY = "zhenweiacr"
SERVER = REGISTRY + ".azurecr.io"
PREFIX = SERVER + "/gpt-live-python/studio"
APP_NAME = "ca-gpt-live-python"


class Rollout:
    def __init__(self, reuse_environment: bool = True):
        self.reuse_environment = reuse_environment
        self.config = Settings()
        if not self.config.auth_required or not self.config.auth_password_hash.get_secret_value():
            raise ValueError("Configure private login authentication before preparing the rollout")
        if not self.config.azure_endpoint or any(
            not url.startswith("https://") or not key for url, key in self.config.mcp_servers.values()
        ):
            raise ValueError("Configure the real Azure model origin and authenticated cloud MCP endpoints")
        self.secrets = [self.config.auth_password_hash.get_secret_value(),
                        *(key for _, key in self.config.mcp_servers.values())]
        (ROOT / ".build").mkdir(exist_ok=True)

    def run(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, env=env)
        if result.returncode:
            message = result.stderr or result.stdout
            for secret in self.secrets:
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            raise RuntimeError(f"{args[0]} {args[1]} failed: {message[-3000:]}")
        return result.stdout

    def az(self, *args: str) -> str:
        return self.run("az", *args, "--subscription", SUBSCRIPTION)

    def image(self) -> str:
        return json.loads((ROOT / ".build/studio-image.json").read_text())["image"]

    def parameters(self, deploy_app: bool, image: str | None = None) -> dict:
        cfg = self.config
        values = {
            "deployApp": deploy_app, "image": image or PREFIX + ":pending", "appName": APP_NAME,
            "reuseEnvironment": self.reuse_environment,
            "azureEndpoint": cfg.azure_endpoint, "authUsername": cfg.auth_username,
            "passwordHash": cfg.auth_password_hash.get_secret_value(),
            "mcpFaqUrl": cfg.mcp_faq_url, "mcpRepairUrl": cfg.mcp_repair_url,
            "mcpVerificationUrl": cfg.mcp_verification_url,
            "mcpFaqKey": cfg.mcp_faq_key.get_secret_value(),
            "mcpRepairKey": cfg.mcp_repair_key.get_secret_value(),
            "mcpVerificationKey": cfg.mcp_verification_key.get_secret_value(),
        }
        return {"parameters": {key: {"value": value} for key, value in values.items()}}

    def arm(self, action: str, deploy_app: bool, image: str | None = None) -> dict:
        self.run("az", "bicep", "build", "--file", "infra/studio.bicep", "--outfile", ".build/studio.json")
        with tempfile.NamedTemporaryFile(mode="w", dir=ROOT / ".build", suffix=".parameters.json",
                                         delete=False) as file:
            path = Path(file.name)
            json.dump(self.parameters(deploy_app, image), file)
        try:
            args = ["deployment", "sub", action, "--location", "eastus2", "--name", DEPLOYMENT,
                    "--template-file", ".build/studio.json", "--parameters", str(path)]
            if action == "what-if":
                args += ["--result-format", "ResourceIdOnly", "--no-pretty-print",
                         "--query", "{status:status,changes:changes[].{type:changeType,id:resourceId}}"]
            elif action == "create":
                args += ["--query", "{state:properties.provisioningState,outputs:properties.outputs}"]
            else:
                args += ["--query", "{state:properties.provisioningState,error:error}"]
            result = json.loads(self.az(*args, "-o", "json"))
            if result.get("error") or result.get("state") in ("Failed", "Canceled"):
                raise RuntimeError(f"ARM {action} did not succeed")
            if action == "what-if" and result.get("status") != "Succeeded":
                raise RuntimeError("ARM what-if did not succeed")
            if action == "create" and result.get("state") != "Succeeded":
                raise RuntimeError("ARM deployment did not succeed")
            if action == "what-if":
                result["changes"] = [change for change in result.get("changes", []) if change["type"] != "Ignore"]
                if any(change["type"] == "Delete" for change in result["changes"]):
                    raise RuntimeError("Unexpected deletion in studio deployment preview")
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        finally:
            path.unlink(missing_ok=True)

    def build(self) -> None:
        tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ").lower() + "-" + uuid.uuid4().hex[:8]
        image = PREFIX + ":" + tag
        print("Building authenticated linux/amd64 studio image", flush=True)
        self.run("docker", "build", "--platform", "linux/amd64",
                 "--build-arg", "PIP_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/",
                 "-t", image, ".")
        (ROOT / ".build/studio-image.json").write_text(json.dumps({"image": image}, indent=2))
        print(f"Built {image}", flush=True)

    def push(self) -> None:
        image = self.image()
        token = json.loads(self.az("acr", "login", "--name", REGISTRY, "--expose-token",
                                  "-o", "json"))["accessToken"]
        self.secrets.append(token)
        endpoint = self.run("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}").strip()
        directory = Path(tempfile.mkdtemp(prefix="studio-docker-", dir=ROOT / ".build"))
        config = directory / "config.json"
        try:
            with open(config, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
                json.dump({"auths": {SERVER: {"auth": base64.b64encode(
                    ("00000000-0000-0000-0000-000000000000:" + token).encode()
                ).decode()}}}, file)
            self.run("docker", "--config", str(directory), "push", image,
                     env={**os.environ, "DOCKER_HOST": endpoint})
            print(f"Pushed {image} using a temporary scoped credential", flush=True)
        finally:
            config.unlink(missing_ok=True)
            directory.rmdir()

    def url(self) -> str:
        return self.az("containerapp", "show", "-g", GROUP, "-n", APP_NAME,
                       "--query", "properties.configuration.ingress.fqdn", "-o", "tsv").strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--foundation", action="store_true", help="Create environment/identity/RBAC only")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--deploy", action="store_true", help="Publish authenticated app with the built image")
    parser.add_argument("--url", action="store_true")
    parser.add_argument("--create-environment", action="store_true",
                        help="Include environment provisioning instead of referencing the existing project environment")
    args = parser.parse_args()
    if not any((args.validate, args.foundation, args.build, args.push, args.deploy, args.url)):
        parser.error("Select a validation, build or rollout operation")
    rollout = Rollout(reuse_environment=not args.create_environment)
    if args.build:
        rollout.build()
    if args.validate:
        rollout.arm("validate", deploy_app=False)
        rollout.arm("what-if", deploy_app=False)
    if args.foundation:
        rollout.arm("create", deploy_app=False)
    if args.push:
        rollout.push()
    if args.deploy:
        image = rollout.image()
        rollout.arm("validate", deploy_app=True, image=image)
        rollout.arm("what-if", deploy_app=True, image=image)
        rollout.arm("create", deploy_app=True, image=image)
    if args.url:
        print("https://" + rollout.url())
