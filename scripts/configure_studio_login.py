"""Generate a private password hash without password arguments, output or source constants."""

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

from gpt_live_python.login import password_hash


def configure(path: Path, username: str, password: str) -> None:
    if not username.strip() or len(username) > 128 or "\n" in username or "\r" in username:
        raise ValueError("Invalid login username")
    values = {
        "AUTH_REQUIRED": "true", "AUTH_USERNAME": username,
        "AUTH_PASSWORD_HASH": password_hash(password),
    }
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0] not in values]
    lines.extend(f"{key}={value}" for key, value in values.items())
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        pending = Path(file.name)
        try:
            file.write("\n".join(lines) + "\n")
            file.flush()
            os.fsync(file.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    print(f"Login hash configured privately in {path}; password not saved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="admin")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Login password: ")
    configure(args.env_file, args.username, password)
