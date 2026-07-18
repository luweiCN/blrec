# Single-Timeline Highlight Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the split highlight-editing workflow with one synchronized timeline that supports multiple pending blue ranges, one selected draft, green created clips, inline marker actions, and accurate current-session upload intent.

**Architecture:** Keep media loading, clipping, upload, and marker APIs in the existing Angular `HighlightEditorComponent`, but make the video element and custom timeline share `HTMLVideoElement.currentTime` as their only playback position. Reuse the existing in-memory `HighlightClipDraft[]` collection for multiple pending ranges and `editingDraftId` for the single selected draft. Derive current upload intent in the recording-session list SQL from the explicit decision, suppression, and current room policy.

**Tech Stack:** Python 3.8+, FastAPI, SQLite, pytest, Angular, TypeScript, Jasmine/Karma, SCSS, native Pointer Events and Fullscreen APIs.

## Global Constraints

- Do not add new frontend or backend dependencies.
- Do not provide start/end keyboard shortcuts.
- All seek, marker, boundary, naming, and draft-selection interactions stay inside the timeline work area.
- Multiple blue pending ranges may coexist, but only one may expose editing controls at a time.
- Created clips remain green and immutable; editing one creates a new blue draft copy.
- Remove thumbnail generation and its HTTP route completely.
- Preserve existing clipping, danmaku, upload, download, retry, and deletion behavior.
- Delete the throwaway HTML prototype after browser verification.

---

### Task 1: Derive the displayed upload intent from current policy

