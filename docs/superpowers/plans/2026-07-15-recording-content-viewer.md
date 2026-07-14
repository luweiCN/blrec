# Recording Content Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Present one clear upload status, link completed archives to Bilibili, stream local recording files safely, and page through completed danmaku XML.

**Architecture:** `RecordingJournalBridge` remains the authority for part IDs and paths. A focused content reader resolves a part to a safe local media/XML resource; FastAPI exposes authenticated Range and cursor endpoints. Angular renders native MP4 or `mpegts.js 1.8.0` FLV playback and falls back to the exact Bilibili part URL only after approval.

**Tech Stack:** Python 3.8+, FastAPI/Starlette, SQLite, defused lxml parser settings, Angular 15, ng-zorro, mpegts.js 1.8.0, Jasmine/Karma.

## Global Constraints

- Never accept a filesystem path from the browser; all reads use a database part ID.
- Local media wins over a remote link; remote URL is `https://www.bilibili.com/video/{bvid}?p={part_index}`.
- Recording FLV is snapshot playback: each response fixes the readable size when opened.
- Danmaku page size is at most 100 and XML entity/network resolution is disabled.
- Keep `AGENTS.md` untracked and untouched.

---

### Task 1: User-facing upload status and archive link

**Files:**
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Test: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- Consumes: existing `RecordingSession.uploadJob` and `partIndex`.
- Produces: `displayUploadStatus(session): string` and `archiveUrl(session, partIndex?): string | null`.

- [ ] **Step 1: Write failing component tests**

```ts
it('shows one completed status and links an approved archive', () => {
  component.view = readyView(approvedSession({ bvid: 'BV1test' }));
  fixture.detectChanges();
  expect(text()).toContain('投稿完成');
  expect(text()).not.toContain('投稿：已确认');
  expect(link('BV1test').href).toContain('/video/BV1test');
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'`

Expected: FAIL because the second submit-state line remains and the title is not linked.

- [ ] **Step 3: Implement the presentation helpers and template**

```ts
archiveUrl(session: RecordingSession, partIndex?: number): string | null {
  const job = session.uploadJob;
  if (!job?.bvid || !['approved', 'completed'].includes(job.state)) return null;
  const suffix = partIndex === undefined ? '' : `?p=${partIndex}`;
  return `https://www.bilibili.com/video/${encodeURIComponent(job.bvid)}${suffix}`;
}
```

Remove the list/detail `submitStateLabel` row and map `approved`/`completed` to `投稿完成`; map scheduled review to `等待定时发布` when the API exposes that display state.

- [ ] **Step 4: Re-run the focused test and commit**

Run the Step 2 command; expected PASS.

Commit: `git commit -m "fix: simplify upload task status presentation"`

### Task 2: Safe recording content reader

**Files:**
- Create: `src/blrec/bili_upload/recording_content.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Test: `tests/bili_upload/test_recording_content.py`

**Interfaces:**
- Produces: `RecordingContentReader.media(part_id: int) -> MediaResource` and `danmaku(part_id: int, cursor: int, limit: int) -> DanmakuPage`.
- `MediaResource` contains `path`, `size`, `content_type`, `recording`, `part_index`, `bvid`, and `remote_available`.

- [ ] **Step 1: Write failing reader tests**

```python
@pytest.mark.asyncio
async def test_media_prefers_existing_final_file(tmp_path, database):
    final = tmp_path / 'part.mp4'
    final.write_bytes(b'video')
    part_id = await seed_part(database, final_path=str(final))
    resource = await RecordingContentReader(database).media(part_id)
    assert resource.path == str(final)
    assert resource.size == 5
```

Add tests for source fallback, missing local file with approved BVID, non-file paths, XML pagination, malformed XML, and an external entity payload that must not be expanded.

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/bili_upload/test_recording_content.py`

Expected: collection error because `recording_content` does not exist.

- [ ] **Step 3: Implement minimal immutable resource/page types and queries**

```python
@dataclass(frozen=True)
class DanmakuPage:
    items: Tuple[DanmakuLine, ...]
    next_cursor: Optional[int]

class RecordingContentReader:
    async def media(self, part_id: int) -> MediaResource:
        row = await self._database.fetchone(MEDIA_QUERY, (part_id,))
        if row is None:
            raise RecordingContentNotFound('recording part not found')
        return await asyncio.get_running_loop().run_in_executor(
            None, self._resolve_media, row
        )
