"""Read-only application-schema backup and guarded restore into a new database.

Private dumps may contain encrypted credentials and session hashes. Never publish them.
"""

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose(release_env: Path | None = None) -> list[str]:
    if release_env:
        if not release_env.resolve().is_relative_to(ROOT / ".local"):
            raise ValueError("Private release env must be under repository .local")
        return [
            "docker",
            "compose",
            "--env-file",
            str(release_env),
            "-p",
            "enterprise-integration-release",
            "-f",
            str(ROOT / "compose.release.yaml"),
        ]
    return ["docker", "compose", "-f", str(ROOT / "compose.yaml")]


def snapshot(command: list[str], database: str | None = None) -> dict:
    # pg_dump's public schema is the application; retained test schemas are excluded.
    sql = """SELECT json_build_object('table',tablename,'count',
      (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) n FROM public.%I',tablename),false,true,'')))[1]::text,
      'fingerprint',(xpath('/row/h/text()',query_to_xml(format(
      $query$SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'')) h FROM public.%I t$query$,tablename),false,true,'')))[1]::text)
      FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"""
    target = database or '"$POSTGRES_DB"'
    result = subprocess.run(
        command
        + [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            f'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d {target}',
        ],
        input=sql.encode(),
        capture_output=True,
        check=True,
    )
    return {
        row["table"]: {"count": int(row["count"]), "fingerprint": row["fingerprint"]}
        for row in map(json.loads, result.stdout.decode().splitlines())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--directory", default=".local/backups")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-env", type=Path)
    args = parser.parse_args()
    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to(ROOT / ".local"):
        parser.error("Backups must remain under repository .local")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "backup":
        command = compose()
        before = snapshot(command)
        prefix = directory / ("application_" + uuid.uuid4().hex[:12])
        dump = prefix.with_suffix(".dump")
        with os.fdopen(
            os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as out:
            subprocess.run(
                command
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "sh",
                    "-c",
                    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=public --format=custom --no-owner --no-acl',
                ],
                stdout=out,
                stderr=subprocess.PIPE,
                check=True,
            )
        assert snapshot(command) == before, (
            "Original public records changed during backup"
        )
        payload = {
            "scope": "public application schema, all tables; excludes retained verification schemas",
            "dump": str(dump),
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "tables": before,
        }
        manifest = prefix.with_suffix(".json")
        with os.fdopen(
            os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as out:
            json.dump(payload, out, indent=2)
        print(
            f"Backup complete; {len(before)} public tables. Private manifest: {manifest.relative_to(ROOT)}"
        )
    else:
        if not args.release_env or not args.manifest:
            parser.error("Restore requires --release-env and --manifest")
        manifest = args.manifest.resolve()
        if not manifest.is_relative_to(ROOT / ".local"):
            parser.error("Manifest must be under repository .local")
        payload = json.loads(manifest.read_text())
        dump = Path(payload["dump"]).resolve()
        assert dump.is_relative_to(ROOT / ".local")
        assert hashlib.sha256(dump.read_bytes()).hexdigest() == payload["sha256"]
        command = compose(args.release_env)
        # No caller-controlled restore destination; never overwrite an existing database.
        database = "phase12_restore_" + uuid.uuid4().hex[:12]
        subprocess.run(
            command
            + [
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                f'exec createdb -U "$POSTGRES_USER" {database}',
            ],
            check=True,
            capture_output=True,
        )
        with dump.open("rb") as source:
            subprocess.run(
                command
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "sh",
                    "-c",
                    f'exec pg_restore --exit-on-error --clean --if-exists --no-owner --no-acl -U "$POSTGRES_USER" -d {database}',
                ],
                stdin=source,
                capture_output=True,
                check=True,
            )
        restored = snapshot(command, database)
        assert restored == payload["tables"], "Restored counts/fingerprints differ"
        result = {
            "database": database,
            "tables_verified": len(restored),
            "counts_and_fingerprints_match": True,
            "dump_sha256_match": True,
            "retained": True,
        }
        evidence = ROOT / "docs/evidence/phase12-backup-restore.json"
        evidence.write_text(json.dumps(result, indent=2) + "\n")
        print(
            f"Isolated restore verified: {len(restored)} tables; original database untouched."
        )


if __name__ == "__main__":
    main()
