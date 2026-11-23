"""Import approved deployment outputs and host keys into private .env, never stdout."""

import json
import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values

SUBSCRIPTION = "a165edb0-c8fb-4569-9e3e-40495d678b10"
GROUP = "rg-gpt-live-python-dev"


def az(*args):
    completed = subprocess.run(
        ["az", *args, "--subscription", SUBSCRIPTION, "--output", "json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def main() -> None:
    outputs = {key.upper(): value for key, value in az(
        "deployment", "sub", "show", "--name", "gpt-live-python-dev", "--query", "properties.outputs",
    ).items()}
    values = {key: value for key, value in dotenv_values(".env").items() if value is not None}
    for key in ("TABLE_ENDPOINT", "TABLE_NAME", "MCP_FAQ_URL", "MCP_REPAIR_URL", "MCP_VERIFICATION_URL"):
        values[key] = outputs[key]["value"]
    values["STATE_BACKEND"] = "table"
    values["AZURE_SUBSCRIPTION_ID"] = SUBSCRIPTION
    for service in ("faq", "repair", "verification"):
        name = outputs[f"SERVICE_{service.upper()}_NAME"]["value"]
        keys = az("functionapp", "keys", "list", "--name", name, "--resource-group", GROUP)
        key = keys.get("functionKeys", {}).get("default")
        if not isinstance(key, str) or not key:
            raise RuntimeError(f"No non-master host key available for {service}; verify deployed host readiness")
        values[f"MCP_{service.upper()}_KEY"] = key
    path = Path(".env")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")
    path.chmod(0o600)
    print("Configured three remote MCP endpoints and private host keys in .env (0600); no keys printed.")


if __name__ == "__main__":
    main()
