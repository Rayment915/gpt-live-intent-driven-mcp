"""Run the built local studio, optionally owning three local MCP processes."""

import argparse
import os
import subprocess
import time
from pathlib import Path

PYTHON = str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python")


def main(local_mcp: bool):
    env = dict(os.environ)
    if local_mcp:
        env["STATE_BACKEND"] = "sqlite"
        for service, port in (("faq", 8101), ("repair", 8102), ("verification", 8103)):
            env[f"MCP_{service.upper()}_URL"] = f"http://127.0.0.1:{port}/mcp"
            env[f"MCP_{service.upper()}_KEY"] = ""
    processes = []
    try:
        if local_mcp:
            for service, port in (("faq", 8101), ("repair", 8102), ("verification", 8103)):
                processes.append(subprocess.Popen(
                    [PYTHON, "-m", "gpt_live_python.mcp_server", service, "--port", str(port)], env=env,
                ))
        processes.append(subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "gpt_live_python.web:app", "--host", "127.0.0.1", "--port", "8000"],
            env=env,
        ))
        print("Studio: http://localhost:8000 (build frontend first). Ctrl+C stops owned processes.", flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(0.5)
        failed = next(p.returncode for p in processes if p.poll() is not None)
        if failed:
            raise SystemExit(failed)
    except KeyboardInterrupt:
        print("Stopping studio processes", flush=True)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            process.wait(timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-mcp", action="store_true")
    args = parser.parse_args()
    main(args.local_mcp)
