"""Scan tracked content and reachable Git blobs without disclosing secret values."""

import subprocess
import tempfile
from pathlib import Path

from detect_secrets.core.scan import scan_file
from detect_secrets.settings import default_settings


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    findings = 0
    scanned = 0
    with default_settings() as settings:
        # Never send suspected credentials to external verification endpoints.
        settings.disable_filters(
            "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
        )

        def check(label, path):
            nonlocal findings, scanned
            scanned += 1
            for secret in scan_file(str(path)):
                print(f"Potential secret: {label}:{secret.line_number} ({secret.type})")
                findings += 1

        for filename in git("ls-files", "-z").decode().split("\0"):
            if filename and Path(filename).is_file():
                check(filename, Path(filename))

        objects = git("rev-list", "--objects", "--all").splitlines()
        with tempfile.TemporaryDirectory(prefix="ios-control-secret-scan-") as folder:
            for entry in objects:
                parts = entry.split(b" ", 1)
                object_id = parts[0].decode("ascii")
                if git("cat-file", "-t", object_id).strip() == b"blob":
                    suffix = Path(parts[1].decode()).suffix if len(parts) == 2 else ".txt"
                    path = Path(folder) / (object_id + suffix)
                    path.write_bytes(git("cat-file", "blob", object_id))
                    check(f"git-blob:{object_id[:12]}", path)
    print(f"Scanned {scanned} files/blobs; {findings} potential secrets.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
