# Highlight Editor and Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide named, accurately mapped highlight markers, a familiar multi-range clip editor, persistent clip records, and enough correlated logs to diagnose the complete user flow.

**Architecture:** Capture the marker timestamp before showing a non-modal naming popover. Map normal live clicks from backend receipt time to the active part's first-byte anchor and subtract only deliberate rewind beyond the sampled baseline. The editor keeps draft ranges client-side, creates persistent clips through one automatic inspect/create action, and projects linked upload progress through the shared SSE stream.

**Tech Stack:** Chromium MV3 extension, TypeScript, Angular, Python/FastAPI, SQLite, FFmpeg/FFprobe, SSE, pytest, Vitest/Jasmine.

## Global Constraints

- Clicking Add Highlight locks time before the user types a name.
- Normal player buffering must not move the marker earlier; intentional rewind must.
- No separate “检查裁剪范围” action in the normal workflow.
- Client diagnostics must never contain credentials, tokens, signed media URLs or danmaku content.

---

### Task 1: Capture named highlights without interrupting playback

**Files:**
- Modify: `browser-extension/src/shared/player.ts`
- Modify: `browser-extension/src/shared/messages.ts`
- Modify: `browser-extension/src/shared/api.ts`
- Modify: `browser-extension/src/content.ts`
- Modify: `browser-extension/src/content.css`
- Modify: `browser-extension/tests/player.spec.ts`
- Modify: `browser-extension/tests/content.spec.ts`

**Interfaces:**
- `PlayerObservation` includes `observedAtMs`, `currentTimeMs`, `seekableEndMs`, `rawDelayMs`, `baselineDelayMs`, and `effectiveRewindMs`.
- Add-highlight message includes optional `name` with maximum 200 characters.

- [ ] **Step 1: Add failing observation and popover tests**

```typescript
controller.clickAddHighlight();
clock.advanceBy(5000);
controller.submitName('精彩操作');
expect(message.observedAtMs).toBe(clickTime);
expect(message.name).toBe('精彩操作');
expect(message.effectiveRewindMs).toBe(0);
```

Also cover blank names, Enter, Escape, repeated markers, baseline sampling and a 60-second rewind.

- [ ] **Step 2: Run extension tests and verify RED**

Run: `cd browser-extension && npm test`
Expected: FAIL because naming and baseline fields do not exist.

- [ ] **Step 3: Implement anchored popover and rolling baseline**

Capture `observePlayer()` before rendering the popover. Sample delay while the room is recording; calculate `effectiveRewindMs = max(0, rawDelayMs - baselineDelayMs)` and treat small first-sample delay as zero rewind.

- [ ] **Step 4: Run tests, typecheck and build**

Run: `cd browser-extension && npm test && npm run typecheck && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add browser-extension
git commit -m "feat: name and calibrate highlight markers"
```

### Task 2: Persist marker observations and map from first-byte anchors

**Files:**
- Create: `src/blrec/bili_upload/migrations/0021_initial.sql`
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `src/blrec/web/routers/browser_extension.py`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/web/test_highlights_routes.py`

**Interfaces:**
- Marker request accepts the observation fields and optional name from Task 1.
- Marker persistence keeps the active `part_id`, anchor time, raw delay, baseline and effective rewind.

- [ ] **Step 1: Add failing 7:41 regression tests**

```python
marker = await service.create_marker(received_at_ms=click, effective_rewind_ms=0, ...)
assert marker.content_at_ms - part.timeline_start_at_ms == 461_000
```

Cover the known first-byte `05:21:34` and click `05:29:15` case, explicit one-minute rewind, reconnect to a new part, optional name and legacy requests.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/bili_upload/test_highlights.py tests/web/test_highlights_routes.py -q`
Expected: FAIL because the old service subtracts all player delay and does not store observation data.

- [ ] **Step 3: Implement anchored mapping and migration**

Find the active recording part at receipt time, set `content_at_ms = received_at_ms - effective_rewind_ms`, and let timeline mapping subtract that part's persisted `timeline_start_at_ms`. Fall back to legacy behavior only when no part anchor exists, and audit every raw component.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/migrations/0021_initial.sql src/blrec/bili_upload/highlights.py src/blrec/web/routers/highlights.py src/blrec/web/routers/browser_extension.py tests/bili_upload/test_highlights.py tests/web/test_highlights_routes.py
git commit -m "fix: map highlights from recording anchors"
```

### Task 3: Avoid full-file FFprobe scans

**Files:**
- Modify: `src/blrec/bili_upload/highlight_cut.py`
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `tests/bili_upload/test_highlight_cut.py`
- Modify: `tests/bili_upload/test_highlights.py`

**Interfaces:**
- `ClipSource` carries known `duration_ms` and optional indexed keyframes.
- `LosslessClipper.inspect` probes stream compatibility separately and requests keyframes only near each selected start.

- [ ] **Step 1: Add a failing bounded-probe test**

Assert the command contains `-read_intervals` around the requested start and never performs an unbounded `-skip_frame nokey` scan for a multi-gigabyte source. Keep full output validation for the newly generated small clip.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py -q`
Expected: FAIL because `_probe_media` scans every keyframe.

- [ ] **Step 3: Implement metadata-first, interval-second inspection**

