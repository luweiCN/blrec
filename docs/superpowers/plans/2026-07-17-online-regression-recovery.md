# Online Regression Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore unattended recording and the affected NAS interfaces while keeping live danmaku anonymous by default and using one coherent account Cookie only as a same-broadcast fallback.

**Architecture:** A small connection wrapper owns the anonymous/authenticated transport mode for the existing `DanmakuClient`; the live controller publishes confirmed LIVE immediately and connects danmaku in the background. Angular realtime events re-enter `NgZone`, media components refresh their overlay views explicitly, and the extension renders either a connected summary or the edit form.

**Tech Stack:** Python 3.8, asyncio, aiohttp, pytest; Angular 15, RxJS, Jasmine/Karma; TypeScript 4.9, Vitest/jsdom; Docker/GHCR/Synology Compose.

## Global Constraints

- Do not use a git worktree.
- A short-lived danmaku token is always fetched; account Cookie is absent in anonymous mode.
- Cookie fallback is attempted at most once per connection activation and is reset only after the broadcast ends.
- Danmaku failure must not stop or delay video recording.
- Never log a Cookie or danmaku token.
- Preserve the untracked local `AGENTS.md`.

---

### Task 1: Coherent anonymous-first danmaku transport

**Files:**
- Create: `src/blrec/bili/danmaku_connection.py`
- Modify: `src/blrec/bili/danmaku_client.py`
- Modify: `src/blrec/task/task.py`
- Test: `tests/task/test_live_connection_controller.py`

**Interfaces:**
- Produces: `DanmakuConnection.start()`, `stop(reset_mode: bool = False)`, `restart()`, and `set_room_id(room_id: int)`.
- Consumes: `DanmakuClient.configure(session, appapi, webapi, headers)` and task callbacks that configure a complete anonymous or authenticated transport.

- [ ] **Step 1: Write failing transport tests**

Add tests proving anonymous is attempted first, an anonymous failure performs one authenticated fallback, reconnects keep the successful authenticated mode, and `stop(reset_mode=True)` restores anonymous mode.

- [ ] **Step 2: Verify the tests fail**

Run: `.venv/bin/pytest tests/task/test_live_connection_controller.py -q`

Expected: failures because `DanmakuConnection` and `DanmakuClient.configure` do not exist.

- [ ] **Step 3: Implement the minimal transport wrapper**

Use this public shape:

```python
class DanmakuConnection:
    def __init__(self, client, configure_anonymous, configure_authenticated): ...
    async def start(self) -> None: ...
    async def stop(self, *, reset_mode: bool = False) -> None: ...
    async def restart(self) -> None: ...
    def set_room_id(self, room_id: int) -> None: ...
```

`configure_authenticated()` returns `False` when no usable Cookie exists. Task configuration must create `AppApi` and `WebApi` using the same routed session and same headers as the WebSocket.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/pytest tests/task/test_live_connection_controller.py -q`

Expected: all transport and existing connection tests pass.

### Task 2: Start recording independently from danmaku

**Files:**
- Modify: `src/blrec/bili/live_connection_controller.py`
- Modify: `src/blrec/task/task.py`
- Test: `tests/task/test_live_connection_controller.py`

**Interfaces:**
- Consumes: `DanmakuConnection` from Task 1.
- Produces: confirmed LIVE applies to `LiveMonitor` before a background connection task; PREPARING/ROUND cancels the task and calls `stop(reset_mode=True)`.

- [ ] **Step 1: Write failing lifecycle tests**

Cover a blocked WebSocket start while `monitor.confirmed == [LIVE]`, a failed start that leaves the controller live, a later confirmation that retries connection without another live-began event, and broadcast end resetting the transport.

- [ ] **Step 2: Verify the lifecycle tests fail**

Run: `.venv/bin/pytest tests/task/test_live_connection_controller.py -q`

Expected: current controller waits for or propagates WebSocket failure.

- [ ] **Step 3: Implement background connection lifecycle**

Track one `_connection_task`; keep `_active` as confirmed broadcast state. Apply LIVE first, schedule the task, and consume/log terminal task errors. On end or close, cancel/await the task, stop the connection, reset its mode, then disable the monitor.

- [ ] **Step 4: Run backend regression tests**

Run: `.venv/bin/pytest tests/task/test_live_connection_controller.py tests/test_application_live_status.py tests/task/test_task_manager_managed_cookie.py -q`

Expected: pass.

### Task 3: Refresh Angular views after realtime and media callbacks

**Files:**
- Modify: `webapp/src/app/core/services/realtime.service.ts`
- Modify: `webapp/src/app/core/services/realtime.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`

**Interfaces:**
- Produces: every EventSource callback emits inside `NgZone.run`; media callbacks call local change detection after their state transition.

- [ ] **Step 1: Write failing zone and asynchronous-view tests**

The realtime test uses a spy `NgZone` and asserts `run()` wraps each event. Media tests use `Subject` responses, emit after initial change detection, and assert loading content is replaced without an extra user action.

- [ ] **Step 2: Verify frontend tests fail**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/core/services/realtime.service.spec.ts' --include='src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'`

