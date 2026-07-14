# Upload Task Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the recording-session list out of the Bilibili account page into an independent top-level “上传任务” page.

**Architecture:** Keep `/uploads` and `UploadsModule` dedicated to account management. Add a lazy `/upload-tasks` feature module, move the recording-session component/model/service into it, and expose a small page shell that owns the “上传任务” heading.

**Tech Stack:** Angular, TypeScript, Ng-Zorro, Jasmine/Karma, SCSS.

## Global Constraints

- Do not change backend APIs or database semantics in this change.
- Preserve existing account-management behavior and `/uploads` links.
- Preserve recording-session loading, error, empty, degraded, and accessible table behavior.
- Do not use a Worktree or subagent.

---

### Task 1: Lock the independent navigation behavior

**Files:**
- Modify: `webapp/src/app/app.component.spec.ts`
- Modify: `webapp/src/app/uploads/uploads.component.spec.ts`

**Interfaces:**
- Consumes: current app shell and account page templates.
- Produces: failing tests that require separate `/upload-tasks` and `/uploads` entries and forbid the recording list in the account page.

- [x] **Step 1: Write the failing navigation test**

Replace the single uploads-link assertion with:

```typescript
it('shows separate upload-task and Bilibili-account navigation', () => {
  const fixture = TestBed.createComponent(AppComponent);
  fixture.detectChanges();

  const uploadTasks = fixture.nativeElement.querySelector(
    'a[href="/upload-tasks"]'
  ) as HTMLAnchorElement;
  const accounts = fixture.nativeElement.querySelector(
    'a[href="/uploads"]'
  ) as HTMLAnchorElement;

  expect(uploadTasks.textContent?.trim()).toBe('上传任务');
  expect(accounts.textContent?.trim()).toBe('投稿账号');
});
```

- [x] **Step 2: Write the failing account-page ownership test**

Keep `RecordingSessionsStubComponent` during the RED run so Angular can compile the
current template, and add:

```typescript
it('does not render upload tasks inside account management', () => {
  fixture.detectChanges();
  expect(
    fixture.nativeElement.querySelector('app-recording-sessions')
  ).toBeNull();
});
```

Remove the stub and its declaration only after the production template no longer
contains `<app-recording-sessions>`.

- [x] **Step 3: Run tests and verify RED**

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts' --include='src/app/uploads/uploads.component.spec.ts'
```

Expected: navigation test fails because `/upload-tasks` is absent; account test compilation or assertion fails because `<app-recording-sessions>` is still present.

### Task 2: Add the upload-task feature module and move ownership

**Files:**
- Modify: `webapp/src/app/app-routing.module.ts`
- Modify: `webapp/src/app/app.component.html`
- Modify: `webapp/src/app/uploads/uploads.component.html`
- Modify: `webapp/src/app/uploads/uploads.module.ts`
- Create: `webapp/src/app/upload-tasks/upload-tasks-routing.module.ts`
- Create: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Create: `webapp/src/app/upload-tasks/upload-tasks.component.ts`
- Create: `webapp/src/app/upload-tasks/upload-tasks.component.html`
- Create: `webapp/src/app/upload-tasks/upload-tasks.component.scss`
- Create: `webapp/src/app/upload-tasks/upload-tasks.component.spec.ts`
- Move: `webapp/src/app/uploads/recording-sessions/*` to `webapp/src/app/upload-tasks/recording-sessions/`
- Move: `webapp/src/app/uploads/shared/recording-session.*` to `webapp/src/app/upload-tasks/shared/`

**Interfaces:**
- Consumes: `GET /api/v1/recording-sessions` through `RecordingSessionService`.
- Produces: lazy `/upload-tasks` route and `UploadTasksComponent` containing `<app-recording-sessions>`.

- [x] **Step 1: Add a failing page-shell test**

Create `upload-tasks.component.spec.ts` with a stub child and assertions:

```typescript
@Component({ selector: 'app-recording-sessions', template: '' })
class RecordingSessionsStubComponent {}

it('renders the upload-task heading and list', () => {
  const fixture = TestBed.createComponent(UploadTasksComponent);
  fixture.detectChanges();
  expect(fixture.nativeElement.textContent).toContain('上传任务');
  expect(
    fixture.nativeElement.querySelector('app-recording-sessions')
  ).not.toBeNull();
});
```

- [x] **Step 2: Run the page test and verify RED**

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/upload-tasks.component.spec.ts'
```

Expected: FAIL because `UploadTasksComponent` does not exist.

- [x] **Step 3: Implement the minimal feature shell**

Use the existing lazy-module pattern. The route is `{ path: '', component: UploadTasksComponent }`. The page template is:

```html
<div class="upload-tasks-page">
  <nz-page-header
    nzTitle="上传任务"
    nzSubtitle="查看录制、上传、投稿和弹幕回灌进度"
  ></nz-page-header>
  <app-recording-sessions></app-recording-sessions>
</div>
```

Register the page, moved recording component, and required Ng-Zorro modules only in `UploadTasksModule`. Remove the recording declaration and unused Ng-Zorro imports from `UploadsModule`.

- [x] **Step 4: Register route and navigation**

Add the lazy `/upload-tasks` route before `/uploads`. Add an “上传任务” sidebar item using `cloud-upload`; change the account item icon to `user`. Register `UserOutline` in the shared icon provider and the app test icon fixture.

- [x] **Step 5: Update list wording**

Change the moved card title from `录制会话与分 P` to `上传任务列表`, while retaining the explanation that the current phase groups recorded parts. Update its existing component assertion accordingly.

- [x] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts' --include='src/app/uploads/uploads.component.spec.ts' --include='src/app/upload-tasks/**/*.spec.ts'
```

Expected: all focused specs pass.

- [x] **Step 7: Run complete verification**

Run:

```bash
cd webapp
npx eslint src/app/app.component.html src/app/app.component.spec.ts src/app/app-routing.module.ts src/app/icons-provider.module.ts src/app/uploads src/app/upload-tasks
npm test -- --watch=false --browsers=ChromeHeadless
npm run build
```

Expected: changed-file lint passes, all Karma tests pass, and the production build emits a lazy upload-task chunk.

- [x] **Step 8: Commit**

```bash
git add docs/superpowers/plans/2026-07-14-upload-task-navigation.md webapp/src/app src/blrec/data/webapp
git commit -m "feat: separate upload tasks from account management"
```

## Plan Self-Review

- Spec coverage: independent navigation, account-only `/uploads`, upload-task page ownership, preserved session behavior, and verification all map to explicit steps.
- Placeholder scan: no implementation placeholders remain.
- Type consistency: `UploadTasksModule`, `UploadTasksComponent`, and `/upload-tasks` use one name across routes, tests, and templates.
