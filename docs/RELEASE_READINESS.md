# Release readiness register

Phase 12 implements local preparation; it does **not** authorize publication/deployment. Status vocabulary: READY = implemented with listed local evidence; NEEDS_REMEDIATION = identified work remains; BLOCKED = release gate prevents an unconditional release recommendation; NOT_VERIFIED = not executed in this environment. Final executed counts are in PHASE_12_REPORT.md and evidence/phase12-verification.json.

| Area | Status | Evidence / limitation |
|---|---|---|
| Existing data preservation | READY | Original volume untouched; original public count/fingerprint comparisons; private backup |
| Migration chain | READY | 13 files, one e8e0abf289b1 head; no model drift; isolated clean upgrade |
| Production configuration | READY | Missing/invalid keys, debug, CSRF secret, DB, hosts and HTTPS origins fail closed; regression matrix |
| Fresh setup / locks | READY | Fresh backend lock install/import/check; npm ci; exclusive private independent secret generation |
| Dependency remediation investigation | NEEDS_REMEDIATION | Next/ESLint patched together to 16.4.1; braces 3.0.3 remains latest, five high dev packages |
| Development dependency release gate | BLOCKED | GHSA-vfj7-8cjw-p6xm; no supported patched braces chain. Do not suppress or force downgrade |
| Python / production npm audit | READY | Actual audit reports, not extrapolated image safety |
| Application container builds | READY | Multi-stage, exact locks/digests, non-root, no secrets in contexts, health checks; final build results in report |
| Container CVEs | BLOCKED | Actual scans reveal critical/high findings; final image counts and identities in report/evidence |
| Release Compose | READY | Separate project/networks/volumes, localhost-only access, caps/limits/health/restart; original Compose unchanged |
| Backup / isolated restore | READY | 82 public tables; SHA-256 and all table counts/fingerprints match; representative ORM read |
| Local release rehearsal | READY | Final images: 13 HTTP demo checks + six restart/outage/health checks passed; four services healthy before graceful stop |
| Frontend / browser | READY | 157 unit tests; lint/type/build; final 28 browser cases and zero overview axe violations |
| Full backend regression | READY | 653 passed, 2061.80 seconds, zero failures/skips; one dependency deprecation warning |
| CI configuration | READY | Three jobs, caches/limits/permissions/artifacts and YAML validation |
| Remote GitHub Actions | NOT_VERIFIED | Explicitly unauthorized in this phase |
| TLS / proxy / DB roles / key custody | NEEDS_REMEDIATION | Production examples and runbooks prepared; local HTTP rehearsal uses test mode |
| Production capacity / SLA | NOT_VERIFIED | Only short local Phase 11 samples; empty polls are not productive throughput |
| Real providers / paid AI | NOT_VERIFIED | Intentionally disabled; all verification uses synthetic/mock contracts |
| Publication / deployment | BLOCKED | Requires separate explicit Phase 13 authorization; no commit/push/remote creation |

## Development advisory investigation

Exact final path: eslint-config-next@16.4.1 → @next/eslint-plugin-next@16.4.1 → fast-glob@3.3.1 → micromatch@4.0.8 → braces@3.0.3. Both direct and compatible patch alternatives were inspected. npm registry still lists braces 3.0.3; the advisory has no patched release. Next canary was inspected for an upstream supported removal; an unstable framework migration or incompatible glob API replacement is not a safe release fix.

The Next rule helper uses globSync for explicitly configured `settings.next.rootDir`; this repository does not accept user-supplied lint globs. The exposure is local lint/build tooling processing trusted repository configuration, not the deployed standalone frontend request path. This narrows exposure but does not erase the advisory. Full audit remains visible in CI and release stays conditional. No custom fork, package rename, audit exception or incompatible force-fix was used.

Primary sources: [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm), [maintainer patched-release issue](https://github.com/micromatch/braces/issues/84), [npm override contracts](https://docs.npmjs.com/cli/configuring-npm/package-json/). Revisit the exact lock chain after a supported upstream release; rerun install/tests/lint/type/build/browser/audit. Container CVEs, upstream remediation and deployment prerequisites are separate gates.
