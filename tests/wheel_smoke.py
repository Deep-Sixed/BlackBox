"""Exercise the distribution outside the source checkout and development venv."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main(wheel):
    wheel = Path(wheel).resolve()
    with tempfile.TemporaryDirectory(prefix="blackbox-wheel-") as directory:
        root = Path(directory)
        environment = root / "venv"
        subprocess.run(
            ["uv", "venv", "--python", "3.14.5", str(environment)], check=True
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(environment / "bin/python"),
                str(wheel),
            ],
            check=True,
        )
        cli = environment / "bin/blackbox"
        subprocess.run([str(cli), "--help"], cwd=root, check=True, capture_output=True)
        command = [str(cli), "--database", str(root / "smoke.sqlite3")]
        subprocess.run([*command, "init"], cwd=root, check=True, capture_output=True)
        result = subprocess.run(
            [*command, "check"], cwd=root, check=True, capture_output=True, text=True
        )
        assert json.loads(result.stdout) == {
            "ok": True,
            "schema_version": 2,
            "errors": [],
        }
    print("Installed wheel: help, init, check passed")


if __name__ == "__main__":
    main(sys.argv[1])
