# Upwork portfolio copy — prepared, not posted

**Title:** Staffing Integration Platform: Durable Workflows & Governed AI

**Role:** Full-stack integration engineer (independent portfolio implementation)

**Project description:** A fictional staffing company needs consistent requisitions across sales CRM and recruiting ATS, reliable team notifications, interview coordination and a clear recovery path when one action fails. I built a runnable platform that demonstrates those boundaries using local synthetic providers and reserved test data.

**Delivered:** FastAPI and Next.js workspace; tenant authentication/RBAC; signed event inbox; versioned canonical mappings; PostgreSQL workflow queue with fenced leases, retries and dead letters; ownership-aware ATS synchronization and conflict review; transactional Slack/email outbox; durable synthetic calendar actions; correlated incidents, audit history and approved recovery. AI explanations cite authorized evidence and require human review before an existing recovery preview is available.

**Evidence:** Dated local results include 653 backend tests, 157 frontend tests, 28 browser checks and 60 deterministic mock evaluation cases, plus builds/lint/types. A 13-check container demo exercises a signed event through synchronization, injected email failure, targeted retry and human-reviewed mock guidance. Backup/restore verification covers 82 public application tables. These are engineering verification results, not customer business outcomes.

**Technologies:** Python, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, Next.js App Router, TypeScript, Tailwind CSS, Docker Compose, pytest, Vitest and Playwright.

**Skills / categories:** Python, FastAPI, PostgreSQL, TypeScript, Next.js; API integration, workflow automation, full-stack development, data integration, reliability engineering.

**Suggested media:** Dashboard → webhook-to-ATS diagram → synchronization/conflict review → notification delivery → incident timeline → human review. Use the [actual screenshot gallery](assets/screenshots/README.md) and [architecture diagrams](ARCHITECTURE_DIAGRAMS.md). Add the actual approved repository link after publication; none has been created for this task.

**Disclosure:** Independent synthetic portfolio project; no client testimonial, real customer data or claimed ROI. CRM/ATS/Slack/email/calendar and AI are simulated in the demo. External OAuth/delivery, production scale and remote CI are unverified. Upstream security findings block production release. No autonomous AI remediation or exactly-once remote-delivery guarantee. See [full disclosures](PUBLIC_RELEASE_DISCLOSURES.md). Publishing this copy needs separate approval.
