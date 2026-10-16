# Rollback and safe shutdown

No production deployment occurred. This runbook is prepared for a separately authorized release.

1. Stop new input and operator mutations; preserve logs, correlation IDs, incident timelines and unknown-outcome evidence. Do not repeat uncertain external writes.
2. Gracefully stop worker admission first, then API/frontend. Wait for bounded in-flight work. A SIGKILL leaves a lease that expires; fencing rejects stale completion. Health and operations worker status identify missing/stopped heartbeats.
3. Record current image digests, Alembic head and backup manifest. Restore the previous reviewed API/worker/frontend image set only when its model contracts are compatible with the current schema. No automatic downgrade or destructive migration is performed.
4. For schema incompatibility, restore a verified backup into a **new** isolated database, retain the failed database read-only for diagnosis and compare counts/fingerprints plus representative application reads. Recover matching encryption/CSRF key material from secure custody; dumps alone cannot decrypt secrets. Reconcile any writes after the backup before switching traffic.
5. Start the API with correct explicit secrets/hosts/origins and verify migrations/readiness, authentication, tenant scope and a read-only operations view. Start workers only after review of pending/unknown action receipts. Validate a synthetic transaction before enabling input.
6. Audit the human-approved switch, monitor retries/incidents and retain rollback evidence according to policy. Do not erase the failed volume or evidence.

Local rehearsal shutdown preserves every volume:

```sh
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml stop worker
docker compose --env-file .local/release/release.env -p enterprise-integration-release -f compose.release.yaml stop frontend api postgres
```

Restart the same release project with the same private env and `up -d --wait`. Never run `down -v`, volume rm/prune, database reset, or restore over the original database. A fresh demo uses `setup_release.py --directory .local/<new-name>` plus a separately reviewed project/port configuration; default demo reruns append uniquely named synthetic records and do not reset data. The guarded backup restore always creates a new destination database. Live production traffic switching, off-host restore and RPO/RTO were not rehearsed here.