```

Use only parameterized queries. Resolve `final_path` first, then `source_path`; call `stat()` once and retain that size. Parse XML with `lxml.etree.iterparse(..., resolve_entities=False, no_network=True)` and clear processed elements.

- [ ] **Step 4: Run tests and commit**

Run: `pytest -q tests/bili_upload/test_recording_content.py tests/bili_upload/test_journal.py`

Expected: PASS.

Commit: `git commit -m "feat: add safe recording content reader"`

### Task 3: Authenticated Range and danmaku APIs

**Files:**
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `src/blrec/web/main.py`
- Test: `tests/web/test_recording_sessions_routes.py`

**Interfaces:**
- Produces: `GET /api/v1/recording-sessions/parts/{part_id}/media` and `/danmaku?cursor=0&limit=100`.
- Consumes: `RecordingContentReader` from Task 2.

- [ ] **Step 1: Write failing route tests for 200, 206, 416, 404 and pagination**

```python
def test_media_range_returns_fixed_slice(client):
    response = client.get('/api/v1/recording-sessions/parts/2/media', headers={
        'x-api-key': 'test-api-key', 'range': 'bytes=2-4'
    })
    assert response.status_code == 206
    assert response.headers['content-range'] == 'bytes 2-4/10'
    assert response.content == b'234'
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/web/test_recording_sessions_routes.py`

Expected: 404 for the new routes.

- [ ] **Step 3: Implement strict single-range parsing and streaming**

```python
def parse_range(value: Optional[str], size: int) -> Tuple[int, int]:
    if size <= 0:
        raise RangeNotSatisfiable()
    if value is None:
        return 0, size - 1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
    if match is None or ',' in value:
        raise RangeNotSatisfiable()
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise RangeNotSatisfiable()
        return max(0, size - suffix), size - 1
    start = int(first)
    end = size - 1 if not last else min(int(last), size - 1)
    if start >= size or end < start:
        raise RangeNotSatisfiable()
    return start, end
```

Open the file after resolution, seek once, and yield no more than the captured end. Return `Accept-Ranges`, `Content-Length`, `Content-Range` and the correct media type. Map reader exceptions to safe 404/409 responses.

- [ ] **Step 4: Run backend route tests and commit**

Run: `pytest -q tests/web/test_recording_sessions_routes.py tests/bili_upload/test_recording_content.py`

Expected: PASS.

Commit: `git commit -m "feat: expose recording media and danmaku APIs"`

### Task 4: Angular part content dialog

**Files:**
- Modify: `webapp/package.json`
- Modify: `webapp/package-lock.json`
- Create: `webapp/src/app/upload-tasks/part-content-dialog/part-content-dialog.component.ts`
- Create: `webapp/src/app/upload-tasks/part-content-dialog/part-content-dialog.component.html`
- Create: `webapp/src/app/upload-tasks/part-content-dialog/part-content-dialog.component.scss`
- Create: `webapp/src/app/upload-tasks/part-content-dialog/part-content-dialog.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,scss}`

**Interfaces:**
- Input: `{ session: RecordingSession; part: RecordingPart; focus: 'video' | 'danmaku' }`.
- Service: `mediaUrl(partId)` and `listDanmaku(partId, cursor, limit)`.

- [ ] **Step 1: Add failing component/service tests**

Cover MP4 native playback, FLV `mpegts.js`, teardown, reload, 100-row pagination, escaped text, local-missing remote link, and no-link pre-approval.

- [ ] **Step 2: Verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/part-content-dialog/*.spec.ts'`

Expected: no matching component/module.

- [ ] **Step 3: Add the pinned dependency and minimal component**

Run: `cd webapp && npm install mpegts.js@1.8.0 --save-exact`

```ts
ngOnDestroy(): void {
  this.requestSubscription?.unsubscribe();
  this.player?.pause();
  this.player?.unload();
  this.player?.detachMediaElement();
  this.player?.destroy();
}
```

Use ng-zorro modal focus handling, semantic buttons, accessible names, a text-only danmaku list, and an `aria-live="polite"` loading/result region.

- [ ] **Step 4: Run component suite, lint and build; commit**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless`

Run: `cd webapp && npx ng lint && npm run build`

Expected: all PASS.

Commit: `git commit -m "feat: add recording playback and danmaku viewer"`

### Task 5: End-to-end verification

**Files:**
- Modify only if a test exposes a defect in files already listed above.

- [ ] **Step 1: Run backend regression suite**

Run: `pytest -q`

Expected: all tests PASS.

- [ ] **Step 2: Browser smoke test against local services**

Open an active FLV part, confirm snapshot playback ends at its captured size, reload and observe more content, page danmaku, then verify a deleted approved part opens `?p=N` on Bilibili.

- [ ] **Step 3: Commit any test-proven correction**

Commit only files directly related to the failing behavior with `fix: correct recording content viewer`.
