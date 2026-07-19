# Room Management Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make room filters and bulk actions explicit, selection-based, and consistent, while moving live-file cutting to recording sessions.

**Architecture:** `TasksComponent` becomes the single owner of room upload policies so the toolbar and list filter the same snapshot. `TaskListComponent` keeps selection and eligible-action logic. Recording sessions reuse the existing room task cut endpoint instead of introducing another backend action.

**Tech Stack:** Angular 14, TypeScript, RxJS, NG-ZORRO, Jasmine/Karma.

## Global Constraints

- Do not use a worktree.
- Do not add new backend endpoints or dependencies.
- Room deletion never deletes historical recordings, clips, danmaku, or Bilibili submissions.
- Hide inapplicable actions instead of leaving unexplained disabled buttons.
- Keep the existing NG-ZORRO visual vocabulary and Chinese business terminology.

---

### Task 1: Submission filters share one policy snapshot

**Files:**
- Modify: `webapp/src/app/tasks/shared/task.model.ts`
- Modify: `webapp/src/app/tasks/tasks.component.ts`
- Modify: `webapp/src/app/tasks/tasks.component.html`
- Modify: `webapp/src/app/tasks/toolbar/toolbar.component.ts`
- Modify: `webapp/src/app/tasks/toolbar/toolbar.component.html`
- Modify: `webapp/src/app/tasks/task-list/task-list.component.ts`
- Modify: `webapp/src/app/tasks/task-list/task-list.component.html`
- Test: `webapp/src/app/tasks/toolbar/toolbar.component.spec.ts`
- Test: `webapp/src/app/tasks/task-list/task-list.component.spec.ts`
- Test: `webapp/src/app/tasks/tasks.component.spec.ts`

**Interfaces:**
- Produces: `SubmissionVisibilityFilter = 'public' | 'private' | null`.
- Produces: `TasksComponent.roomUploadPolicies`, `submissionVisibilityFilter`, and `submissionAccountFilter`.
- Consumes: `RoomUploadPolicy.resolvedAccountId`, `resolvedAccountName`, and `isOnlySelf`.

- [ ] **Step 1: Write failing component tests**

Add assertions that the toolbar exposes `监控已开启/监控已关闭`, visibility options, and unique account options; add list assertions that public/private/account filters combine with the automatic-submission filter.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/tasks/toolbar/toolbar.component.spec.ts --include=src/app/tasks/task-list/task-list.component.spec.ts --include=src/app/tasks/tasks.component.spec.ts`

Expected: failures for missing filter inputs/options and old status labels.

- [ ] **Step 3: Implement the shared policy snapshot and filters**

Use the parent page to load policies once, derive unique account options, and pass the same policies and selected filters to both children. Filter visibility with:

```ts
if (this.submissionVisibilityFilter === 'private') {
  return policy?.isOnlySelf === true;
}
if (this.submissionVisibilityFilter === 'public') {
  return policy !== null && !policy.isOnlySelf;
}
```

Filter account with `policy?.resolvedAccountId === submissionAccountFilter`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Task 1 test command; expect all included specs to pass.

### Task 2: Selection-based room actions

**Files:**
- Modify: `webapp/src/app/tasks/toolbar/toolbar.component.ts`
- Modify: `webapp/src/app/tasks/toolbar/toolbar.component.html`
- Modify: `webapp/src/app/tasks/task-list/task-list.component.ts`
- Modify: `webapp/src/app/tasks/task-list/task-list.component.html`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.ts`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.html`
- Test: `webapp/src/app/tasks/toolbar/toolbar.component.spec.ts`
- Test: `webapp/src/app/tasks/task-list/task-list.component.spec.ts`
- Test: `webapp/src/app/tasks/task-item/task-item.component.spec.ts`

**Interfaces:**
- Consumes: existing `TaskManagerService.runBatchAction()` and `removeTask()` behavior.
- Produces: conditional action buttons with eligible counts and an always-available selected-room delete action.

- [ ] **Step 1: Write failing action tests**

Cover removal of global mutating actions, conditional start/stop labels with counts, absence of batch cut/force-stop, deletion of selected running rooms, and conditional single-room `强制中断`.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/tasks/toolbar/toolbar.component.spec.ts --include=src/app/tasks/task-list/task-list.component.spec.ts --include=src/app/tasks/task-item/task-item.component.spec.ts`

Expected: failures showing old global menu entries, disabled buttons, and old force-stop/cut copy.

- [ ] **Step 3: Implement minimal action changes**

Remove global start/stop/delete handlers. Render start/stop only when `eligibleCount(...) > 0`, remove batch cut and force-stop, and let delete return every selected room ID. Use confirmation copy that states current recording stops but historical files and Bilibili submissions remain.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Task 2 test command; expect all included specs to pass.

### Task 3: Move current-file cutting to recording sessions

**Files:**
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Test: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- Consumes: `TaskManagerService.canCutStream(roomId)` and `cutStream(roomId)`.
- Produces: `canCutCurrentFile(session)` and `cutCurrentFile(session)`.

- [ ] **Step 1: Write a failing recording-session test**

Assert that `切割当前文件` appears only for `scope === 'recordings'`, `sourceKind === 'live'`, and `state === 'open'`; clicking it must check capability and call the existing cut endpoint once.

- [ ] **Step 2: Run the spec and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'`

Expected: failure because the action and methods do not exist.

- [ ] **Step 3: Implement the action with loading protection**

Use `switchMap`, `EMPTY`, and `finalize` so repeated clicks are ignored while a room cut is in progress. Include the action in the existing ellipsis menu only when `canCutCurrentFile(session)` is true.

- [ ] **Step 4: Run the recording-session spec and verify GREEN**

Run the Task 3 test command; expect the spec to pass.

### Task 4: Regression verification and production bundle

**Files:**
- Regenerate: `src/blrec/data/webapp/`

- [ ] **Step 1: Run frontend regression checks**

Run the full headless Jasmine suite, `npx ng lint`, focused ESLint for changed TypeScript files, and `npm run build`.

- [ ] **Step 2: Review generated and source diffs**

Run `git diff --check` and confirm every changed line maps to the approved design.

- [ ] **Step 3: Commit the implementation**

Commit source, tests, and generated web assets with `feat: clarify room management actions`.
