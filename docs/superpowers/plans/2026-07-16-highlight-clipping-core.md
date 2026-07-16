# Highlight Clipping Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 BLREC 内实现独立高光书签、正在录制视频的时间轴预览、FFmpeg 无损裁剪、同步弹幕裁剪，以及可复用现有投稿链路的独立单 P 高光任务。

**Architecture:** 高光书签只按房间和绝对时间保存，通过每个录像分段的本地时间锚点动态映射到编辑时间轴。高光片段使用持久化后台任务执行关键帧探测、stream copy 和弹幕归零；完成后包装成 `source_kind='highlight'` 的派生媒体场次，以复用现有上传、审核、合集和回灌实现，避免高风险重建上传主表及其全部外键子表。

**Tech Stack:** Python 3.8、FastAPI、SQLite、FFmpeg/FFprobe、lxml、Angular 15、RxJS、mpegts.js、Jasmine/Karma、pytest。

## Global Constraints

- 不使用 git worktree；所有修改在当前工作区按小提交完成。
- 高光标记不强制关联录像、分段或上传任务，也没有“录像不存在”错误状态。
- 正在录制文件的最后 10 秒不可提交裁剪；后端必须再次校验。
- 默认只允许 FFmpeg `-c copy` 无损裁剪，不自动回退到重新编码。
- 实际起点比选择起点提前超过 10 秒时必须要求显式确认。
- 跨分段仅在音视频编码参数兼容时无损拼接。
- 视频和弹幕都以实际无损起点归零；高光点只用于导航。
- 每个高光输出对应一个独立单 P 投稿任务，合集继续按投稿账号隔离。
- Python 代码保持 3.8 兼容、Black 88 列、四空格和现有单引号风格；Angular 保持两空格和单引号。
- 不记录 Cookie、管理员凭据、插件令牌或完整弹幕正文。

---

## File Structure

### Backend files

- Create `src/blrec/bili_upload/migrations/0018_initial.sql`: 高光、剪辑任务、源分段关系及媒体时间锚点。
- Create `src/blrec/bili_upload/highlights.py`: 高光 CRUD、时间轴映射和剪辑任务持久化接口。
- Create `src/blrec/bili_upload/highlight_cut.py`: 关键帧探测、兼容性检查及 FFmpeg stream copy。
- Create `src/blrec/bili_upload/highlight_danmaku.py`: 分段弹幕筛选、偏移与 XML 输出。
- Create `src/blrec/bili_upload/highlight_worker.py`: 可恢复剪辑执行器和任务进度快照。
- Create `src/blrec/web/routers/highlights.py`: 管理员高光、时间轴、剪辑与投稿接口。
- Modify `src/blrec/bili_upload/database.py`: 迁移版本和 `highlight_clips` 租约表白名单。
- Modify `src/blrec/bili_upload/journal.py`: 分段本地时间锚点、派生媒体场次和响应字段。
- Modify `src/blrec/bili_upload/upload.py`: 为指定派生场次创建默认暂停的上传任务。
- Modify `src/blrec/bili_upload/runtime.py`: 组装剪辑服务、工作器和上传任务创建入口。
- Modify `src/blrec/bili_upload/retention.py`: 跳过正在被剪辑任务引用的源文件。
- Modify `src/blrec/bili_upload/task_actions.py`: 删除派生高光任务时只删除高光输出。
- Modify `src/blrec/web/realtime.py`, `src/blrec/web/main.py`, `src/blrec/web/routers/__init__.py`: 注册服务并发布统一 SSE 事件。

### Frontend files

- Create `webapp/src/app/upload-tasks/highlight-editor/*`: 独立剪辑页面、时间轴、样式和测试。
- Create `webapp/src/app/upload-tasks/shared/highlight.model.ts`: 高光、时间轴和剪辑状态类型。
- Create `webapp/src/app/upload-tasks/shared/highlight.service.ts`: 高光与剪辑 API 客户端。
- Modify `webapp/src/app/upload-tasks/upload-tasks-routing.module.ts`: 增加 `highlights/:sessionId` 路由。
- Modify `webapp/src/app/upload-tasks/upload-tasks.module.ts`: 声明编辑页面并导入表单/进度组件。
- Modify `webapp/src/app/upload-tasks/recording-sessions/*`: 增加“剪辑”入口和高光任务标识。
- Modify `webapp/src/app/upload-tasks/shared/recording-session.model.ts`: 增加 `sourceKind`、`highlightClipId` 和 `timelineStartAtMs`。
- Modify `webapp/src/app/upload-tasks/shared/recording-session.service.ts`: 提供编辑器媒体 URL。
- Modify `webapp/src/app/core/services/realtime.service.ts`: 识别 `highlight_progress` 事件。

### Tests

- Create `tests/bili_upload/test_highlights.py`.
- Create `tests/bili_upload/test_highlight_cut.py`.
- Create `tests/bili_upload/test_highlight_danmaku.py`.
- Create `tests/bili_upload/test_highlight_worker.py`.
- Create `tests/web/test_highlights_routes.py`.
- Modify `tests/bili_upload/test_database.py`, `tests/bili_upload/test_journal.py`, `tests/bili_upload/test_upload.py`, `tests/bili_upload/test_retention.py`, `tests/web/test_realtime_routes.py`.

