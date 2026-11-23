"""Build three self-contained source deployment packages from one Python package."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", ".build/wheels"], cwd=ROOT, check=True)
    wheels = list((ROOT / ".build/wheels").glob("gpt_live_python-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one application wheel in .build/wheels")
    # Export only runtime dependencies; the local wheel supplies application code, never a macOS venv.
    requirements = [
        "mcp==1.30.0", "uvicorn==0.54.0", "azure-identity==1.27.0",
        "azure-data-tables==12.7.0", "pydantic-settings==2.16.2",
    ]
    for service in ("faq", "repair", "verification"):
        dest = ROOT / ".build/functions" / service
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wheels[0], dest / wheels[0].name)
        shutil.copy2(ROOT / "requirements.lock", dest / "constraints.txt")
        (dest / "requirements.txt").write_text(
            "\n".join(["-c constraints.txt", *requirements, f"./{wheels[0].name}"]) + "\n"
        )
        (dest / "server.py").write_text(
            f"import sys\nfrom gpt_live_python.mcp_server import main\n"
            f"sys.argv = ['server', '{service}', '--port', '8000', '--host', '0.0.0.0']\nmain()\n"
        )
        (dest / "host.json").write_text(json.dumps({
            "version": "2.0", "configurationProfile": "mcp-custom-handler",
            "customHandler": {
                "description": {"defaultExecutablePath": "python", "arguments": ["server.py"]},
                "http": {"defaultAuthorizationLevel": "function"},
                "port": "8000",
            },
        }, indent=2))
        (dest / ".funcignore").write_text(".venv\nlocal.settings.json\n__pycache__\n")
        print(f"Packaged {service}: {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