Use persisted FLV keyframes when present. Otherwise inspect a bounded interval before/after the requested start, validate the selection against timeline duration, and return a clear index-not-ready state if no preceding keyframe is available.

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/highlight_cut.py src/blrec/bili_upload/highlights.py tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py
git commit -m "perf: bound highlight keyframe probing"
```

### Task 4: Rebuild the editor around multiple ranges

**Files:**
- Modify: `webapp/src/app/recordings/highlight-editor/highlight-editor.component.{ts,html,scss,spec.ts}`
- Create: `webapp/src/app/recordings/highlight-editor/clip-range.model.ts`
- Create: `webapp/src/app/recordings/highlight-editor/timeline.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/recordings/shared/highlight.model.ts`
- Modify: `webapp/src/app/recordings/shared/highlight.service.ts`

**Interfaces:**
- Draft range: `{id,startMs,endMs,name,markerId}`.
- Timeline emits `seek`, `startChange`, `endChange`; editor supports several drafts.
- One Create action automatically inspects and creates, surfacing a confirmation only when keyframe lead exceeds 10 seconds.

- [ ] **Step 1: Add failing interaction tests**

Test Set Start/Set End at playhead, drag handles, marker seek, multiple ranges, per-range preview/delete/create, automatic initial load and keyframe confirmation without a separate inspect button.

- [ ] **Step 2: Run focused Angular tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/recordings/highlight-editor/**/*.spec.ts'`
Expected: FAIL on current numeric-input/single-range UI.

- [ ] **Step 3: Implement the familiar timeline workflow**

Keep exact numeric values available only as secondary fine adjustment. Render parts, markers, unsafe tail, playhead and all draft ranges on one accessible custom track; update active duration through the shared SSE stream.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/recordings/highlight-editor webapp/src/app/recordings/shared
git commit -m "feat: add multi-range highlight editor"
```

### Task 5: Show persistent clip and upload progress records

**Files:**
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `src/blrec/web/main.py`
- Modify: `webapp/src/app/recordings/highlight-editor/*`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/web/test_highlights_routes.py`

- [ ] **Step 1: Add failing clip summary tests**

Assert clip list includes title, requested/actual range, generation state, linked job ID, percent, upload state, submit state and BVID. Assert title edits before job creation become the default upload title without mutating an existing job snapshot.

- [ ] **Step 2: Run tests and verify RED**

Run backend highlight tests and the focused editor specs.
Expected: FAIL on missing list/progress projection.

- [ ] **Step 3: Implement clip summaries and SSE projection**

Add list/update endpoints, join clip upload sessions to job progress, and merge `highlight_progress` plus upload progress in the editor. Provide play, rename, regenerate, create/open upload and delete actions according to state.

- [ ] **Step 4: Run tests and verify GREEN**

Run the commands from Steps 1-2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/highlights.py src/blrec/web/routers/highlights.py src/blrec/web/main.py webapp/src/app/recordings/highlight-editor tests/bili_upload/test_highlights.py tests/web/test_highlights_routes.py
git commit -m "feat: track highlight clip publishing"
```

### Task 6: Add correlated client diagnostics

**Files:**
- Create: `src/blrec/web/routers/client_diagnostics.py`
- Modify: `src/blrec/web/main.py`
- Create: `tests/web/test_client_diagnostics_routes.py`
- Create: `webapp/src/app/core/services/client-diagnostics.service.{ts,spec.ts}`
- Modify: recording player/editor and extension API call sites.

**Interfaces:**
- `POST /api/v1/client-diagnostics` accepts an allowlisted event, correlated IDs, elapsed milliseconds, state and bounded error code/message.
- Events include media access, player attached/first-frame/stalled/error, timeline load/retry, confirmed clip range and highlight popover submission.

- [ ] **Step 1: Add failing security, rate-limit and correlation tests**

Reject unknown fields/events, credentials and oversized strings; deduplicate repeated stall events; verify logs include IDs but no token or signed URL.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/web/test_client_diagnostics_routes.py tests/logging/test_audit.py -q`; run the diagnostics service Angular spec.
Expected: FAIL because the endpoint/service do not exist.

- [ ] **Step 3: Implement allowlisted telemetry and call sites**

Use authenticated same-origin requests, per-session/event throttling and audit JSON. Report state transitions only, not playback timeupdate or drag events.

- [ ] **Step 4: Run complete verification**

Run backend tests/static checks, Angular tests/build, and extension tests/typecheck/build.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/web/routers/client_diagnostics.py src/blrec/web/main.py tests/web/test_client_diagnostics_routes.py webapp/src/app/core/services/client-diagnostics.service.* webapp/src/app/recordings browser-extension
git commit -m "feat: log media and highlight client diagnostics"
```

### Task 7: End-to-end NAS acceptance

**Files:**
- Create: `docs/operations/recording-workflow-acceptance.md`

- [ ] **Step 1: Build and publish a beta image**

Run the repository release checks, tag the next beta, verify GHCR digest and update the Synology Compose project without changing its project name or volumes.

- [ ] **Step 2: Exercise the real workflow**

Add a room, start recording, create named highlights, verify approximately 7:41 mapping, open active playback without refresh, close auto submission before live end, verify no job, create several clips, create one upload task, and inspect SSE progress.

- [ ] **Step 3: Reconstruct the workflow only from logs**

Use correlated IDs to list room collection, recording start/first bytes, marker capture/mapping, media access/first frame, clip generation, final upload decision and upload progress. Record any missing boundary as a failing test before changing code.

- [ ] **Step 4: Commit evidence**

```bash
git add docs/operations/recording-workflow-acceptance.md
git commit -m "docs: record recording workflow acceptance"
```
