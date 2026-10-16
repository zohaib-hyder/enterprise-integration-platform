# Backup and isolated restore

The application uses PostgreSQL's `public` schema. Phase verification retains additional independent schemas; the release backup deliberately includes **all 82 public application tables** and excludes those transient verification schemas. It is an application-schema backup, not a cluster/global-role backup. For deployments adding other application schemas, extend and verify backup scope before release.

From repository root with the original development PostgreSQL healthy:

```sh
backend/.venv/bin/python scripts/database_recovery.py backup
```

This runs read-only `pg_dump --schema=public --format=custom --no-owner --no-acl` inside the existing PostgreSQL container. It compares public table counts and deterministic content fingerprints before/after and writes an exclusive mode-600 dump plus manifest under ignored `.local/backups`. Dumps contain confidential application material, encrypted credentials, password/session hashes and audit data; never publish or attach them. The manifest also stays private. There is no automatic deletion or retention cleanup.

After preparing the separate release Compose database:

```sh
backend/.venv/bin/python scripts/database_recovery.py restore \
  --release-env .local/release/release.env \
  --manifest .local/backups/<printed-manifest-name>.json
```

The script verifies the dump SHA-256, creates a fresh unpredictable `phase12_restore_*` database inside the **enterprise-integration-release** PostgreSQL service and restores there. The caller cannot supply the destination database name. `pg_restore --clean --if-exists` handles the destination's default public schema only inside that newly created database. The original volume/database is never a restore destination. Partial/failed destinations are retained for diagnosis, not silently reused or deleted. Counts and fingerprints for every backed-up table must match exactly. Safe evidence contains only counts, booleans and generated destination identity in `evidence/phase12-backup-restore.json`.

The executed rehearsal restored 82 tables with matching counts/fingerprints. Representative application ORM reads are checked separately in the release evidence. Encryption keys are **not** embedded in the SQL dump: preserving matching secret-manager key material is necessary to decrypt restored credentials. New rehearsal keys cannot decrypt original ciphertext; this run does not claim otherwise. Database roles/ownership/ACLs and external backup encryption/key custody require separate deployment procedures.

Production policy: encrypted off-host backups, restricted access, defined RPO/RTO, rotation and retention, restore drills after schema/key changes and monitoring of failed backups. This local pass establishes neither an RPO/RTO nor off-host disaster recovery. Prefer a consistent PostgreSQL snapshot when concurrent writes are expected; this local original application was quiescent. The script intentionally fails if its before/after integrity snapshot changes.
