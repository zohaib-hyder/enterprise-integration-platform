# Publication checklist — approval required

Phase 13A prepares local review material only. A checked local verification item does not authorize any Git or remote mutation. Use [the disclosures](PUBLIC_RELEASE_DISCLOSURES.md) alongside this checklist.

## Local preparation evidence

- [x] Review tracked and untracked candidates, including examples and lockfiles.
- [x] Extend Git and Docker context exclusions for env files, keys, dumps and raw captures.
- [x] Run an actual secret scanner without network credential validation; review fixture/digest findings.
- [x] Keep original data, env files, backups and volumes intact.
- [x] Prepare README, eight Mermaid sources, case study, demo and portfolio copy.
- [x] Final Phase 13A checks, 13 screenshots and sanitized evidence reviewed: see [report](PHASE_13A_REPORT.md).

## Phase 13B authorization

The owner explicitly authorized MIT, public visibility, staging/initial commit, repository creation, push and remote Actions in Phase 13B. The owner confirmed copyright (c) 2026 Zohaib Hyder and `zohaib-hyder` as the intended account. The root MIT LICENSE is present. Authorization is already given and does not need to be repeated. Actual Git/remote verification is recorded in the Phase 13B report.

## Remaining publication gates

- [x] Owner selected MIT; approved holder/year and root LICENSE are present.
- [x] Owner confirmed `zohaib-hyder/enterprise-integration-platform`, public visibility.
- [x] Owner explicitly authorized repository creation.
- [ ] Review `git status --short`, `git ls-files`, `git ls-files --others --exclude-standard` and `git diff --cached --stat` after separately authorized staging.
- [ ] Inspect every staged file. No private env, key, token, browser state, dump, trace, raw scanner output, personal paths or customer information.
- [ ] Re-run secret scanning on the exact staged tree and inspect any existing Git history before pushing. If a credential is found, stop and rotate through an authorized private process.
- [ ] Confirm screenshots are real, synthetic, current and free of one-time secrets or private email addresses; check all local links and diagram rendering.
- [ ] Retain visible mock-provider, external-integration, local-CI and exactly-once limitations.
- [ ] Refresh dependency/image scans and record unresolved findings. Production security gates remain blocked until remediated and independently rechecked.
- [ ] Review relevant local checks and their dates. Run affected tests if application source changes; do not relabel historical results as new runs.
- [x] Owner explicitly authorized audited staging and the initial portfolio commit.
- [x] Owner explicitly authorized remote configuration and publication; visibility must still be checked before push.
- [x] Owner explicitly authorized GitHub Actions execution; actual remote results still require verification.
- [ ] Review rendered GitHub README, links, diagrams and image accessibility after authorized publication; record actual remote URL and run links.
- [ ] Owner separately approves LinkedIn/Upwork posting and exact text; preparation of copy does not authorize posting.

Deployment, image publication, releases/tags, real provider accounts, OAuth, paid AI calls and real messages/calendar invitations each remain outside this approval. No production-ready badge or claim is permitted while release gates remain open.
