"""Read-only candidate/privacy audit; writes sanitized counts, never private values."""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE = re.compile(r"PASSWORD|SECRET|ENCRYPTION_KEY|TOKEN|COOKIE", re.IGNORECASE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/evidence/phase13a-publication-audit.json",
    )
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT):
        parser.error("Evidence must stay inside the repository")

    def git(*args: str) -> list[str]:
        data = subprocess.check_output(["git", *args], cwd=ROOT)
        return [s for s in data.decode().split("\0") if s]

    tracked = git("ls-files", "-z")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    candidates = sorted(set(tracked + untracked))
    private_values = set()
    env_files = [ROOT / ".env", ROOT / "backend/.env", ROOT / "frontend/.env.local"]
    env_files += list((ROOT / ".local").glob("**/*.env"))
    for path in env_files:
        if path.is_file():
            for key, value in dotenv_values(path).items():
                if value and SENSITIVE.search(key) and len(value) >= 16:
                    private_values.add(value.encode())
    fixture_files = list((ROOT / "backend/.local").glob("*verification*.json"))
    fixture_files += list((ROOT / "backend/.local").glob("phase*-fixtures.json"))

    def collect(value, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, sensitive or bool(SENSITIVE.search(key)))
        elif isinstance(value, list):
            for child in value:
                collect(child, sensitive)
        elif sensitive and isinstance(value, str) and len(value) >= 16:
            private_values.add(value.encode())

    for path in fixture_files:
        collect(json.loads(path.read_text()))
    findings = {
        "configured_secret_matches": [],
        "personal_path_matches": [],
        "sensitive_filename_matches": [],
        "private_key_markers": [],
        "large_candidates": [],
    }
    sizes = []
    for name in candidates:
        path = ROOT / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        sizes.append((len(data), name))
        if any(value in data for value in private_values):
            findings["configured_secret_matches"].append(name)
        if re.search(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/", data, re.IGNORECASE):
            findings["personal_path_matches"].append(name)
        if (
            "env" in path.name and path.name in {".env", ".env.local", "release.env"}
        ) or path.suffix in {
            ".pem",
            ".key",
            ".dump",
            ".pgdump",
            ".p12",
            ".pfx",
            ".har",
            ".sarif",
            ".zip",
            ".sqlite",
            ".sqlite3",
        }:
            findings["sensitive_filename_matches"].append(name)
        if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", data):
            findings["private_key_markers"].append(name)
        if len(data) > 2_000_000:
            findings["large_candidates"].append({"file": name, "bytes": len(data)})
    ignored = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    classes = {}
    for name in ignored:
        group = (
            "dependencies/caches"
            if any(
                p in name
                for p in [
                    "node_modules/",
                    ".venv/",
                    ".uv-cache/",
                    ".npm-cache/",
                    ".playwright/",
                    ".next/",
                ]
            )
            else "private fixtures/backups/raw evidence"
            if ".local/" in name
            else "private env"
            if Path(name).name.startswith(".env")
            else "generated/local artifacts"
        )
        classes[group] = classes.get(group, 0) + 1
    report = {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Current tracked and all untracked non-ignored candidates, including examples; no Git history exists at HEAD",
        "tracked_files": len(tracked),
        "untracked_candidate_files": len(untracked),
        "candidate_files": len(candidates),
        "candidate_bytes": sum(n for n, _ in sizes),
        "ignored_file_classes": classes,
        "private_env_files_compared": len([p for p in env_files if p.is_file()]),
        "private_fixture_files_compared": len(fixture_files),
        "configured_private_values_compared": len(private_values),
        "findings": findings,
        "largest_candidates": [
            {"file": name, "bytes": size}
            for size, name in sorted(sizes, reverse=True)[:8]
        ],
        "limits": "Secret patterns and configured-value comparison cannot certify absence of all unknown secrets or pixel content; separate detector and visual review required. No staging, commit, remote or publication performed.",
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Audited {len(candidates)} candidates; configured secret matches: {len(findings['configured_secret_matches'])}; personal path matches: {len(findings['personal_path_matches'])}."
    )
    if any(findings.values()):
        raise SystemExit("Review sanitized findings before publication")


if __name__ == "__main__":
    main()