Expected: zone and automatic DOM refresh assertions fail.

- [ ] **Step 3: Implement minimal change detection**

Inject `NgZone` in `RealtimeService` and `ChangeDetectorRef` in the two media components. Run subscriber emissions inside the zone and call `detectChanges()` only at completed asynchronous state transitions.

- [ ] **Step 4: Run focused frontend tests**

Run the command from Step 2.

Expected: pass.

### Task 4: Render a stable browser-extension connected state

**Files:**
- Modify: `browser-extension/src/options.ts`
- Modify: `browser-extension/src/options.html`
- Modify: `browser-extension/src/options.css`
- Create: `browser-extension/tests/options.spec.ts`

**Interfaces:**
- Consumes: existing `ROOM_STATUS` message with `roomId: 0` as an authorization probe.
- Produces: connected summary with `#connected-summary` and `#edit-connection`; editable form remains hidden until needed.

- [ ] **Step 1: Write failing jsdom tests**

Test valid stored authorization, invalid stored authorization, successful pairing, and clicking 修改 while preserving the old token until reconnection succeeds.

- [ ] **Step 2: Verify extension tests fail**

Run: `cd browser-extension && npm test -- options.spec.ts`

Expected: connected summary elements or state transitions are absent.

- [ ] **Step 3: Implement connected/edit states**

Use native semantic HTML and the existing controls. Do not add animation or a new component library. Display only address and username; never expose the token.

- [ ] **Step 4: Build and test extension**

Run: `cd browser-extension && npm test && npm run typecheck && npm run build`

Expected: all pass and `dist/options.*` is refreshed.

### Task 5: Remove unnecessary network-table overflow

**Files:**
- Modify: `webapp/src/app/network/network.component.html`
- Modify: `webapp/src/app/network/network.component.scss`
- Modify: `webapp/src/app/network/network.component.spec.ts`

**Interfaces:**
- Produces: a single table-owned horizontal scrollbar below the true compact minimum width; desktop action column remains visible.

- [ ] **Step 1: Write failing layout contract test**

Assert the table scroll width is `1040px` and `.network-panel` no longer owns horizontal overflow.

- [ ] **Step 2: Verify the test fails**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/network/network.component.spec.ts'`

Expected: current `1180px` and panel overflow violate the contract.

- [ ] **Step 3: Reduce column widths and remove nested overflow**

Use widths `130/180/145/180/70/245/70px`, preserve the existing route-grid mobile minimum width, and let `nz-table` own overflow.

- [ ] **Step 4: Run network and full frontend checks**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npx ng lint && npm run build`

Expected: pass.

### Task 6: Release and NAS acceptance

**Files:**
- Modify: `src/blrec/__init__.py`
- Modify: `.github/workflows/test.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `compose.synology.yml`
- Modify: `synology.env.example`
- Modify: `docs/operations/synology-multi-network.md`
- Create: `docs/releases/3.0.0-beta.6.md`
- Modify: `tests/release/test_github_release_workflows.py`
- Modify: `tests/release/test_synology_release_contract.py`
- Modify: `tests/release/test_version_metadata.py`
- Verify: `/volume1/docker/blrec-next/workspace/compose.yml` on the NAS.

- [ ] **Step 1: Run repository verification**

Run backend focused/full checks, extension checks, Angular checks, `git diff --check`, and confirm only intended files plus local untracked `AGENTS.md` remain.

- [ ] **Step 2: Prepare and publish the next beta tag**

Follow the existing beta release workflow, push `master` and the tag, and wait for the multi-architecture GHCR image to finish successfully.

- [ ] **Step 3: Update Synology Compose**

Back up the Compose file, change only the image tag, pull, recreate the `blrec-next` service, and verify health/restart count. Do not print credentials or application secrets.

- [ ] **Step 4: Perform live acceptance**

Confirm a currently LIVE room creates a growing recording file even if danmaku is unavailable; verify logs show anonymous or Cookie-fallback mode without secrets; open playback and highlight editor; verify the extension connected summary and network action column.