---

### Task 1: Add highlight persistence and media timeline anchors

**Files:**
- Create: `src/blrec/bili_upload/migrations/0018_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `tests/bili_upload/test_database.py`

**Interfaces:**
- Produces: claimable table `highlight_clips`; tables `highlight_markers`, `highlight_clip_sources`; `recording_sessions.source_kind`; `recording_parts.timeline_start_at_ms`.
- Consumes: existing `BiliUploadDatabase.claim()` lease contract.

- [ ] **Step 1: Write the failing migration tests**

Add final-schema assertions and an independent marker insert:

```python
assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 18
assert {
    'highlight_markers',
    'highlight_clips',
    'highlight_clip_sources',
} <= await database.table_names()

await database.execute(
    'INSERT INTO highlight_markers('
    'room_id,observed_at_ms,player_delay_ms,content_at_ms,title,anchor_name,'
    'name,note,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
    (100, 10_000, 500, 9_500, '直播', '主播', '直播 · 00:09', '', 'web', 10, 10),
)
assert await database.scalar('SELECT COUNT(*) FROM highlight_markers') == 1
```

Also assert `source_kind='invalid'` and a clip with `requested_end_ms <= requested_start_ms` raise `sqlite3.IntegrityError`.

- [ ] **Step 2: Run the migration test and confirm failure**

Run: `python -m pytest tests/bili_upload/test_database.py::test_migration_enables_wal_constraints_and_claim_indexes -q`

Expected: FAIL because schema version is 17 and highlight tables do not exist.

- [ ] **Step 3: Add migration 0018**

Use this complete schema shape:

```sql
ALTER TABLE recording_sessions
ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'live'
CHECK (source_kind IN ('live','highlight'));

ALTER TABLE recording_parts
ADD COLUMN timeline_start_at_ms INTEGER
CHECK (timeline_start_at_ms IS NULL OR timeline_start_at_ms > 0);

