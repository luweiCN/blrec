# Primary List Layout and Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the four primary list layouts with the clip library baseline, restore clip-library scrolling, and stop realtime upload events from visibly reloading the whole list repeatedly.

**Architecture:** Keep each page's existing table and business logic. Remove only the primary-list max-width wrappers, align their outer padding with the clip library, and make each route own its vertical scrolling. Extend recording-list requests with a loading-visibility flag so automatic convergence stays in the ready state, and react only to relevant realtime collection changes.

**Tech Stack:** Angular 12, TypeScript, SCSS, RxJS, Jasmine/Karma, ng-zorro-antd.

## Global Constraints

- Scope is limited to room management, recording tasks, upload tasks, and clip management.
- Do not change APIs, filters, pagination rules, list fields, or business actions.
- Desktop content padding is `0 24px 24px`; narrow-screen content padding is `0 12px 12px`.
- Realtime scalar progress remains in-place; background reconciliation must not clear visible rows.

---

### Task 1: Prevent visible and irrelevant realtime list reloads

**Files:**
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`

**Interfaces:**
- Consumes: `RealtimeService.events$` upload-progress snapshots and `RecordingSessionService.listSessions(...)`.
- Produces: `load(showLoading?: boolean): void`, where automatic refreshes pass `false` and user-initiated refreshes keep the default `true`.

- [ ] **Step 1: Add failing regression tests**

Add assertions that a state-changing realtime event may request reconciliation without changing `view.state` from `ready`, and that removal of a job outside the current page does not call `listSessions`.

- [ ] **Step 2: Verify the tests fail for the reported behavior**

Run:

```bash
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'
```

Expected: the ready-state assertion fails because the current request enters `loading`, and the off-page removal assertion fails because it triggers a list request.

- [ ] **Step 3: Implement the minimal realtime fix**

Carry `showLoading` in `RecordingListRequest`; only set `view = { state: 'loading' }` for visible loads. Use hidden loads for SSE resync, action completion, and realtime reconciliation. Detect collection changes as newly added jobs or disappearance of a tracked current-page job, not disappearance of any job in the global five-minute snapshot.

- [ ] **Step 4: Verify the focused tests pass**

Run the focused Karma command from Step 2. Expected: all recording-session component specs pass.

### Task 2: Unify primary list width, spacing, and scrolling

**Files:**
- Modify: `webapp/src/app/tasks/tasks.component.html`
- Modify: `webapp/src/app/tasks/tasks.component.scss`
- Modify: `webapp/src/app/tasks/task-list/task-list.component.scss`
- Modify: `webapp/src/app/tasks/tasks.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.html`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.scss`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/clip-library/clip-library.component.scss`
- Modify: `webapp/src/app/upload-tasks/clip-library/clip-library.component.spec.ts`

**Interfaces:**
- Consumes: the existing application shell's fixed-height `.main-content` and existing ng-zorro tables.
- Produces: full-width list route shells with consistent content padding and route-owned scrolling.

- [ ] **Step 1: Add failing layout contract tests**

Assert that room and recording/upload wrappers no longer carry `.primary-page`, that room list content has `24px` desktop padding, and that the clip-library host computes to vertical `overflow: auto` with full-height layout.

- [ ] **Step 2: Verify the layout tests fail**

Run the three affected component spec files with Karma. Expected: the old max-width-class assertions and missing clip overflow fail.

- [ ] **Step 3: Apply the approved layout baseline**

Remove `.primary-page` only from the three constrained main-list wrappers, move room toolbar and list into one `24px` content region, remove the task-list's extra outer `12px`, and set clip-library host height/overflow. Preserve the existing responsive table rules and use `12px` outer padding below `575px`.

- [ ] **Step 4: Verify focused and full frontend checks**

Run:

```bash
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless
cd webapp && npx ng lint
cd webapp && npm run build
```

Expected: all tests and lint pass; the production bundle completes successfully.

### Task 3: Release and deploy the verified build

**Files:**
- Modify: `.github/workflows/test.yml`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `compose.synology.yml`
- Modify: `docs/operations/synology-multi-network.md`
- Create: `docs/releases/3.0.0-beta.23.md`
- Modify: `src/blrec/__init__.py`
- Modify: `synology.env.example`
- Modify: `tests/release/test_github_release_workflows.py`
- Modify: `tests/release/test_synology_release_contract.py`
- Modify: `tests/release/test_version_metadata.py`

**Interfaces:**
- Consumes: the current Container Manager project at `/volume1/docker/blrec-next/workspace/compose.yml`.
- Produces: a published image version referenced by the NAS Compose project.

- [ ] **Step 1: Prepare and verify beta 23 metadata**

Update every version contract from `3.0.0-beta.22` to `3.0.0-beta.23`, add release notes describing the list layout and realtime refresh fixes, and run:

```bash
.venv/bin/python -m pytest -q tests/release
.venv/bin/python -m build
git diff --check
```

Expected: release tests and package build pass with version `3.0.0-beta.23`.

- [ ] **Step 2: Publish the verified release**

```bash
git push origin master
git tag -a v3.0.0-beta.23 -m "BLREC 3.0.0-beta.23"
git push origin v3.0.0-beta.23
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
docker buildx imagetools inspect ghcr.io/luweicn/blrec:3.0.0-beta.23
```

Expected: the release workflow succeeds and the public multi-architecture image contains both `linux/amd64` and `linux/arm64`.

- [ ] **Step 3: Update the NAS Compose image version**

Connect using the credentials in `SYNO_ADMIN_USERNAME` and `SYNO_ADMIN_PASSWORD`. Back up `/volume1/docker/blrec-next/workspace/compose.yml`, replace only `3.0.0-beta.22` with `3.0.0-beta.23`, then run from `/volume1/docker/blrec-next/workspace`:

```bash
/usr/local/bin/docker-compose -p blrec-next -f compose.yml config >/dev/null
/usr/local/bin/docker-compose -p blrec-next -f compose.yml pull
/usr/local/bin/docker-compose -p blrec-next -f compose.yml up -d
```

- [ ] **Step 4: Verify deployment**

Confirm container `blrec-next` is healthy, `http://192.168.50.24:2234/api/v1/version` reports `3.0.0-beta.23`, Docker still reports the `blrec-next` Compose project and host networking, `/cfg`, `/log`, `/rec`, and `/clips` mounts are unchanged, and the last 200 log lines contain no startup or realtime errors.
