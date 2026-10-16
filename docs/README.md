# Documentation index

Start with the [project README](../README.md), [case study](PORTFOLIO_CASE_STUDY.md), [demo](DEMO_WALKTHROUGH.md) and [public disclosures](PUBLIC_RELEASE_DISCLOSURES.md). Documents from earlier phases describe their historical scope; the README, implementation plan and latest reports describe the cumulative system.

## Portfolio and publication

- [Portfolio Case Study](PORTFOLIO_CASE_STUDY.md)
- [Demo Walkthrough](DEMO_WALKTHROUGH.md)
- [Architecture Diagrams](ARCHITECTURE_DIAGRAMS.md)
- [Linkedin Project](LINKEDIN_PROJECT.md)
- [Upwork Portfolio](UPWORK_PORTFOLIO.md)
- [Public Release Disclosures](PUBLIC_RELEASE_DISCLOSURES.md)
- [Publication Checklist](PUBLICATION_CHECKLIST.md)
- [License Status](LICENSE_STATUS.md)
- [Phase 13A Report](PHASE_13A_REPORT.md)
- [Phase 13B Report](PHASE_13B_REPORT.md)

## Setup, architecture and scope

- [Environment Setup](ENVIRONMENT_SETUP.md)
- [Architecture](ARCHITECTURE.md)
- [Decisions](DECISIONS.md)
- [Implementation Plan](IMPLEMENTATION_PLAN.md)
- [Portfolio Scope](PORTFOLIO_SCOPE.md)
- [Deployment Guide](DEPLOYMENT_GUIDE.md)

## Integration contracts and data

- [Connector Contracts](CONNECTOR_CONTRACTS.md)
- [Integration Catalog](INTEGRATION_CATALOG.md)
- [Webhook Security](WEBHOOK_SECURITY.md)
- [Event Inbox](EVENT_INBOX.md)
- [Canonical Data Model](CANONICAL_DATA_MODEL.md)
- [Transformation Engine](TRANSFORMATION_ENGINE.md)
- [Identity Mapping](IDENTITY_MAPPING.md)
- [Field Authority](FIELD_AUTHORITY.md)
- [Crm Ats Synchronization](CRM_ATS_SYNCHRONIZATION.md)
- [Sync Conflict Resolution](SYNC_CONFLICT_RESOLUTION.md)
- [Reconciliation](RECONCILIATION.md)

## Execution, delivery and recovery

- [Workflow Engine](WORKFLOW_ENGINE.md)
- [Worker Recovery](WORKER_RECOVERY.md)
- [Retry And Replay](RETRY_AND_REPLAY.md)
- [Connector Reliability](CONNECTOR_RELIABILITY.md)
- [Notification Delivery](NOTIFICATION_DELIVERY.md)
- [Calendar Coordination](CALENDAR_COORDINATION.md)
- [Partial Failure Recovery](PARTIAL_FAILURE_RECOVERY.md)
- [Operations Command Center](OPERATIONS_COMMAND_CENTER.md)
- [Operations Observability](OPERATIONS_OBSERVABILITY.md)
- [Incident Management](INCIDENT_MANAGEMENT.md)
- [Controlled Recovery](CONTROLLED_RECOVERY.md)
- [Operational Reconciliation](OPERATIONAL_RECONCILIATION.md)
- [Runbooks](RUNBOOKS.md)

## AI and human review

- [Ai Assistance](AI_ASSISTANCE.md)
- [Ai Evidence Grounding](AI_EVIDENCE_GROUNDING.md)
- [Ai Recommendation Safety](AI_RECOMMENDATION_SAFETY.md)
- [Ai Security](AI_SECURITY.md)
- [Ai Evaluation](AI_EVALUATION.md)

## Security, testing and release

- [Threat Model](THREAT_MODEL.md)
- [Credential Lifecycle](CREDENTIAL_LIFECYCLE.md)
- [Security Audit](SECURITY_AUDIT.md)
- [Production Security Checklist](PRODUCTION_SECURITY_CHECKLIST.md)
- [Verification](VERIFICATION.md)
- [Browser E2E](BROWSER_E2E.md)
- [Reliability Testing](RELIABILITY_TESTING.md)
- [Performance Baselines](PERFORMANCE_BASELINES.md)
- [Ci Verification](CI_VERIFICATION.md)
- [Release Readiness](RELEASE_READINESS.md)
- [Release Rehearsal](RELEASE_REHEARSAL.md)
- [Backup Restore](BACKUP_RESTORE.md)
- [Rollback Runbook](ROLLBACK_RUNBOOK.md)

## Phase reports

- [Phase 7 Report](PHASE_7_REPORT.md)
- [Phase 8 Report](PHASE_8_REPORT.md)
- [Phase 9 Report](PHASE_9_REPORT.md)
- [Phase 10 Report](PHASE_10_REPORT.md)
- [Phase 11 Report](PHASE_11_REPORT.md)
- [Phase 12 Report](PHASE_12_REPORT.md)


## Evidence and media

- [Real screenshot gallery and manifest](assets/screenshots/README.md)
- [Phase 12 verification](evidence/phase12-verification.json)
- [Container scan findings](evidence/phase12-container-scans.json)
- [Phase 13A demo checks](evidence/phase13a-demo.json)
- [Phase 13A diagram render checks](evidence/phase13a-diagrams.json)
- [Phase 13A publication audit](evidence/phase13a-publication-audit.json)

## Repository navigation

`backend/app` holds API, configuration, models, typed schemas, application services, connectors, workflows and the worker. `backend/alembic` owns migrations; `backend/tests` owns pytest checks. `frontend/app`, `components` and `lib` own the App Router workspace, UI and API contracts. `frontend/tests` and `e2e` contain unit/browser coverage. `scripts` contains local setup and verification helpers; `.github/workflows` contains unexecuted remote CI configuration.

Supported setup entry points are `scripts/setup_local.py`, `setup_release.py` and the environment guide. `scaffold_phase0.py` is a historical one-time generator with overwrite refusal; do not run it on an existing checkout. Phase-specific verification helpers preserve earlier reproduction paths. No broad code relocation or deletion was performed for publication.
