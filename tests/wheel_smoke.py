"""Exercise the distribution outside the source checkout and development venv."""

import json
import os
import shutil
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
        consumer = root / "consumer.py"
        shutil.copyfile(Path(__file__).with_name("public_consumer.py"), consumer)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "VIRTUAL_ENV")
        }
        subprocess.run(
            [
                str(environment / "bin/python"),
                "-I",
                str(consumer),
                str(root / "consumer.sqlite3"),
            ],
            cwd=root,
            env=env,
            check=True,
        )
        cli = environment / "bin/blackbox"
        subprocess.run([str(cli), "--help"], cwd=root, check=True, capture_output=True)
        command = [str(cli), "--database", str(root / "smoke.sqlite3")]
        subprocess.run([*command, "init"], cwd=root, check=True, capture_output=True)

        def run(*args):
            result = subprocess.run(
                [*command, *args], cwd=root, check=True, capture_output=True, text=True
            )
            return json.loads(result.stdout)

        claim = {"source": "caller", "topic": "smoke", "statement": "Original"}
        sessions = []
        for request_id, claims in (("smoke-original", [claim]), ("smoke-review", [])):
            request = root / f"{request_id}.json"
            request.write_text(
                json.dumps(
                    {"request_id": request_id, "producer": "smoke", "claims": claims}
                )
            )
            sessions.append(run("capture", "--input", str(request))["session_id"])
        (original,) = run("claims", "--topic", "smoke")
        assert original["session_id"] == sessions[0]
        retraction = root / "retraction.json"
        retraction.write_text(
            json.dumps({"source": "reviewer", "topic": "smoke", "statement": "Wrong"})
        )
        retracted = run(
            "claim",
            sessions[1],
            "--input",
            str(retraction),
            "--target",
            original["id"],
            "--relation",
            "retracts",
        )["claim_id"]
        statuses = {row["id"]: row["status"] for row in run("claims")}
        assert statuses == {original["id"]: "retracted", retracted: "active"}
        intact = {
            "ok": True,
            "schema_version": 3,
            "errors": [],
            "first_broken_sequence": None,
        }
        assert run("check") == intact
        anchor = root / "anchor.json"
        anchor.write_text(json.dumps(run("head")))
        assert run("check", "--anchor", str(anchor)) == intact
    print(
        "Installed wheel CLI: help, init, capture, cross-session retraction, "
        "claims, check, head, anchored check passed"
    )


if __name__ == "__main__":
    main(sys.argv[1])