**Files:**
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`

**Interfaces:**
- Consumes: `recording_sessions.upload_decision`, `upload_suppressions`, and `room_upload_policies.enabled`.
- Produces: `RecordingSession.upload_intent` with `none | auto | upload | skip`, used unchanged by the Angular response model.

- [ ] **Step 1: Write the failing backend test**

Add a test that starts a room with an enabled policy, verifies `list_sessions()` returns `auto`, then covers explicit `upload`, explicit `skip`, a suppression row, and a disabled policy:

```python
@pytest.mark.asyncio
async def test_list_sessions_derives_current_upload_intent(database) -> None:
    await seed_upload_policy(database)
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)

    assert (await journal.list_sessions())[0].upload_intent == 'auto'
    await database.execute(
        "UPDATE recording_sessions SET upload_decision='upload' WHERE id=?",
        (session.id,),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'upload'
    await database.execute(
        "UPDATE recording_sessions SET upload_decision='skip' WHERE id=?",
        (session.id,),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'skip'
```

- [ ] **Step 2: Run the backend test and verify RED**

Run: `pytest tests/bili_upload/test_journal.py::test_list_sessions_derives_current_upload_intent -q`

Expected: FAIL because the enabled room still returns legacy `upload_intent='none'`.

- [ ] **Step 3: Implement the SQL derivation**

In `RecordingJournalBridge.list_sessions()`, replace the selected legacy field with a `CASE` expression and join the room policy:

```sql
CASE
    WHEN suppression.session_id IS NOT NULL
         OR session.upload_decision='skip' THEN 'skip'
    WHEN session.upload_decision='upload' THEN 'upload'
    WHEN policy.enabled=1 THEN 'auto'
    ELSE 'none'
END AS upload_intent
```

Add `LEFT JOIN room_upload_policies policy ON policy.room_id=session.room_id` before the existing filters.

- [ ] **Step 4: Lock the recording-list copy to the derived field**

Add a Jasmine assertion that a recording with `uploadIntent: 'auto'` renders `本场结束后上传` and one with `uploadIntent: 'skip'` renders `本场不上传`. Keep `displayStateDetail()` as a pure mapping of the derived API field.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
pytest tests/bili_upload/test_journal.py -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts
```

Expected: both suites PASS.

Commit: `fix: derive current recording upload intent`

### Task 2: Remove recording thumbnails end to end

**Files:**
- Modify: `tests/web/test_recording_sessions_routes.py`
- Delete: `tests/bili_upload/test_recording_thumbnail.py`
- Delete: `src/blrec/bili_upload/recording_thumbnail.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.html`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.scss`

**Interfaces:**
- Removes: `GET /api/v1/recording-sessions/parts/{part_id}/thumbnail` and `RecordingSessionService.thumbnailUrl()`.
- Preserves: signed media playback access and all cover-image behavior in recording lists.

- [ ] **Step 1: Change the route test to expect absence and verify RED**

Replace the successful thumbnail-route test with:

```python
def test_recording_thumbnail_route_is_not_exposed(client: TestClient) -> None:
    response = client.get('/api/v1/recording-sessions/parts/2/thumbnail')
    assert response.status_code == 404
```

Run: `pytest tests/web/test_recording_sessions_routes.py::test_recording_thumbnail_route_is_not_exposed -q`

Expected: FAIL because the existing route responds with validation/authentication behavior rather than 404.

- [ ] **Step 2: Add the frontend absence test and verify RED**

In the editor spec, assert that `.thumbnail-strip` is absent and remove `thumbnailUrl` from the recording-service spy only after observing the test fail against the current template.

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`

Expected: FAIL because the thumbnail strip still exists.

- [ ] **Step 3: Remove backend thumbnail code**

Delete the provider module and its dedicated tests. Remove the provider import, singleton, injectable callable, route, audit events, and JPEG response from `recording_sessions.py`.

- [ ] **Step 4: Remove frontend thumbnail code**

Delete `TimelineThumbnail`, `timelineThumbnails`, hover methods, thumbnail builders, HTML strip, thumbnail SCSS, the service URL method, and its URL test. Media access must no longer trigger any random-frame request.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
pytest tests/web/test_recording_sessions_routes.py -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/upload-tasks/shared/recording-session.service.spec.ts --include=src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts
```

Expected: both suites PASS and `rg -n "recording_thumbnail|thumbnailUrl|thumbnail-strip" src tests webapp/src/app/upload-tasks` returns no result.

Commit: `perf: remove recording timeline thumbnails`

### Task 3: Implement multiple pending ranges with one selected draft

**Files:**
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`

**Interfaces:**
- Reuses: `HighlightClipDraft[] drafts` as blue pending ranges.
- Reuses: `editingDraftId: number | null` as the only selected draft.
- Produces: optional working boundaries, an anchored timeline action target, and methods to select, deselect, copy, cancel, inspect, and create a draft.

- [ ] **Step 1: Write failing state-transition tests**

Cover these transitions through public component methods:

```typescript
it('keeps completed drafts when the playhead moves elsewhere', () => {
  component.setTimelineAction(20_000);
  component.setTimelineBoundary('start');
  component.setTimelineAction(40_000);
  component.setTimelineBoundary('end');
  const draftId = component.editingDraftId;

  component.setTimelineAction(70_000);

  expect(component.drafts.map((draft) => draft.id)).toContain(draftId as number);
  expect(component.editingDraftId).toBeNull();
});

it('selects only one of several pending drafts', () => {
  component.drafts = [draftOne, draftTwo];
  component.selectDraftForEditing(draftOne);
  component.selectDraftForEditing(draftTwo);

  expect(component.editingDraftId).toBe(draftTwo.id);
  expect(component.drafts).toHaveSize(2);
});
```

Also cover invalid boundary preservation, `±1` second preview, cancellation deleting only the selected blue draft, and a created clip copy becoming a new blue draft.

- [ ] **Step 2: Run the component spec and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`

Expected: FAIL because timeline action methods and deselection semantics do not exist.

- [ ] **Step 3: Implement the minimal timeline state**

Change working boundaries to `number | null`, add `timelineActionMs`, `hoverTimelineMs`, and a discriminated popover state:

```typescript
type TimelinePopover =
  | { readonly kind: 'none' }
  | { readonly kind: 'point'; readonly timeMs: number; readonly markerId: number | null }
  | { readonly kind: 'boundary'; readonly boundary: 'start' | 'end' }
  | { readonly kind: 'draft'; readonly draftId: number }
  | { readonly kind: 'clip'; readonly clipId: number };
```

Completing both valid boundaries must insert or update one draft, select it, and open the draft popover. Clicking blank timeline space must persist the selected draft, clear only its selected state, seek, and open a point popover. Remove the document-level `I`/`O` shortcut handler.

- [ ] **Step 4: Preserve clipping behavior**

Keep the existing `inspectClip()`, keyframe-confirmation, `createClip()`, retry, and SSE progress logic. `createDraft()` must still operate on the selected stored draft and convert it to a server-side clip only after the user confirms the inline name panel.

- [ ] **Step 5: Run the component spec and commit**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include=src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`

Expected: PASS.

Commit: `feat: model pending highlight ranges on the timeline`

### Task 4: Replace the editor UI with the approved single timeline

**Files:**
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.html`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.scss`

**Interfaces:**
- Consumes: Task 3 timeline state and the existing `HTMLVideoElement`/FLV player.
- Produces: one custom timeline containing playback controls, markers, pending drafts, created clips, boundaries, and one contextual popover.

- [ ] **Step 1: Write failing DOM interaction tests**

Assert all approved affordances:

```typescript
expect(video.hasAttribute('controls')).toBeFalse();
expect(fixture.nativeElement.querySelectorAll('.timeline-track')).toHaveSize(1);
expect(fixture.nativeElement.querySelector('.marker-list')).toBeNull();
expect(fixture.nativeElement.querySelector('.selection-editor')).toBeNull();
expect(fixture.nativeElement.querySelector('.draft-panel')).toBeNull();
expect(fixture.nativeElement.querySelectorAll('.draft-range')).toHaveSize(2);
```

Add pointer tests proving hover does not seek, pointer release opens the point actions, marker clicks snap exactly, only the selected blue draft renders handles, and creation removes one blue range while adding a green clip.

- [ ] **Step 2: Run the component spec and verify RED**

Run the focused Karma command from Task 3.

Expected: FAIL against the old native controls and three external editing panels.

- [ ] **Step 3: Build custom playback controls inside the timeline**

Remove the `<video controls>` attribute. Add timeline-local play/pause, current/total time, mute/volume, and fullscreen controls. `timeupdate` reads `video.currentTime`; pointer seeking writes the same property. No second progress input or range control may exist.

- [ ] **Step 4: Render all editing affordances on the timeline**

Render orange downward marker pins, blue pending ranges, one selected blue range with start/end handles, green created ranges, the red playhead, hover guide, and unsafe tail. Use event priority `boundary > marker > pending draft > created clip > playhead/track` and stop propagation at each interactive layer.

- [ ] **Step 5: Render one anchored contextual panel**

Use a single contextual region whose content changes by `TimelinePopover.kind`: point actions, boundary `−1 秒 / +1 秒`, marker name/edit/delete, pending name/create/cancel, or created-clip copy action. Clamp left/right alignment so the panel remains inside the timeline.

- [ ] **Step 6: Retain only the created-clip status list below**

Keep preview, download, upload settings, retry, status, Bilibili link, and delete controls. Remove independent marker, selection-editor, and pending-draft panels.

- [ ] **Step 7: Run focused tests and commit**

Run the focused Karma suite and `npx ng lint`.

Expected: PASS with no template/type/style errors.

Commit: `feat: unify highlight editing on one timeline`

### Task 5: Browser verification, cleanup, and full regression

**Files:**
- Delete: `webapp/src/app/upload-tasks/highlight-editor/single-timeline.prototype.html`
- Modify: `docs/superpowers/specs/2026-07-18-single-timeline-highlight-editor-design.md` only if implementation revealed a documented contradiction.

**Interfaces:**
- Consumes: completed Tasks 1–4.
- Produces: releasable source without prototype artifacts.

- [ ] **Step 1: Run the complete automated suites**

Run:

```bash
pytest -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless
cd webapp && npx ng lint && npm run build
cd browser-extension && npm test && npm run typecheck && npm run build
```

Expected: all suites PASS.

- [ ] **Step 2: Exercise the real editor in a browser**

Verify one local recording part: play without native controls, set boundaries during playback, click/drag seek, select a highpoint, create two blue drafts, dismiss/reopen each, adjust each boundary by one second, create one clip, and confirm its range turns green while the other remains blue.

- [ ] **Step 3: Verify the upload-intent regression against NAS-shaped data**

Use an enabled `follow_room` fixture and confirm the list displays `本场结束后上传`; verify explicit skip displays `本场不上传`.

- [ ] **Step 4: Remove the prototype and scan for dead code**

Delete the prototype, then run:

```bash
rg -n "single-timeline\.prototype|recording_thumbnail|thumbnailUrl|thumbnail-strip|handleEditorShortcut" .
git diff --check
```

Expected: no obsolete implementation references and no whitespace errors.

- [ ] **Step 5: Commit the verified result**

Commit: `chore: finalize single-timeline highlight editor`
