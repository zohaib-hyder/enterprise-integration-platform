# LinkedIn project copy — prepared, not posted

## Title

Enterprise Integration & AI Workflow Platform

## Description (under 2,000 characters)

Built a working full-stack portfolio platform for a fictional North American staffing company with disconnected CRM, ATS, calendar, email and Slack systems.

Implemented tenant-aware authentication/RBAC, HMAC webhook ingestion, canonical data mapping, PostgreSQL-backed durable workflows, fenced worker leases, retries/dead letters, CRM-to-ATS synchronization with conflict review, and a transactional notification outbox. An operations console connects incidents, timelines and governed recovery. Evidence-grounded AI assistance uses strict citations and human review; acceptance produces a recovery preview, never autonomous execution.

Stack: Python, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Next.js, TypeScript, Tailwind CSS, Docker Compose, pytest and Playwright.

Recorded local verification: 653 backend tests, 157 frontend tests, 28 browser checks and 60 deterministic mock evaluation cases passing, plus lint/type/build checks. Separate local rehearsals verified partial-failure recovery, restarts and backup/restore across 82 application tables.

This is synthetic portfolio work, not a customer deployment. All demonstrated provider effects and AI responses are simulated. Real OAuth/provider delivery, remote CI and production operation are unverified. Upstream security findings remain disclosed and block production release; no exactly-once remote-delivery guarantee is claimed.

## Five skills

Python · FastAPI · PostgreSQL · Next.js · Systems Integration

## Architecture summary

Next.js sends allowlisted same-origin requests to FastAPI, which owns tenant authorization and application contracts. PostgreSQL persists records, durable workflow queues and the outbox; one Python worker processes fenced steps and independent companion actions. AI reads bounded authorized evidence and can only propose a human-reviewed recovery preview.

## Architecture / evidence placement

Approved project link: [GitHub repository](https://github.com/zohaib-hyder/enterprise-integration-platform), now publicly published under MIT. This draft has not been posted to LinkedIn. The README leads with a real dashboard screenshot and links to eight Mermaid diagrams, a 12-step demo, the case study and dated verification evidence. Add the operations and human-review screenshots as project media, with “synthetic local portfolio demo; mock AI” captions. Keep security and external-provider limitations visible in linked disclosures. Posting requires separate explicit approval.