CREATE TABLE highlight_markers (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL CHECK (room_id > 0),
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms > 0),
    player_delay_ms INTEGER NOT NULL DEFAULT 0
        CHECK (player_delay_ms BETWEEN 0 AND 300000),
    content_at_ms INTEGER NOT NULL CHECK (content_at_ms > 0),
    title TEXT NOT NULL,
    anchor_name TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    source TEXT NOT NULL CHECK (source IN ('web','browser_extension')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX highlight_markers_room_time_idx
ON highlight_markers(room_id,content_at_ms,id);

CREATE TABLE highlight_clips (
    id INTEGER PRIMARY KEY,
    marker_id INTEGER REFERENCES highlight_markers(id) ON DELETE SET NULL,
    room_id INTEGER NOT NULL CHECK (room_id > 0),
    source_session_id INTEGER REFERENCES recording_sessions(id) ON DELETE SET NULL,
    upload_session_id INTEGER UNIQUE
        REFERENCES recording_sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    requested_start_ms INTEGER NOT NULL CHECK (requested_start_ms >= 0),
    requested_end_ms INTEGER NOT NULL CHECK (requested_end_ms > requested_start_ms),
    actual_start_ms INTEGER CHECK (actual_start_ms IS NULL OR actual_start_ms >= 0),
    actual_end_ms INTEGER CHECK (
        actual_end_ms IS NULL OR
        (actual_start_ms IS NOT NULL AND actual_end_ms > actual_start_ms)
    ),
    output_video_path TEXT,
    output_xml_path TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('queued','processing','ready','failed','cancelled')
    ),
    keyframe_confirmation_required INTEGER NOT NULL DEFAULT 0
        CHECK (keyframe_confirmation_required IN (0,1)),
    keyframe_confirmed INTEGER NOT NULL DEFAULT 0
        CHECK (keyframe_confirmed IN (0,1)),
    error_message TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_until INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX highlight_clips_claim_idx
ON highlight_clips(state,next_attempt_at,priority,id);

CREATE TABLE highlight_clip_sources (
    clip_id INTEGER NOT NULL REFERENCES highlight_clips(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES recording_parts(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    requested_start_ms INTEGER NOT NULL CHECK (requested_start_ms >= 0),
    requested_end_ms INTEGER NOT NULL CHECK (requested_end_ms > requested_start_ms),
    actual_start_ms INTEGER CHECK (actual_start_ms IS NULL OR actual_start_ms >= 0),
    actual_end_ms INTEGER CHECK (
        actual_end_ms IS NULL OR
        (actual_start_ms IS NOT NULL AND actual_end_ms > actual_start_ms)
    ),
    PRIMARY KEY (clip_id,ordinal),
    UNIQUE (clip_id,part_id)
);

CREATE INDEX highlight_clip_sources_part_idx
ON highlight_clip_sources(part_id,clip_id);
```

Set `latest_version = 18` and add `'highlight_clips'` to `_CLAIM_TABLES`.

- [ ] **Step 4: Test lease claiming and foreign-key behavior**

Insert one queued clip, claim it through `database.claim('highlight_clips', ('queued',), 'worker')`, delete its marker, and assert `marker_id IS NULL` while the clip survives.

Run: `python -m pytest tests/bili_upload/test_database.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/migrations/0018_initial.sql src/blrec/bili_upload/database.py tests/bili_upload/test_database.py
git commit -m "feat: add highlight persistence schema"
```

---

### Task 2: Record local media anchors and implement independent highlight markers

**Files:**
- Create: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Create: `tests/bili_upload/test_highlights.py`
- Modify: `tests/bili_upload/test_journal.py`

**Interfaces:**
- Produces: `HighlightService.create_marker()`, `update_marker()`, `delete_marker()`, `timeline(session_id, active_durations_ms)`.
- Produces dataclasses: `HighlightMarker`, `TimelinePart`, `MappedHighlight`, `HighlightTimeline`.
- Consumes: `recording_parts.timeline_start_at_ms`; old rows fall back to `record_start_time * 1000`.

- [ ] **Step 1: Write failing tests for the local anchor**

Use a mutable clock at `1000.250` and call:

```python
await journal.video_created('run', '/rec/p1.flv', record_start_time=990)
row = await database.fetchone(
    'SELECT record_start_time,timeline_start_at_ms FROM recording_parts WHERE run_id=?',
    ('run',),
)
assert dict(row) == {'record_start_time': 990, 'timeline_start_at_ms': 1_000_250}
```

Run: `python -m pytest tests/bili_upload/test_journal.py -q`

Expected: FAIL because `timeline_start_at_ms` remains null.

- [ ] **Step 2: Persist the local write anchor**

In `RecordingJournalBridge.video_created()`, calculate `timeline_start_at_ms = int(self._clock() * 1000)` before entering the database transaction and include it in the insert and audit payload. Do not replace `record_start_time`; existing danmaku behavior still depends on it.

- [ ] **Step 3: Write failing marker and timeline tests**

Cover these exact behaviors:

```python
marker = await service.create_marker(
    room_id=100,
    observed_at_ms=1_100_000,
    player_delay_ms=20_000,
    title='测试直播',
    anchor_name='主播',
    source='web',
)
assert marker.content_at_ms == 1_080_000

timeline = await service.timeline(1, active_durations_ms={2: 120_000})
assert timeline.parts[0].timeline_start_ms == 0
assert timeline.parts[1].stable_end_ms == 210_000
assert [item.marker.id for item in timeline.markers] == [marker.id]
```

Also insert a marker outside every part and assert it remains in the database but is absent from `timeline.markers`.

- [ ] **Step 4: Implement the focused service**

Expose four focused methods on `HighlightService`: `create_marker()` accepts
`room_id`, `observed_at_ms`, `player_delay_ms`, `title`, `anchor_name`, and
`source` and returns `HighlightMarker`; `update_marker(marker_id, name, note)`
returns the updated marker; `delete_marker(marker_id)` returns `None`; and
`timeline(session_id, active_durations_ms)` returns `HighlightTimeline`.
`active_durations_ms` is an explicit `Mapping[int, int]` supplied by the web
layer and is never implemented as a mutable default argument.

Clamp `player_delay_ms` to `0..300000`, use the supplied observation time only as an audit field, and compute `content_at_ms = int(self._clock() * 1000) - player_delay_ms`. Build the default name as `直播标题 + 格式化高光时间`, audit creation/update/deletion, build part ranges independently, and never infer a missing marker state.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/bili_upload/test_highlights.py tests/bili_upload/test_journal.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/blrec/bili_upload/highlights.py src/blrec/bili_upload/journal.py tests/bili_upload/test_highlights.py tests/bili_upload/test_journal.py
git commit -m "feat: map independent highlights onto recordings"
```

---

### Task 3: Inspect keyframes and perform lossless video cuts

**Files:**
- Create: `src/blrec/bili_upload/highlight_cut.py`
- Create: `tests/bili_upload/test_highlight_cut.py`

**Interfaces:**
- Produces: `LosslessClipper.inspect(sources, requested_start_ms, requested_end_ms, stable_end_ms) -> ClipInspection`.
- Produces: `LosslessClipper.cut(inspection, output_path) -> CutArtifact`.
- Consumes: ordered source paths and per-part local ranges from `HighlightService.timeline()`.

- [ ] **Step 1: Write failing keyframe selection tests**

Use a fake probe returning keyframes `[0, 28_600, 30_600]` and request `30_000..80_000`:

```python
inspection = clipper.inspect(
    (
        ClipSource(
            part_id=1,
            path=str(source),
            requested_start_ms=30_000,
            requested_end_ms=80_000,
        ),
    ),
    requested_start_ms=30_000,
    requested_end_ms=80_000,
    stable_end_ms=100_000,
)
assert inspection.actual_start_ms == 28_600
assert inspection.actual_end_ms == 80_000
assert inspection.extra_lead_ms == 1_400
assert inspection.confirmation_required is False
```

Add tests for a previous keyframe 12 seconds earlier, a requested end inside the last 10 seconds, and incompatible codec profiles across two parts.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/bili_upload/test_highlight_cut.py -q`

Expected: FAIL because `highlight_cut` does not exist.

- [ ] **Step 3: Implement safe FFprobe inspection**

Define immutable input `ClipSource(part_id, path, requested_start_ms,
requested_end_ms, keyframes_ms)`, output
`InspectedClipSource(part_id, path, actual_start_ms, actual_end_ms,
output_offset_ms, profile)`, `MediaProfile`,
`ClipInspection(sources, requested_start_ms, requested_end_ms,
actual_start_ms, actual_end_ms, extra_lead_ms, confirmation_required)`, and
`CutArtifact` dataclasses. Invoke FFprobe with an argument tuple and
`shell=False`:

```python
command = (
    self._ffprobe,
    '-v',
    'error',
    '-select_streams',
    'v:0',
    '-skip_frame',
    'nokey',
    '-show_entries',
    'frame=best_effort_timestamp_time:stream=codec_name,width,height,'
    'r_frame_rate,extradata_size:format=duration',
    '-of',
    'json',
    source.path,
)
```

Reject missing video, invalid JSON, non-positive duration, incompatible codec/size/rate, and ranges outside `stable_end_ms`. Choose the nearest keyframe at or before the requested start. Set `confirmation_required = extra_lead_ms > 10_000`.

- [ ] **Step 4: Write the failing command-array and output-validation test**

Mock `subprocess.run`, create an output file in the FFmpeg branch, and assert the command contains `'-c', 'copy'`, `'-avoid_negative_ts', 'make_zero'`, `shell=False`, and no shell string.

- [ ] **Step 5: Implement stream-copy cutting**

For one `InspectedClipSource` from `inspection.sources`, run:

```python
command = (
    self._ffmpeg,
    '-hide_banner',
    '-nostdin',
    '-ss',
    self._seconds(source.actual_start_ms),
    '-i',
    source.path,
    '-t',
    self._seconds(source.actual_end_ms - source.actual_start_ms),
    '-map',
    '0:v:0',
    '-map',
    '0:a?',
    '-c',
    'copy',
    '-avoid_negative_ts',
    'make_zero',
    '-y',
    temporary_output,
)
```

For multiple sources, first create stream-copy temporary segments, then use an FFmpeg concat demuxer with `-safe 0 -c copy`. Write concat entries with safely escaped absolute paths; never interpolate them into a shell command. Probe the final output and require video, retained audio when present, and duration within `max(2 seconds, 2%)` of the planned duration. Rename the valid temporary file atomically to `output_path`.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/bili_upload/test_highlight_cut.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/blrec/bili_upload/highlight_cut.py tests/bili_upload/test_highlight_cut.py
git commit -m "feat: add lossless highlight cutter"
```

---

### Task 4: Cut and rebase danmaku for the actual video range

**Files:**
- Create: `src/blrec/bili_upload/highlight_danmaku.py`
- Create: `tests/bili_upload/test_highlight_danmaku.py`

**Interfaces:**
- Produces: `HighlightDanmakuClipper.cut(sources, output_path) -> DanmakuCutResult`.
- Consumes: `DanmakuClipSource(xml_path, actual_start_ms, actual_end_ms, output_offset_ms)`.

- [ ] **Step 1: Write failing XML slicing tests**

Create P1 XML with messages at 9s, 10s, 15s and P2 XML with messages at 1s and 6s. Slice P1 `10..20s`, P2 `0..5s`, with P2 output offset 10s. Assert output progress values are `0`, `5`, and `11` seconds, original text/attributes remain plain XML, and the 9s/6s messages are absent.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/bili_upload/test_highlight_danmaku.py -q`

Expected: FAIL because the clipper does not exist.

- [ ] **Step 3: Implement safe streaming parsing and rebasing**

Expose immutable `DanmakuClipSource(xml_path, actual_start_ms,
actual_end_ms, output_offset_ms)` and
`DanmakuCutResult(output_path, source_count, message_count)` dataclasses.
`HighlightDanmakuClipper.cut(sources, output_path)` accepts a
`Sequence[DanmakuClipSource]` and returns `DanmakuCutResult`.

Parse only `<d>` elements with entity resolution and network access disabled. Parse the first `p` field as seconds, filter with a half-open range `[start, end)`, and write `new_ms = output_offset_ms + original_ms - actual_start_ms`. Preserve the remaining `p` fields and text; sort by `(new_ms, source_order, original_order)` without content-based deduplication. If no source XML exists, return `output_path=None` and do not create an empty file.

- [ ] **Step 4: Add security and missing-source tests**

Assert an XML external entity is never expanded and that one missing XML among valid sources is skipped while a completely missing set returns zero messages.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/bili_upload/test_highlight_danmaku.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/blrec/bili_upload/highlight_danmaku.py tests/bili_upload/test_highlight_danmaku.py
git commit -m "feat: align danmaku with highlight clips"
```

---

### Task 5: Add a persistent, restart-safe highlight worker

**Files:**
- Create: `src/blrec/bili_upload/highlight_worker.py`
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/retention.py`
- Create: `tests/bili_upload/test_highlight_worker.py`
- Modify: `tests/bili_upload/test_retention.py`

**Interfaces:**
- Produces: `HighlightWorker.run_once()`, `recover_interrupted()`, `progress()`.
- Produces: `HighlightService.create_clip(session_id, marker_id, name, requested_start_ms, requested_end_ms, confirm_keyframe, active_durations_ms) -> HighlightClip`.
- Consumes: `LosslessClipper`, `HighlightDanmakuClipper`, database leases, recording root.

- [ ] **Step 1: Write failing creation and safety tests**

Cover these cases:

```python
clip = await service.create_clip(
    session_id=1,
    marker_id=None,
    name='第一段高光',
    requested_start_ms=20_000,
    requested_end_ms=70_000,
    confirm_keyframe=False,
    active_durations_ms={2: 120_000},
)
assert clip.state == 'queued'

with pytest.raises(HighlightRangeUnavailable, match='最后 10 秒'):
    await service.create_clip(
        session_id=1,
        marker_id=None,
        name='过近',
        requested_start_ms=100_000,
        requested_end_ms=119_000,
        confirm_keyframe=False,
        active_durations_ms={2: 120_000},
    )
```

Assert source rows are written in order and a capacity cleanup query excludes parts referenced by queued/processing clips.

- [ ] **Step 2: Implement atomic clip creation**

Resolve the requested session range into per-part local intersections, inspect
keyframes before insertion, enforce explicit confirmation, and insert the clip
plus all `highlight_clip_sources` in one `database.write()` transaction.
Recheck session/part state and active durations inside that transaction. Store
output under:

```python
recording_root / 'highlights' / str(room_id) / 'highlight-{}.mp4'.format(clip_id)
```

Use `.partial` for work in progress and a sibling `.xml` for clipped danmaku.

- [ ] **Step 3: Write failing worker and recovery tests**

Use fake cutter/danmaku implementations and assert:

- queued → processing → ready;
- actual bounds and source bounds are persisted;
- `create_clip()` raises `HighlightConfirmationRequired` before inserting a task when inspection needs confirmation and `confirm_keyframe=False`;
- a confirmed task persists `keyframe_confirmation_required=1` and `keyframe_confirmed=1` before the worker invokes FFmpeg;
- a stale processing row is reset to queued at startup;
- a valid final output restores ready without running the cutter twice;
- a stale `.partial` file is removed before retry.

- [ ] **Step 4: Implement the worker**

Use a lease claim and a bounded thread call for blocking FFmpeg work:

```python
async def run_once(self) -> Optional[int]:
    claim = await self._database.claim(
        'highlight_clips', ('queued',), self._worker_id, now=int(self._clock())
    )
    if claim is None:
        return None
    await self._database.fenced_update(
        'highlight_clips', claim.id, claim.lease_owner, claim.lease_generation,
        {'state': 'processing', 'updated_at': int(self._clock())},
    )
    await asyncio.get_running_loop().run_in_executor(None, self._process_sync, claim)
    return claim.id
```

Before final success, require a non-empty valid output, persist actual ranges, generate optional XML, clear the lease and audit `highlight_clip_completed` with requested/actual bounds, source part IDs, output size, elapsed time and danmaku count. On deterministic input errors set `failed`; on transient file growth or I/O errors set `queued` with bounded backoff. Deleting a queued clip sets `cancelled`; deleting a ready clip removes only its generated video/XML and metadata.

- [ ] **Step 5: Wire runtime lifecycle**

Construct `HighlightService`, `LosslessClipper`, `HighlightDanmakuClipper`, and `HighlightWorker` in `BiliAccountRuntime.start()`. Add read-only properties and a background loop with a 2-second idle interval. Stop and await this loop before closing the database. Call `recover_interrupted()` before starting it.

- [ ] **Step 6: Protect referenced source parts**

Add this predicate to retention candidates and manual deletion preflight:

```sql
AND NOT EXISTS (
    SELECT 1 FROM highlight_clip_sources source
    JOIN highlight_clips clip ON clip.id=source.clip_id
    WHERE source.part_id=part.id AND clip.state IN ('queued','processing')
)
```

Return a concise conflict message for manual deletion; capacity cleanup silently skips the part.

- [ ] **Step 7: Run focused tests**

Run: `python -m pytest tests/bili_upload/test_highlight_worker.py tests/bili_upload/test_retention.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/blrec/bili_upload/highlight_worker.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/runtime.py src/blrec/bili_upload/retention.py tests/bili_upload/test_highlight_worker.py tests/bili_upload/test_retention.py
git commit -m "feat: process highlight clips reliably"
```

---

### Task 6: Reuse the upload pipeline through derived highlight sessions

**Files:**
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/policies.py`
- Modify: `tests/bili_upload/test_upload.py`
- Modify: `tests/bili_upload/test_task_actions.py`
- Modify: `tests/bili_upload/test_journal.py`

**Interfaces:**
- Produces: `UploadCoordinator.create_highlight_job(session_id) -> int`.
- Produces: `BiliAccountRuntime.create_highlight_upload_task(clip_id, manager_subject) -> int`.
- Consumes: a ready `highlight_clip` with optional XML and either the room's existing upload policy or the transient built-in default.

- [ ] **Step 1: Write a failing derived-session upload test**

Seed one ready clip, room policy and active primary account, then call:

```python
job_id = await runtime.create_highlight_upload_task(
    clip_id, manager_subject='administrator'
)
job = await database.fetchone(
    'SELECT job.state,job.operator_paused,session.source_kind '
    'FROM upload_jobs job JOIN recording_sessions session '
    'ON session.id=job.session_id WHERE job.id=?',
    (job_id,),
)
assert dict(job) == {
    'state': 'paused',
    'operator_paused': 1,
    'source_kind': 'highlight',
}
```

Assert the derived session has exactly one ready part pointing to the clipped video/XML, while the original recording session and parts remain unchanged.

Repeat without a saved room policy and assert the transient built-in default
creates the paused draft but does not insert a row into
`room_upload_policies`.

- [ ] **Step 2: Add an explicit coordinator entry point**

Refactor candidate loading so normal `create_ready_jobs()` and the new method share snapshot creation, file identity validation and part insertion. Use:

```python
async def create_highlight_job(self, session_id: int) -> int:
    candidate = await self._candidate_for_session(
        session_id,
        required_source_kind='highlight',
    )
    return await self._create_candidate(
        candidate,
        initial_state='paused',
        operator_paused=True,
        operator_resume_state='ready',
    )
```

Do not let the normal auto-job scan pick `source_kind='highlight'`; it only scans `source_kind='live'`.

Add `default_room_upload_policy()` to `policies.py` with the same templates,
primary-account mode, TID 21、转载声明、动态、自动评论、弹幕回灌和五天保留设置
as the existing Angular `DEFAULT_DRAFT`. This is a transient fallback for a
highlight draft when the room has no saved policy; it must not persist or enable
automatic full-session upload for that room.

- [ ] **Step 3: Create the derived media session transactionally**

For a ready clip, insert:

- a closed `recording_sessions` row with `source_kind='highlight'`, a unique key `highlight:<clip-id>`, copied room/anchor/area/cover metadata, and `upload_intent='upload'`;
- one finished `recording_runs` row;
- one ready `recording_parts` row using the clip video as both source/final path and clipped XML;
- `highlight_clips.upload_session_id`.

If a prior call already created the session/job, return the existing job ID. Stop the upload worker under `_session_action_lock`, create the paused job, then restart the worker.

- [ ] **Step 4: Make deletion source-aware**

When deleting a `source_kind='highlight'` job, reuse existing child cleanup but remove only the derived session's output video/XML and the linked clip row. Never traverse `highlight_clip_sources` to delete original recordings. Keep existing live-session deletion unchanged.

- [ ] **Step 5: Expose the source kind in journal responses**

Extend `RecordingSession` with:

```python
source_kind: str = 'live'
highlight_clip_id: Optional[int] = None
```

List both live and highlight sessions in the upload-task query. Join `highlight_clips` by `upload_session_id` to populate the clip ID. Keep recording-only automation and reconciliation queries restricted to `source_kind='live'`.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/bili_upload/test_upload.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_journal.py -q`

Expected: PASS, including existing live-session upload tests.

- [ ] **Step 7: Commit**

```bash
git add src/blrec/bili_upload/highlights.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/journal.py src/blrec/bili_upload/runtime.py src/blrec/bili_upload/policies.py tests/bili_upload/test_upload.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_journal.py
git commit -m "feat: create upload tasks from highlight clips"
```

---

### Task 7: Add authenticated highlight APIs and realtime progress

**Files:**
- Create: `src/blrec/web/routers/highlights.py`
- Modify: `src/blrec/web/routers/__init__.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/realtime.py`
- Create: `tests/web/test_highlights_routes.py`
- Modify: `tests/web/test_realtime_routes.py`

**Interfaces:**
- Produces REST endpoints under `/api/v1/highlights` and `/api/v1/highlights/sessions/{session_id}/timeline`.
- Produces SSE event `highlight_progress` with a `clips` array of progress records.
- Consumes: `HighlightService`, `HighlightWorker`, `BiliAccountRuntime.create_highlight_upload_task()`.

- [ ] **Step 1: Write failing route tests**

Cover authenticated calls:

```text
POST   /api/v1/highlights
PATCH  /api/v1/highlights/{marker_id}
DELETE /api/v1/highlights/{marker_id}
GET    /api/v1/highlights/sessions/{session_id}/timeline
POST   /api/v1/highlights/sessions/{session_id}/clips/inspect
POST   /api/v1/highlights/sessions/{session_id}/clips
GET    /api/v1/highlights/clips/{clip_id}
DELETE /api/v1/highlights/clips/{clip_id}
POST   /api/v1/highlights/clips/{clip_id}/upload-task
```

Assert marker creation works without a recording FK, clip creation rejects an unsafe tail with 409, and deleting a marker returns 204 without deleting a linked clip.

- [ ] **Step 2: Define exact request models**

```python
class CreateMarkerRequest(ApiModel):
    room_id: int = Field(..., gt=0)
    observed_at_ms: int = Field(..., gt=0)
    player_delay_ms: int = Field(0, ge=0, le=300_000)
    title: str = Field('', max_length=200)
    anchor_name: str = Field('', max_length=100)
    source: Literal['web', 'browser_extension'] = 'web'

class CreateClipRequest(ApiModel):
    marker_id: Optional[int] = Field(None, gt=0)
    name: str = Field(..., min_length=1, max_length=200)
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., gt=0)
    confirm_keyframe: bool = False
```

`inspect` returns requested/actual bounds, extra lead, compatibility and
confirmation requirement without creating files. For both inspect and create,
the router obtains active part durations from the existing active-recording
metadata provider and passes them to `HighlightService`; `create clip` repeats
all validation and does not trust the inspection response.

- [ ] **Step 3: Implement router dependency wiring**

Follow existing module-global dependency style. Set `highlights.service`, `highlights.worker`, and `highlights.upload_task_creator` during FastAPI startup and reset them to `None` during shutdown. All management routes depend on `authenticated_manager_subject`.

- [ ] **Step 4: Publish progress through the existing SSE sampler**

Extend `RealtimeSampler` with an async `highlight_provider`. Publish only when the JSON snapshot changes:

```python
highlights = await self._highlight_provider()
await self._publish_changed('highlight_progress', {'clips': highlights})
```

Do not create a second EventSource URL.

- [ ] **Step 5: Run route and realtime tests**

Run: `python -m pytest tests/web/test_highlights_routes.py tests/web/test_realtime_routes.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/blrec/web/routers/highlights.py src/blrec/web/routers/__init__.py src/blrec/web/main.py src/blrec/web/realtime.py tests/web/test_highlights_routes.py tests/web/test_realtime_routes.py
git commit -m "feat: expose highlight editing APIs"
```

---

### Task 8: Build the Angular highlight editor and growing timeline

**Files:**
- Create: `webapp/src/app/upload-tasks/shared/highlight.model.ts`
- Create: `webapp/src/app/upload-tasks/shared/highlight.service.ts`
- Create: `webapp/src/app/upload-tasks/shared/highlight.service.spec.ts`
- Create: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`
- Create: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.html`
- Create: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.scss`
- Create: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks-routing.module.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Modify: `webapp/src/app/core/services/realtime.service.ts`

**Interfaces:**
- Consumes: timeline/inspect/create APIs and existing `RecordingSessionService.createMediaAccess()`.
- Produces route: `/upload-tasks/highlights/:sessionId`.

- [ ] **Step 1: Write failing service tests**

Assert exact camelCase URLs and bodies for `getTimeline`, `inspectClip`, `createClip`, `getClip`, `deleteClip`, and `createUploadTask`. Verify `highlight_progress` is added to `EVENT_TYPES` without creating another EventSource.

- [ ] **Step 2: Define frontend models and service**

Use explicit models:

```typescript
export interface HighlightMarker {
  readonly id: number;
  readonly roomId: number;
  readonly contentAtMs: number;
  readonly name: string;
  readonly note: string;
}

export interface HighlightTimelinePart {
  readonly partId: number;
  readonly partIndex: number;
  readonly timelineStartMs: number;
  readonly durationMs: number;
  readonly stableEndMs: number;
  readonly recording: boolean;
}

export interface HighlightClipInspection {
  readonly requestedStartMs: number;
  readonly requestedEndMs: number;
  readonly actualStartMs: number;
  readonly actualEndMs: number;
  readonly extraLeadMs: number;
  readonly confirmationRequired: boolean;
}
```

- [ ] **Step 3: Write failing component interaction tests**

Cover marker click seeking, marker rename/note/delete, start/end handle validation, disabled submit inside the safe tail, display of both selected and actual ranges, confirmation UI above 10 seconds, SSE progress update, player cleanup, and switching media access when crossing a P boundary.

- [ ] **Step 4: Implement the editor page**

Use the existing `PartPlayerFactory` for FLV and native `<video>` for MP4. Keep one active player instance. The timeline is a semantic button/slider surface with keyboard-accessible start/end inputs; marker buttons set the preview part and seek to their mapped local offset. Display gaps and the final 10-second region visually, but do not display markers that the backend did not map.

Provide a compact marker list beside the timeline for rename, note and delete; these actions only mutate the bookmark. On `highlight_progress`, update only the matching clip cards. On `resync`, reload the timeline and clips once. Refresh a growing part's media snapshot only when the user crosses its old snapshot end or explicitly resumes preview; do not continuously reopen the media source.

- [ ] **Step 5: Run focused frontend tests**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/highlight.service.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/src/app/upload-tasks/shared/highlight.model.ts webapp/src/app/upload-tasks/shared/highlight.service.ts webapp/src/app/upload-tasks/shared/highlight.service.spec.ts webapp/src/app/upload-tasks/highlight-editor webapp/src/app/upload-tasks/upload-tasks-routing.module.ts webapp/src/app/upload-tasks/upload-tasks.module.ts webapp/src/app/core/services/realtime.service.ts
git commit -m "feat: add highlight clipping editor"
```

---

### Task 9: Integrate clip preview, upload settings, and task-list presentation

**Files:**
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.scss`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.*`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `tests/web/test_recording_sessions_routes.py`

**Interfaces:**
- Produces a “剪辑” action for local video sessions and a “高光” badge for derived upload tasks.
- Reuses `TaskEditDialogComponent` after a paused draft upload task is created.

- [ ] **Step 1: Expose source metadata through the existing task API**

Add to `RecordingSessionResponse` and `_session_response()`:

```python
source_kind: str
highlight_clip_id: Optional[int]
```

Add matching TypeScript fields. Test that normal rows return `sourceKind: 'live'` and derived rows return `sourceKind: 'highlight'` with a clip ID.

- [ ] **Step 2: Add the task-list entry point**

In each live session row/drawer that has local media, add one concise “剪辑” action linking to `/upload-tasks/highlights/{session.id}`. Do not put danmaku inside the playback action. For `sourceKind === 'highlight'`, show a “高光” tag beside the title and keep all existing upload status/actions.

- [ ] **Step 3: Complete the post-cut workflow**

When a clip becomes ready, show its local preview and “创建上传任务”. The endpoint creates an operator-paused draft and returns `{sessionId, jobId}`. Immediately open the existing task edit dialog for that job. After the user saves, call `resume_upload`; if the dialog is closed, leave the draft paused so it can be edited or deleted later from the upload list.

- [ ] **Step 4: Verify collection reuse**

Use the existing task-edit account and collection APIs. Test that switching the account clears an incompatible collection, refreshing loads that account's cached catalog, and selecting an existing collection is preserved in the task snapshot before resume.

- [ ] **Step 5: Run focused tests**

Run backend: `python -m pytest tests/web/test_recording_sessions_routes.py -q`

Run frontend: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/blrec/web/routers/recording_sessions.py tests/web/test_recording_sessions_routes.py webapp/src/app/upload-tasks/recording-sessions webapp/src/app/upload-tasks/shared/recording-session.model.ts webapp/src/app/upload-tasks/highlight-editor
git commit -m "feat: connect highlight clips to upload tasks"
```

---

### Task 10: Run full verification and real-media smoke tests

**Files:**
- Create: `tests/fixtures/highlights/README.md`
- Modify: `.github/workflows/test.yml` only if FFmpeg is unavailable in the current runner.

**Interfaces:**
- Consumes all prior tasks.
- Produces a reproducible acceptance record through commands and audit logs; no new feature behavior.

- [ ] **Step 1: Generate a disposable FFmpeg fixture outside git**

Run:

```bash
mkdir -p /tmp/blrec-highlight-fixture
ffmpeg -hide_banner -f lavfi -i testsrc2=size=640x360:rate=30 \
  -f lavfi -i sine=frequency=1000 -t 40 -c:v libx264 -g 60 \
  -c:a aac -y /tmp/blrec-highlight-fixture/source.mp4
```

Expected: a 40-second H.264/AAC file with two-second GOPs.

- [ ] **Step 2: Exercise the real cutter and validate stream copy**

Run the focused integration test with `BLREC_HIGHLIGHT_FIXTURE=/tmp/blrec-highlight-fixture/source.mp4` and assert with FFprobe that codec names match the source, output duration matches the inspected range, and FFmpeg did not invoke an encoder.

Run: `BLREC_HIGHLIGHT_FIXTURE=/tmp/blrec-highlight-fixture/source.mp4 python -m pytest tests/bili_upload/test_highlight_cut.py -k real_ffmpeg -q`

Expected: PASS.

- [ ] **Step 3: Run backend quality gates**

```bash
python -m pytest
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
```

Expected: all commands exit 0.

- [ ] **Step 4: Run frontend quality gates**

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless
npx ng lint
npm run build
```

Expected: all commands exit 0 and the production bundle is generated.

- [ ] **Step 5: Perform local browser acceptance**

Start the backend and Angular app, use a currently recording H.264/AAC FLV, create two web highlighters, open the editor, verify only mapped markers appear, cut a range ending at least 10 seconds behind live, preview the output, create a draft upload task, select an existing account collection, save and resume. Confirm the original recording continues growing and audit logs include marker delay, requested/actual range, source parts, output size and danmaku count.

- [ ] **Step 6: Commit the fixture documentation**

```bash
git add tests/fixtures/highlights/README.md .github/workflows/test.yml
git commit -m "test: verify highlight clipping workflow"
```

If `.github/workflows/test.yml` required no change, omit it from `git add`.
