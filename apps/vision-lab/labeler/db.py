"""虚荣视觉标注工作台 —— SQLite 交互工作库。

权威数据以 JSONL 导出(可版本化);本库只服务标注工作流。
分层标注体系见 README:content_family → game_context → screen_type,
辅助字段 game_mode / match_kind / view_context / quality_flags / black_bars / ocr_usable。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

_SCHEMA = """
-- 训练目标(结算检测/游戏状态/模式/窗口/同局判断,后续可新增)
CREATE TABLE IF NOT EXISTS annotation_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- 视频(只读来源,NAS 路径)
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_path TEXT NOT NULL UNIQUE,
    streamer TEXT NOT NULL DEFAULT '',
    room_id TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    bvid TEXT NOT NULL DEFAULT '',
    part_index INTEGER,
    part_total INTEGER,
    duration_seconds REAL DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | extracting | done | failed
    extracted_at TEXT,
    error TEXT DEFAULT ''
);

-- 抽帧任务(策略配方)
CREATE TABLE IF NOT EXISTS extraction_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,                 -- 见 extract.py STRATEGIES
    params TEXT NOT NULL DEFAULT '{}',      -- JSON
    status TEXT NOT NULL DEFAULT 'running', -- running | done | failed
    created_at TEXT,
    error TEXT DEFAULT ''
);

-- 帧:原始分辨率 + 真实 PTS + 哈希
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    timestamp_ms INTEGER NOT NULL,          -- 视频内真实 PTS(毫秒)
    part_index INTEGER,                     -- 分批视频的 part(单文件为 1)
    part_offset_ms INTEGER,                 -- part 内时间
    session_offset_ms INTEGER,              -- 整场直播时间偏移
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    phash TEXT NOT NULL DEFAULT '',
    frame_path TEXT NOT NULL,               -- 原始分辨率帧(永久保留)
    thumb_path TEXT NOT NULL DEFAULT '',
    event_id INTEGER REFERENCES events(id),
    strategy TEXT NOT NULL DEFAULT '',      -- 来源抽帧策略
    model_source TEXT DEFAULT '',           -- 生成样本的模型版本
    model_confidence REAL,                  -- 模型置信度
    is_representative INTEGER NOT NULL DEFAULT 0,
    labeled INTEGER NOT NULL DEFAULT 0,
    UNIQUE (video_id, timestamp_ms)
);
CREATE INDEX IF NOT EXISTS idx_frames_event ON frames (event_id);
CREATE INDEX IF NOT EXISTS idx_frames_video ON frames (video_id);
CREATE INDEX IF NOT EXISTS idx_frames_labeled ON frames (labeled, id);

-- 事件(独立片段,如一次结算界面出现)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'candidate', -- candidate | manual | other
    notes TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_video ON events (video_id);

-- 标注:一帧一条权威分层标签(不设相互冲突的冗余布尔字段)
CREATE TABLE IF NOT EXISTS annotations (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    content_family TEXT,                    -- vainglory | not_vainglory | uncertain
    non_vainglory_type TEXT,                -- 仅 not_vainglory
    game_context TEXT,                      -- in_match | out_of_match | transition | unknown
    screen_type TEXT,                       -- 条件式具体界面(见 config.py)
    game_mode TEXT,                         -- 3v3 | 5v5 | aram | blitz | unknown
    match_kind TEXT NOT NULL DEFAULT 'unknown',  -- pvp | bot | practice | unknown
    view_context TEXT NOT NULL DEFAULT 'unknown',-- played | spectated | replay | unknown
    quality_flags TEXT NOT NULL DEFAULT '[]',    -- JSON 数组
    black_bars TEXT NOT NULL DEFAULT 'none',     -- none|top|bottom|left|right|multiple
    ocr_usable TEXT,                        -- yes | no | unknown(仅 result_page)
    annotation_status TEXT NOT NULL DEFAULT 'draft',  -- draft|complete|needs_review|ignored
    talent_mode INTEGER NOT NULL DEFAULT 0, -- 天赋模式独立开关(0/1)
    result_clarity TEXT,                    -- clear | translucent | unknown(仅结算页)
    result_occlusion TEXT,                  -- none | occluded | unknown(仅结算页)
    occluder_types TEXT NOT NULL DEFAULT '[]',  -- JSON 数组(遮挡物多选)
    notes TEXT DEFAULT '',
    label_version TEXT NOT NULL DEFAULT 'v1',
    updated_at TEXT
);

-- 边界框(相对原图归一化 0-1,导出时换算)
CREATE TABLE IF NOT EXISTS boxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    box_type TEXT NOT NULL,                 -- viewport | result_panel | scoreboard_panel | shop_panel
    x REAL NOT NULL, y REAL NOT NULL,
    w REAL NOT NULL, h REAL NOT NULL,
    UNIQUE (frame_id, box_type)
);

-- 主播级默认框(跨视频记忆;主播不换设备时画面坐标基本固定)
CREATE TABLE IF NOT EXISTS streamer_boxes (
    streamer TEXT NOT NULL,
    box_type TEXT NOT NULL,                 -- viewport | result_panel | scoreboard_panel | shop_panel
    x REAL NOT NULL, y REAL NOT NULL,
    w REAL NOT NULL, h REAL NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (streamer, box_type)
);

-- 同局判断(双图)
CREATE TABLE IF NOT EXISTS pair_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_a_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    frame_b_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    label TEXT NOT NULL,                    -- same_match | different_match | uncertain
    created_at TEXT,
    UNIQUE (frame_a_id, frame_b_id)
);

-- 模型预测(多模型版本)
CREATE TABLE IF NOT EXISTS model_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    pred_type TEXT NOT NULL,
    confidence REAL,
    bbox TEXT
);

-- 数据集版本(不可变快照)
CREATE TABLE IF NOT EXISTS dataset_versions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    filter_json TEXT NOT NULL DEFAULT '{}',
    counts_json TEXT NOT NULL DEFAULT '{}',
    manifest_path TEXT NOT NULL,
    git_commit TEXT DEFAULT ''
);

-- 审计/撤销日志
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER,
    event_id INTEGER,
    action TEXT NOT NULL,                   -- label | box | event_merge | event_split | propagate | undo | pair
    detail TEXT DEFAULT '',
    created_at TEXT
);

-- 实时打标进度(单用户,单行状态)
CREATE TABLE IF NOT EXISTS live_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    queue_json TEXT NOT NULL DEFAULT '[]',  -- 实时打标视频队列 [video_id,...]
    queue_index INTEGER NOT NULL DEFAULT 0, -- 当前视频在队列中的位置
    video_id INTEGER,                       -- 当前视频
    last_pts_ms INTEGER,                    -- 最后打标到的视频内位置
    last_frame_id INTEGER,
    updated_at TEXT
);

-- 每个视频的实时打标进度
CREATE TABLE IF NOT EXISTS live_video_progress (
    video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    last_pts_ms INTEGER,
    last_frame_id INTEGER,
    updated_at TEXT
);

-- 3V3 / 大乱斗光栅专项。与通用 annotations/boxes 完全隔离，避免专项打标
-- 改写一帧原有的界面、模式或完成状态。
CREATE TABLE IF NOT EXISTS mode_gate_rounds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mode_gate_round_videos (
    round_id TEXT NOT NULL REFERENCES mode_gate_rounds(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    expected_mode TEXT NOT NULL,             -- aram | 3v3（只作选片提示）
    start_ms INTEGER NOT NULL DEFAULT 0,      -- 人工筛过的建议起点
    sort_order INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    last_pts_ms INTEGER,
    last_frame_id INTEGER REFERENCES frames(id),
    updated_at TEXT,
    PRIMARY KEY (round_id, video_id)
);

CREATE TABLE IF NOT EXISTS mode_gate_annotations (
    round_id TEXT NOT NULL REFERENCES mode_gate_rounds(id) ON DELETE CASCADE,
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    evidence TEXT NOT NULL,                  -- blocked_gate | open_entrance | no_evidence
    x REAL, y REAL, w REAL, h REAL,          -- 兼容旧库：镜像第一个框
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (round_id, frame_id)
);
CREATE INDEX IF NOT EXISTS idx_mode_gate_annotations_round
    ON mode_gate_annotations (round_id, frame_id);

-- 一张画面可能同时出现多个光栅/开放入口，框单独存成一对多记录。
CREATE TABLE IF NOT EXISTS mode_gate_boxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    frame_id INTEGER NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    x REAL NOT NULL, y REAL NOT NULL,
    w REAL NOT NULL, h REAL NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (round_id, frame_id)
        REFERENCES mode_gate_annotations(round_id, frame_id) ON DELETE CASCADE,
    UNIQUE (round_id, frame_id, sort_order)
);
CREATE INDEX IF NOT EXISTS idx_mode_gate_boxes_frame
    ON mode_gate_boxes (round_id, frame_id, sort_order);

-- BP 主动学习复核队列。模型建议与人工确认分开保存；人工确认不会改写通用
-- annotations，避免“不是 BP”这种专项负样本被迫填写错误的通用界面类型。
CREATE TABLE IF NOT EXISTS bp_review_items (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    suggested_label TEXT NOT NULL,           -- bp_3v3 | bp_aram | bp_5v5 | not_bp
    suggestion_confidence REAL NOT NULL,
    stage_class TEXT NOT NULL,
    stage_confidence REAL NOT NULL,
    pre_match_confidence REAL NOT NULL,
    mode_class TEXT NOT NULL,
    mode_confidence REAL NOT NULL,
    mode_margin REAL NOT NULL,
    selection_reason TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 0,
    raw_prediction TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'pending', -- pending | confirmed | skipped
    confirmed_label TEXT,                    -- 同 suggested_label；skipped 时为空
    -- clear | occluded | windowed | occluded_windowed | unreadable
    visual_condition TEXT NOT NULL DEFAULT 'clear',
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bp_review_status
    ON bp_review_items (review_status, priority DESC, frame_id);

-- 结算页 / 计分板主动学习复核。三分类放在同一个队列里，专门收集二者
-- 容易混淆的画面及 hard negative；与通用标注隔离，人工确认优先于模型预标。
CREATE TABLE IF NOT EXISTS key_screen_review_items (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    suggested_label TEXT NOT NULL,           -- result_page | scoreboard | other
    suggestion_confidence REAL NOT NULL,
    selection_reason TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 0,
    raw_prediction TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'pending', -- pending | confirmed | skipped
    confirmed_label TEXT,
    visual_condition TEXT NOT NULL DEFAULT 'clear', -- clear | occluded | unreadable
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_key_screen_review_status
    ON key_screen_review_items (review_status, priority DESC, frame_id);

-- 每次训练都绑定一个不可变 dataset_versions 快照。后续新增标注再训练时会
-- 新建数据集版本和训练记录，不覆盖旧模型或旧指标。
CREATE TABLE IF NOT EXISTS training_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    status TEXT NOT NULL,                    -- queued | running | succeeded | failed | cancelled | interrupted
    epochs INTEGER NOT NULL,
    current_epoch INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    config_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    published_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_runs_created
    ON training_runs (created_at DESC, id DESC);
"""

DEFAULT_TASKS = [
    ('result_detector', '结算界面检测(result_panel 边界框)', '检测赛后结算面板边界框'),
    ('game_state', '游戏画面与游戏状态识别(screen_type 分类)', '基于分层 screen_type 的画面分类'),
    ('game_mode', '游戏模式识别(3v3/5v5/aram/blitz)', '地图/玩法模式分类'),
    ('viewport', '游戏窗口/有效画面区域检测(viewport_bbox)', '直播画面中游戏窗口定位'),
    ('same_match', '同局判断(双图配对)', '两张 HUD 是否属于同一局'),
    ('mode_gate', '3V3/大乱斗光栅专项', '圈出大乱斗光栅或 3V3 同位置的开放入口'),
    ('bp_review', 'BP 模式主动学习复核', '模型预标选英雄画面，由人工确认或纠错'),
    ('key_screen_review', '结算页/计分板主动学习复核',
     '模型预标关键画面，由人工确认结算页、计分板或其他'),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.executemany(
        'INSERT OR IGNORE INTO annotation_tasks (id, name, description) '
        'VALUES (?, ?, ?)',
        DEFAULT_TASKS,
    )
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移:旧库补新列 + 旧枚举值转换。"""
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(annotations)')}
    if 'annotation_status' not in cols:
        conn.execute(
            "ALTER TABLE annotations ADD COLUMN annotation_status TEXT "
            "NOT NULL DEFAULT 'draft'")
    if 'talent_mode' not in cols:
        conn.execute(
            'ALTER TABLE annotations ADD COLUMN talent_mode INTEGER '
            "NOT NULL DEFAULT 0")
    for col, ddl in [
        ('result_clarity', 'TEXT'),
        ('result_occlusion', 'TEXT'),
        ('occluder_types', "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        if col not in cols:
            conn.execute(f'ALTER TABLE annotations ADD COLUMN {col} {ddl}')
    # 旧 screen_type 枚举 → 新枚举
    for old, new in config.SCREEN_TYPE_MIGRATION.items():
        conn.execute('UPDATE annotations SET screen_type = ? WHERE screen_type = ?',
                     (new, old))
    # 旧标注记录(以前保存过完整标注)→ 迁移为 complete
    conn.execute(
        "UPDATE annotations SET annotation_status = 'complete' "
        "WHERE annotation_status = 'draft' AND content_family IS NOT NULL "
        "AND (content_family != 'vainglory' "
        "     OR (game_context IS NOT NULL AND screen_type IS NOT NULL))")
    # 旧的 game_context 语义不变(in_match 等),列名沿用
    # 光栅专项旧版一帧只有一个 x/y/w/h；首次打开新版时迁到多框子表。
    conn.execute(
        """
        INSERT INTO mode_gate_boxes
            (round_id, frame_id, sort_order, x, y, w, h, updated_at)
        SELECT a.round_id, a.frame_id, 0, a.x, a.y, a.w, a.h, a.updated_at
        FROM mode_gate_annotations a
        WHERE a.evidence != 'no_evidence'
          AND a.x IS NOT NULL AND a.y IS NOT NULL
          AND a.w IS NOT NULL AND a.h IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM mode_gate_boxes b
              WHERE b.round_id = a.round_id AND b.frame_id = a.frame_id
          )
        """
    )
    bp_cols = {
        row['name'] for row in conn.execute('PRAGMA table_info(bp_review_items)')
    }
    if 'visual_condition' not in bp_cols:
        conn.execute(
            "ALTER TABLE bp_review_items ADD COLUMN visual_condition TEXT "
            "NOT NULL DEFAULT 'clear'"
        )


def now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def audit(conn: sqlite3.Connection, action: str, *, frame_id: int = None,
          event_id: int = None, detail: str = '') -> None:
    conn.execute(
        'INSERT INTO audit_log (frame_id, event_id, action, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (frame_id, event_id, action, detail, now()),
    )
    conn.commit()


# ---------- 视频 ----------

def upsert_video(conn: sqlite3.Connection, *, remote_path: str, streamer: str,
                 room_id: str, filename: str, duration_seconds: float,
                 size_bytes: int, bvid: str = '', part_index: int = None,
                 part_total: int = None) -> int:
    conn.execute(
        """
        INSERT INTO videos (remote_path, streamer, room_id, filename, bvid,
                            part_index, part_total, duration_seconds, size_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(remote_path) DO UPDATE SET
            streamer=excluded.streamer, room_id=excluded.room_id,
            filename=excluded.filename, bvid=excluded.bvid,
            duration_seconds=excluded.duration_seconds,
            size_bytes=excluded.size_bytes
        """,
        (remote_path, streamer, room_id, filename, bvid, part_index, part_total,
         duration_seconds, size_bytes),
    )
    conn.commit()
    row = conn.execute(
        'SELECT id FROM videos WHERE remote_path = ?', (remote_path,)
    ).fetchone()
    return int(row['id'])


def list_videos(conn: sqlite3.Connection, *, status: Optional[str] = None,
                streamer: Optional[str] = None, room_id: Optional[str] = None,
                bvid: Optional[str] = None,
                min_size_bytes: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = ('SELECT v.*, '
           '(SELECT COUNT(*) FROM frames f WHERE f.video_id = v.id) AS frame_count, '
           '(SELECT COUNT(*) FROM frames f WHERE f.video_id = v.id AND f.labeled = 1) AS labeled_count '
           'FROM videos v')
    where: List[str] = ["v.remote_path NOT LIKE 'worker-candidate://%'"]
    args: List[Any] = []
    if status:
        where.append('v.status = ?')
        args.append(status)
    if streamer:
        where.append('v.streamer LIKE ?')
        args.append(f'%{streamer}%')
    if room_id:
        where.append('v.room_id = ?')
        args.append(room_id)
    if bvid:
        where.append('v.bvid = ?')
        args.append(bvid)
    if min_size_bytes:
        where.append('v.size_bytes >= ?')
        args.append(min_size_bytes)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY v.streamer, v.filename'
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def set_video_status(conn: sqlite3.Connection, video_id: int, status: str,
                     error: str = '') -> None:
    conn.execute(
        'UPDATE videos SET status = ?, error = ?, extracted_at = ? WHERE id = ?',
        (status, error, now() if status in ('done', 'failed') else None, video_id),
    )
    conn.commit()


# ---------- 帧 ----------

def add_frames(conn: sqlite3.Connection, video_id: int,
               entries: List[Dict[str, Any]]) -> List[int]:
    """批量插入帧(sha256 去重)。返回新插入的帧 id 列表。"""
    ids: List[int] = []
    defaults = {'part_index': None, 'part_offset_ms': None,
                'session_offset_ms': None}
    for e in entries:
        data = {**defaults, **e, 'video_id': video_id}
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO frames
                (video_id, timestamp_ms, part_index, part_offset_ms,
                 session_offset_ms, width, height, sha256, phash, frame_path,
                 thumb_path, strategy, model_source, model_confidence)
            VALUES (:video_id, :timestamp_ms, :part_index, :part_offset_ms,
                    :session_offset_ms, :width, :height, :sha256, :phash,
                    :frame_path, :thumb_path, :strategy, :model_source,
                    :model_confidence)
            """,
            data,
        )
        if cur.rowcount:
            ids.append(int(conn.execute('SELECT last_insert_rowid()').fetchone()[0]))
    conn.commit()
    return ids


def get_frame(conn: sqlite3.Connection, frame_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, v.filename '
        'FROM frames f JOIN videos v ON v.id = f.video_id WHERE f.id = ?',
        (frame_id,),
    ).fetchone()
    return dict(row) if row else None


def query_frames(conn: sqlite3.Connection, *, video_id: Optional[int] = None,
                 event_id: Optional[int] = None, labeled: Optional[int] = None,
                 status: Optional[str] = None,
                 screen_type: Optional[str] = None, strategy: Optional[str] = None,
                 representative_only: bool = False, limit: int = 200,
                 offset: int = 0) -> List[Dict[str, Any]]:
    sql = ('SELECT f.*, v.streamer, v.remote_path, v.filename, a.content_family, '
           'a.game_context, a.screen_type, a.game_mode, a.match_kind, '
           'a.view_context, a.quality_flags, a.black_bars, a.ocr_usable, '
           'a.annotation_status, a.notes '
           'FROM frames f JOIN videos v ON v.id = f.video_id '
           'LEFT JOIN annotations a ON a.frame_id = f.id')
    where: List[str] = []
    args: List[Any] = []
    if video_id:
        where.append('f.video_id = ?')
        args.append(video_id)
    if event_id:
        where.append('f.event_id = ?')
        args.append(event_id)
    if labeled is not None:
        where.append('f.labeled = ?')
        args.append(labeled)
    if status:
        where.append('a.annotation_status = ?')
        args.append(status)
    if screen_type:
        where.append('a.screen_type = ?')
        args.append(screen_type)
    if strategy:
        where.append('f.strategy = ?')
        args.append(strategy)
    if representative_only:
        where.append('f.is_representative = 1')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY f.video_id, f.timestamp_ms LIMIT ? OFFSET ?'
    args += [limit, offset]
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------- 事件 ----------

def create_event(conn: sqlite3.Connection, video_id: int, start_ms: int,
                 end_ms: int, kind: str = 'candidate', notes: str = '') -> int:
    cur = conn.execute(
        'INSERT INTO events (video_id, start_ms, end_ms, kind, notes, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (video_id, start_ms, end_ms, kind, notes, now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def event_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        'SELECT e.id, e.video_id, e.start_ms, e.end_ms, e.kind, '
        'COUNT(f.id) AS frame_count '
        'FROM events e LEFT JOIN frames f ON f.event_id = e.id '
        'GROUP BY e.id ORDER BY e.id'
    ).fetchall()
    return [dict(r) for r in rows]


def assign_event(conn: sqlite3.Connection, frame_ids: List[int],
                 event_id: int) -> None:
    for fid in frame_ids:
        conn.execute('UPDATE frames SET event_id = ? WHERE id = ?', (event_id, fid))
    conn.commit()


def merge_events(conn: sqlite3.Connection, event_ids: List[int]) -> int:
    """合并多个事件:时间范围取并集,帧归入最小 id 事件。返回保留的事件 id。"""
    keep = min(event_ids)
    evs = [dict(conn.execute('SELECT * FROM events WHERE id = ?', (eid,)).fetchone())
           for eid in event_ids]
    start = min(e['start_ms'] for e in evs)
    end = max(e['end_ms'] for e in evs)
    conn.execute('UPDATE events SET start_ms = ?, end_ms = ? WHERE id = ?',
                 (start, end, keep))
    for eid in event_ids:
        if eid != keep:
            conn.execute('UPDATE frames SET event_id = ? WHERE event_id = ?',
                         (keep, eid))
            conn.execute('DELETE FROM events WHERE id = ?', (eid,))
    conn.commit()
    return keep


def split_event(conn: sqlite3.Connection, event_id: int,
                split_at_ms: int) -> int:
    """把事件在 split_at_ms 处拆成两个,返回新事件 id。"""
    ev = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    if not ev:
        raise KeyError(event_id)
    # 原事件保留前半段
    conn.execute('UPDATE events SET end_ms = ? WHERE id = ?',
                 (split_at_ms, event_id))
    new_id = create_event(conn, ev['video_id'], split_at_ms, ev['end_ms'],
                          ev['kind'], ev['notes'])
    conn.execute('UPDATE frames SET event_id = ? WHERE event_id = ? AND timestamp_ms >= ?',
                 (new_id, event_id, split_at_ms))
    conn.commit()
    return new_id


# ---------- 标注 ----------

def save_annotation(conn: sqlite3.Connection, frame_id: int,
                    values: Dict[str, Any], *, label_version: str = 'v1',
                    status: str = 'draft') -> None:
    if status not in config.ANNOTATION_STATUSES:
        status = 'draft'
    fields = {
        'content_family', 'non_vainglory_type', 'game_context', 'screen_type',
        'game_mode', 'match_kind', 'view_context', 'quality_flags',
        'black_bars', 'ocr_usable', 'result_clarity', 'result_occlusion',
        'occluder_types', 'notes',
    }
    data = {k: values.get(k) for k in fields}
    if not isinstance(data['quality_flags'], (list, tuple)):
        data['quality_flags'] = data['quality_flags'] or []
    if isinstance(data['quality_flags'], (list, tuple)):
        data['quality_flags'] = json.dumps(list(data['quality_flags']))
    if not isinstance(data['occluder_types'], (list, tuple)):
        data['occluder_types'] = data['occluder_types'] or []
    if isinstance(data['occluder_types'], (list, tuple)):
        data['occluder_types'] = json.dumps(list(data['occluder_types']))
    data['match_kind'] = data['match_kind'] or 'unknown'
    data['view_context'] = data['view_context'] or 'unknown'
    data['black_bars'] = data['black_bars'] or 'none'
    data['frame_id'] = frame_id
    data['label_version'] = label_version
    data['updated_at'] = now()
    data['annotation_status'] = status
    data['talent_mode'] = 1 if values.get('talent_mode') else 0
    conn.execute(
        """
        INSERT INTO annotations (frame_id, content_family, non_vainglory_type,
            game_context, screen_type, game_mode, match_kind, view_context,
            quality_flags, black_bars, ocr_usable, result_clarity,
            result_occlusion, occluder_types, notes, label_version,
            annotation_status, talent_mode, updated_at)
        VALUES (:frame_id, :content_family, :non_vainglory_type, :game_context,
            :screen_type, :game_mode, :match_kind, :view_context, :quality_flags,
            :black_bars, :ocr_usable, :result_clarity, :result_occlusion,
            :occluder_types, :notes, :label_version,
            :annotation_status, :talent_mode, :updated_at)
        ON CONFLICT(frame_id) DO UPDATE SET
            content_family=excluded.content_family,
            non_vainglory_type=excluded.non_vainglory_type,
            game_context=excluded.game_context,
            screen_type=excluded.screen_type,
            game_mode=excluded.game_mode,
            match_kind=excluded.match_kind,
            view_context=excluded.view_context,
            quality_flags=excluded.quality_flags,
            black_bars=excluded.black_bars,
            ocr_usable=excluded.ocr_usable,
            result_clarity=excluded.result_clarity,
            result_occlusion=excluded.result_occlusion,
            occluder_types=excluded.occluder_types,
            notes=excluded.notes,
            label_version=excluded.label_version,
            annotation_status=excluded.annotation_status,
            talent_mode=excluded.talent_mode,
            updated_at=excluded.updated_at
        """,
        data,
    )
    labeled = 1 if status == 'complete' else 0
    conn.execute('UPDATE frames SET labeled = ? WHERE id = ?', (labeled, frame_id))
    conn.commit()


def get_annotation(conn: sqlite3.Connection, frame_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM annotations WHERE frame_id = ?', (frame_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d['quality_flags'] = json.loads(d['quality_flags'] or '[]')
    d['occluder_types'] = json.loads(d['occluder_types'] or '[]')
    return d


# ---------- 边界框 ----------

def save_box(conn: sqlite3.Connection, frame_id: int, box_type: str,
             x: float, y: float, w: float, h: float) -> None:
    conn.execute(
        """
        INSERT INTO boxes (frame_id, box_type, x, y, w, h) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(frame_id, box_type) DO UPDATE SET
            x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h
        """,
        (frame_id, box_type, x, y, w, h),
    )
    # 同步主播级默认框(跨视频记忆)
    row = conn.execute(
        'SELECT v.streamer FROM frames f JOIN videos v ON v.id = f.video_id '
        'WHERE f.id = ?', (frame_id,)).fetchone()
    if row and row['streamer']:
        conn.execute(
            """
            INSERT INTO streamer_boxes (streamer, box_type, x, y, w, h, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(streamer, box_type) DO UPDATE SET
                x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h,
                updated_at=excluded.updated_at
            """,
            (row['streamer'], box_type, x, y, w, h, now()),
        )
    conn.commit()


def delete_box(conn: sqlite3.Connection, frame_id: int, box_type: str) -> None:
    conn.execute('DELETE FROM boxes WHERE frame_id = ? AND box_type = ?',
                 (frame_id, box_type))
    conn.commit()


def get_boxes(conn: sqlite3.Connection, frame_id: int) -> Dict[str, Dict[str, float]]:
    rows = conn.execute(
        'SELECT box_type, x, y, w, h FROM boxes WHERE frame_id = ?', (frame_id,)
    ).fetchall()
    return {r['box_type']: dict(r) for r in rows}


# ---------- 同局配对 ----------

def save_pair(conn: sqlite3.Connection, frame_a_id: int, frame_b_id: int,
              label: str) -> None:
    a, b = sorted((frame_a_id, frame_b_id))
    conn.execute(
        """
        INSERT INTO pair_annotations (frame_a_id, frame_b_id, label, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(frame_a_id, frame_b_id) DO UPDATE SET label=excluded.label
        """,
        (a, b, label, now()),
    )
    conn.commit()


# ---------- 模型预测 ----------

def add_prediction(conn: sqlite3.Connection, frame_id: int, *,
                   model_version: str, pred_type: str, confidence: float,
                   bbox: Optional[Dict[str, float]] = None) -> None:
    conn.execute(
        'INSERT INTO model_predictions (frame_id, model_version, pred_type, '
        'confidence, bbox) VALUES (?, ?, ?, ?, ?)',
        (frame_id, model_version, pred_type, confidence,
         json.dumps(bbox) if bbox else None),
    )
    conn.commit()


# ---------- 数据集版本 ----------

def create_dataset_version(conn: sqlite3.Connection, *, version_id: str,
                           task_id: str, filter_json: Dict[str, Any],
                           counts: Dict[str, Any], manifest_path: str,
                           git_commit: str = '') -> None:
    conn.execute(
        'INSERT INTO dataset_versions (id, task_id, created_at, filter_json, '
        'counts_json, manifest_path, git_commit) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (version_id, task_id, now(), json.dumps(filter_json, ensure_ascii=False),
         json.dumps(counts, ensure_ascii=False), str(manifest_path), git_commit),
    )
    conn.commit()


def list_dataset_versions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute('SELECT * FROM dataset_versions ORDER BY created_at DESC').fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['filter_json'] = json.loads(d['filter_json'])
        d['counts_json'] = json.loads(d['counts_json'])
        out.append(d)
    return out


# ---------- 训练记录 ----------

TRAINING_RUN_STATUSES = {
    'queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted',
}


def create_training_run(
        conn: sqlite3.Connection, *, run_id: str, task_id: str,
        dataset_version_id: str, epochs: int, config_json: Dict[str, Any],
        log_path: str) -> None:
    if not run_id.strip():
        raise ValueError('训练记录 id 不能为空')
    if epochs <= 0:
        raise ValueError('训练轮数必须为正数')
    dataset = conn.execute(
        'SELECT task_id FROM dataset_versions WHERE id = ?',
        (dataset_version_id,),
    ).fetchone()
    if dataset is None:
        raise KeyError(f'数据集版本不存在: {dataset_version_id}')
    if str(dataset['task_id']) != task_id:
        raise ValueError('训练任务与数据集任务不一致')
    conn.execute(
        """
        INSERT INTO training_runs
            (id, task_id, dataset_version_id, status, epochs,
             config_json, log_path, created_at)
        VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
        """,
        (
            run_id,
            task_id,
            dataset_version_id,
            int(epochs),
            json.dumps(config_json, ensure_ascii=False),
            log_path,
            now(),
        ),
    )
    conn.commit()


def update_training_run(
        conn: sqlite3.Connection, run_id: str, **updates: Any) -> None:
    allowed = {
        'status', 'current_epoch', 'progress', 'metrics', 'artifact_path',
        'error', 'published_path', 'started_at', 'finished_at',
    }
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(
            '未知训练记录字段: {}'.format(', '.join(sorted(unknown))))
    if not updates:
        return
    values: Dict[str, Any] = dict(updates)
    if 'status' in values and values['status'] not in TRAINING_RUN_STATUSES:
        raise ValueError(f'未知训练状态: {values["status"]}')
    if 'current_epoch' in values:
        values['current_epoch'] = max(0, int(values['current_epoch']))
    if 'progress' in values:
        progress = float(values['progress'])
        if not 0 <= progress <= 1:
            raise ValueError('训练进度必须在 0 到 1 之间')
        values['progress'] = progress
    if 'metrics' in values:
        values['metrics_json'] = json.dumps(
            values.pop('metrics') or {}, ensure_ascii=False)
    assignments = ', '.join(f'{field} = ?' for field in values)
    params = [values[field] for field in values]
    params.append(run_id)
    cursor = conn.execute(
        f'UPDATE training_runs SET {assignments} WHERE id = ?', params)
    if cursor.rowcount != 1:
        conn.rollback()
        raise KeyError(f'训练记录不存在: {run_id}')
    conn.commit()


def _training_run_dict(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result['metrics_json'] = json.loads(result['metrics_json'] or '{}')
    result['config_json'] = json.loads(result['config_json'] or '{}')
    return result


def get_training_run(
        conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM training_runs WHERE id = ?', (run_id,)).fetchone()
    return _training_run_dict(row) if row else None


def list_training_runs(
        conn: sqlite3.Connection, *, limit: int = 100) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM training_runs ORDER BY created_at DESC, id DESC LIMIT ?',
        (max(1, min(1_000, int(limit))),),
    ).fetchall()
    return [_training_run_dict(row) for row in rows]


def audit_recent(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- 实时打标进度 ----------

def save_live_state(conn: sqlite3.Connection, *, queue: List[int],
                    queue_index: int, video_id: Optional[int],
                    last_pts_ms: Optional[int],
                    last_frame_id: Optional[int]) -> None:
    conn.execute(
        """
        INSERT INTO live_state (id, queue_json, queue_index, video_id,
                                last_pts_ms, last_frame_id, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            queue_json=excluded.queue_json, queue_index=excluded.queue_index,
            video_id=excluded.video_id, last_pts_ms=excluded.last_pts_ms,
            last_frame_id=excluded.last_frame_id, updated_at=excluded.updated_at
        """,
        (json.dumps(queue), queue_index, video_id, last_pts_ms, last_frame_id,
         now()),
    )
    conn.commit()


def load_live_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute('SELECT * FROM live_state WHERE id = 1').fetchone()
    if not row:
        return {'queue': [], 'queue_index': 0, 'video_id': None,
                'last_pts_ms': None, 'last_frame_id': None}
    d = dict(row)
    d['queue'] = json.loads(d.pop('queue_json') or '[]')
    return d


# ---------- 每视频实时打标进度 ----------

def save_video_progress(conn: sqlite3.Connection, video_id: int, *,
                        last_pts_ms: Optional[int],
                        last_frame_id: Optional[int]) -> None:
    conn.execute(
        """
        INSERT INTO live_video_progress (video_id, last_pts_ms, last_frame_id,
                                         updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            last_pts_ms=excluded.last_pts_ms,
            last_frame_id=excluded.last_frame_id,
            updated_at=excluded.updated_at
        """,
        (video_id, last_pts_ms, last_frame_id, now()),
    )
    conn.commit()


def load_video_progress(conn: sqlite3.Connection,
                        video_id: int) -> Dict[str, Any]:
    row = conn.execute(
        'SELECT * FROM live_video_progress WHERE video_id = ?', (video_id,)
    ).fetchone()
    return dict(row) if row else {'video_id': video_id, 'last_pts_ms': None,
                                  'last_frame_id': None}


def all_video_progress(conn: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    rows = conn.execute('SELECT * FROM live_video_progress').fetchall()
    return {r['video_id']: dict(r) for r in rows}


# ---------- 3V3 / 大乱斗光栅专项 ----------

MODE_GATE_EVIDENCE = {'blocked_gate', 'open_entrance', 'no_evidence'}


def save_mode_gate_round(conn: sqlite3.Connection, *, round_id: str, name: str,
                         description: str = '', active: bool = True) -> None:
    if active:
        conn.execute('UPDATE mode_gate_rounds SET active = 0')
    conn.execute(
        """
        INSERT INTO mode_gate_rounds (id, name, description, active, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            description=excluded.description,
            active=excluded.active
        """,
        (round_id, name, description, 1 if active else 0, now()),
    )
    conn.commit()


def add_mode_gate_round_video(conn: sqlite3.Connection, *, round_id: str,
                              video_id: int, expected_mode: str,
                              start_ms: int = 0, sort_order: int = 0,
                              notes: str = '') -> None:
    if expected_mode not in {'aram', '3v3'}:
        raise ValueError('expected_mode 必须是 aram 或 3v3')
    if not conn.execute(
            'SELECT 1 FROM mode_gate_rounds WHERE id = ?', (round_id,)).fetchone():
        raise KeyError(f'轮次不存在: {round_id}')
    if not conn.execute('SELECT 1 FROM videos WHERE id = ?', (video_id,)).fetchone():
        raise KeyError(f'视频不存在: {video_id}')
    conn.execute(
        """
        INSERT INTO mode_gate_round_videos
            (round_id, video_id, expected_mode, start_ms, sort_order, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(round_id, video_id) DO UPDATE SET
            expected_mode=excluded.expected_mode,
            start_ms=excluded.start_ms,
            sort_order=excluded.sort_order,
            notes=excluded.notes
        """,
        (round_id, video_id, expected_mode, max(0, start_ms), sort_order, notes),
    )
    conn.commit()


def get_mode_gate_round(conn: sqlite3.Connection,
                        round_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM mode_gate_rounds WHERE id = ?', (round_id,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    videos = conn.execute(
        """
        SELECT rv.*, v.streamer, v.filename, v.remote_path,
               v.duration_seconds, v.size_bytes,
               (SELECT width || 'x' || height FROM frames
                WHERE video_id = v.id ORDER BY id LIMIT 1) AS dimensions,
               (SELECT COUNT(*) FROM mode_gate_annotations mga
                JOIN frames f ON f.id = mga.frame_id
                WHERE mga.round_id = rv.round_id
                  AND f.video_id = rv.video_id) AS annotation_count,
               (SELECT COUNT(*) FROM mode_gate_annotations mga
                JOIN frames f ON f.id = mga.frame_id
                WHERE mga.round_id = rv.round_id
                  AND f.video_id = rv.video_id
                  AND mga.evidence = 'blocked_gate') AS blocked_count,
               (SELECT COUNT(*) FROM mode_gate_annotations mga
                JOIN frames f ON f.id = mga.frame_id
                WHERE mga.round_id = rv.round_id
                  AND f.video_id = rv.video_id
                  AND mga.evidence = 'open_entrance') AS open_count,
               (SELECT COUNT(*) FROM mode_gate_annotations mga
                JOIN frames f ON f.id = mga.frame_id
                WHERE mga.round_id = rv.round_id
                  AND f.video_id = rv.video_id
                  AND mga.evidence = 'no_evidence') AS no_evidence_count
        FROM mode_gate_round_videos rv
        JOIN videos v ON v.id = rv.video_id
        WHERE rv.round_id = ?
        ORDER BY rv.sort_order, rv.video_id
        """,
        (round_id,),
    ).fetchall()
    result['videos'] = [dict(video) for video in videos]
    result['annotation_count'] = sum(
        int(video['annotation_count']) for video in result['videos'])
    return result


def get_active_mode_gate_round(
        conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT id FROM mode_gate_rounds WHERE active = 1 '
        'ORDER BY created_at DESC LIMIT 1'
    ).fetchone()
    return get_mode_gate_round(conn, row['id']) if row else None


def get_mode_gate_annotation(conn: sqlite3.Connection, *, round_id: str,
                             frame_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM mode_gate_annotations '
        'WHERE round_id = ? AND frame_id = ?',
        (round_id, frame_id),
    ).fetchone()
    if not row:
        return None
    annotation = dict(row)
    annotation['boxes'] = [dict(box) for box in conn.execute(
        'SELECT id, x, y, w, h FROM mode_gate_boxes '
        'WHERE round_id = ? AND frame_id = ? ORDER BY sort_order, id',
        (round_id, frame_id),
    ).fetchall()]
    return annotation


def _validate_mode_gate_boxes(
        evidence: str, boxes: Optional[List[Dict[str, Any]]], *,
        x: Optional[float], y: Optional[float],
        w: Optional[float], h: Optional[float]) -> List[Dict[str, float]]:
    if boxes is None:
        legacy_coords = (x, y, w, h)
        boxes = [] if all(value is None for value in legacy_coords) else [{
            'x': x, 'y': y, 'w': w, 'h': h,
        }]
    if not isinstance(boxes, list):
        raise ValueError('boxes 必须是边界框数组')

    normalized: List[Dict[str, float]] = []
    for box in boxes:
        if not isinstance(box, dict):
            raise ValueError('每个边界框必须是对象')
        try:
            coords = {name: float(box[name]) for name in ('x', 'y', 'w', 'h')}
        except (KeyError, TypeError, ValueError):
            raise ValueError('每个边界框都必须包含数字 x/y/w/h')
        bx, by, bw, bh = (
            coords['x'], coords['y'], coords['w'], coords['h'])
        if not (0 <= bx <= 1 and 0 <= by <= 1
                and 0 < bw <= 1 and 0 < bh <= 1
                and bx + bw <= 1.001 and by + bh <= 1.001):
            raise ValueError('框坐标必须归一化到 [0,1]')
        normalized.append(coords)

    if evidence == 'no_evidence':
        if normalized:
            raise ValueError('no_evidence 不能带边界框')
    elif not normalized:
        raise ValueError('光栅或开放入口必须至少有一个边界框')
    return normalized


def save_mode_gate_annotation(conn: sqlite3.Connection, *, round_id: str,
                              frame_id: int, evidence: str,
                              boxes: Optional[List[Dict[str, Any]]] = None,
                              x: Optional[float] = None,
                              y: Optional[float] = None,
                              w: Optional[float] = None,
                              h: Optional[float] = None,
                              notes: str = '') -> Dict[str, Any]:
    if evidence not in MODE_GATE_EVIDENCE:
        raise ValueError('未知的光栅证据类型')
    normalized_boxes = _validate_mode_gate_boxes(
        evidence, boxes, x=x, y=y, w=w, h=h)
    frame = conn.execute(
        'SELECT video_id, timestamp_ms FROM frames WHERE id = ?', (frame_id,)
    ).fetchone()
    if not frame:
        raise KeyError(f'帧不存在: {frame_id}')
    member = conn.execute(
        'SELECT 1 FROM mode_gate_round_videos '
        'WHERE round_id = ? AND video_id = ?',
        (round_id, frame['video_id']),
    ).fetchone()
    if not member:
        raise KeyError('该帧不属于本轮挑选的视频')
    first = normalized_boxes[0] if normalized_boxes else {}
    updated_at = now()
    with conn:
        conn.execute(
            """
            INSERT INTO mode_gate_annotations
                (round_id, frame_id, evidence, x, y, w, h, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(round_id, frame_id) DO UPDATE SET
                evidence=excluded.evidence,
                x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (round_id, frame_id, evidence,
             first.get('x'), first.get('y'), first.get('w'), first.get('h'),
             notes, updated_at),
        )
        conn.execute(
            'DELETE FROM mode_gate_boxes WHERE round_id = ? AND frame_id = ?',
            (round_id, frame_id),
        )
        conn.executemany(
            """
            INSERT INTO mode_gate_boxes
                (round_id, frame_id, sort_order, x, y, w, h, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (round_id, frame_id, index,
                 box['x'], box['y'], box['w'], box['h'], updated_at)
                for index, box in enumerate(normalized_boxes)
            ],
        )
        conn.execute(
            'UPDATE mode_gate_round_videos '
            'SET last_pts_ms = ?, last_frame_id = ?, updated_at = ? '
            'WHERE round_id = ? AND video_id = ?',
            (frame['timestamp_ms'], frame_id, updated_at,
             round_id, frame['video_id']),
        )
    annotation = get_mode_gate_annotation(
        conn, round_id=round_id, frame_id=frame_id)
    assert annotation is not None
    return annotation


def delete_mode_gate_annotation(conn: sqlite3.Connection, *, round_id: str,
                                frame_id: int) -> None:
    with conn:
        conn.execute(
            'DELETE FROM mode_gate_boxes WHERE round_id = ? AND frame_id = ?',
            (round_id, frame_id),
        )
        conn.execute(
            'DELETE FROM mode_gate_annotations '
            'WHERE round_id = ? AND frame_id = ?',
            (round_id, frame_id),
        )


def list_mode_gate_frames(conn: sqlite3.Connection, *, round_id: str,
                          video_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.*, mga.evidence, mga.x, mga.y, mga.w, mga.h,
               mga.notes AS mode_gate_notes, mga.updated_at AS mode_gate_updated_at
        FROM mode_gate_annotations mga
        JOIN frames f ON f.id = mga.frame_id
        WHERE mga.round_id = ? AND f.video_id = ?
        ORDER BY f.timestamp_ms
        """,
        (round_id, video_id),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------- BP 主动学习复核 ----------

BP_REVIEW_LABELS = {'bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp'}
BP_REVIEW_STATUSES = {'pending', 'confirmed', 'skipped'}
BP_VISUAL_CONDITIONS = {
    'clear',
    'occluded',
    'windowed',
    'occluded_windowed',
    'unreadable',
}


def upsert_bp_review_item(
        conn: sqlite3.Connection, *, frame_id: int, model_version: str,
        suggested_label: str, suggestion_confidence: float,
        stage_class: str, stage_confidence: float,
        pre_match_confidence: float, mode_class: str,
        mode_confidence: float, mode_margin: float,
        selection_reason: str, priority: float,
        raw_prediction: Dict[str, Any]) -> bool:
    """写入一个待复核候选；已经人工处理的帧只更新模型信息，不退回队列。"""
    if suggested_label not in BP_REVIEW_LABELS:
        raise ValueError(f'未知 BP 建议标签: {suggested_label}')
    if not conn.execute(
            'SELECT 1 FROM frames WHERE id = ?', (frame_id,)).fetchone():
        raise KeyError(f'帧不存在: {frame_id}')
    existing = conn.execute(
        'SELECT review_status FROM bp_review_items WHERE frame_id = ?',
        (frame_id,),
    ).fetchone()
    created_at = now()
    conn.execute(
        """
        INSERT INTO bp_review_items
            (frame_id, model_version, suggested_label, suggestion_confidence,
             stage_class, stage_confidence, pre_match_confidence,
             mode_class, mode_confidence, mode_margin, selection_reason,
             priority, raw_prediction, review_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(frame_id) DO UPDATE SET
            model_version=excluded.model_version,
            suggested_label=excluded.suggested_label,
            suggestion_confidence=excluded.suggestion_confidence,
            stage_class=excluded.stage_class,
            stage_confidence=excluded.stage_confidence,
            pre_match_confidence=excluded.pre_match_confidence,
            mode_class=excluded.mode_class,
            mode_confidence=excluded.mode_confidence,
            mode_margin=excluded.mode_margin,
            selection_reason=excluded.selection_reason,
            priority=excluded.priority,
            raw_prediction=excluded.raw_prediction
        """,
        (
            frame_id, model_version, suggested_label,
            max(0.0, min(1.0, float(suggestion_confidence))),
            stage_class, max(0.0, min(1.0, float(stage_confidence))),
            max(0.0, min(1.0, float(pre_match_confidence))),
            mode_class, max(0.0, min(1.0, float(mode_confidence))),
            max(0.0, min(1.0, float(mode_margin))), selection_reason,
            float(priority), json.dumps(raw_prediction, ensure_ascii=False),
            created_at,
        ),
    )
    conn.commit()
    return existing is None


def list_bp_review_items(
        conn: sqlite3.Connection, *, status: str = 'pending',
        limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
    if status not in BP_REVIEW_STATUSES and status != 'all':
        raise ValueError(f'未知 BP 复核状态: {status}')
    where = '' if status == 'all' else 'WHERE b.review_status = ?'
    args: List[Any] = [] if status == 'all' else [status]
    args.extend((limit, offset))
    rows = conn.execute(
        f"""
        SELECT b.*, f.video_id, f.timestamp_ms, f.width, f.height,
               f.frame_path, f.thumb_path, f.phash,
               v.streamer, v.filename,
               a.screen_type AS existing_screen_type,
               a.game_mode AS existing_game_mode,
               a.annotation_status AS existing_annotation_status
        FROM bp_review_items b
        JOIN frames f ON f.id = b.frame_id
        JOIN videos v ON v.id = f.video_id
        LEFT JOIN annotations a ON a.frame_id = b.frame_id
        {where}
        ORDER BY
            CASE b.review_status WHEN 'pending' THEN 0 ELSE 1 END,
            b.priority DESC, f.video_id, f.timestamp_ms
        LIMIT ? OFFSET ?
        """,
        args,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['raw_prediction'] = json.loads(item['raw_prediction'] or '{}')
        result.append(item)
    return result


def review_bp_item(
        conn: sqlite3.Connection, *, frame_id: int, label: Optional[str],
        visual_condition: str = 'clear') -> Dict[str, Any]:
    if label is not None and label not in BP_REVIEW_LABELS:
        raise ValueError(f'未知 BP 确认标签: {label}')
    if visual_condition not in BP_VISUAL_CONDITIONS:
        raise ValueError(f'未知 BP 画面情况: {visual_condition}')
    if label is None or label == 'not_bp':
        visual_condition = 'clear'
    row = conn.execute(
        'SELECT 1 FROM bp_review_items WHERE frame_id = ?', (frame_id,)
    ).fetchone()
    if not row:
        raise KeyError(f'BP 复核候选不存在: {frame_id}')
    status = 'skipped' if label is None else 'confirmed'
    conn.execute(
        'UPDATE bp_review_items SET review_status = ?, confirmed_label = ?, '
        'visual_condition = ?, reviewed_at = ? WHERE frame_id = ?',
        (status, label, visual_condition, now(), frame_id),
    )
    audit(
        conn, 'bp_review', frame_id=frame_id,
        detail=json.dumps(
            {
                'label': label,
                'status': status,
                'visual_condition': visual_condition,
            },
            ensure_ascii=False,
        ),
    )
    item = list_bp_review_items(conn, status=status, limit=100000)
    return next(row for row in item if row['frame_id'] == frame_id)


def bp_review_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    statuses = {
        row['review_status']: row['count']
        for row in conn.execute(
            'SELECT review_status, COUNT(*) AS count FROM bp_review_items '
            'GROUP BY review_status'
        ).fetchall()
    }
    confirmed = {
        row['confirmed_label']: row['count']
        for row in conn.execute(
            'SELECT confirmed_label, COUNT(*) AS count FROM bp_review_items '
            "WHERE review_status = 'confirmed' GROUP BY confirmed_label"
        ).fetchall()
    }
    existing_rows = conn.execute(
        """
        SELECT a.game_mode, COUNT(*) AS count, COUNT(DISTINCT f.video_id) AS videos
        FROM annotations a
        JOIN frames f ON f.id = a.frame_id
        WHERE a.annotation_status = 'complete'
          AND a.screen_type IN ('hero_select_bp', 'hero_select_blind',
                                'hero_select_aram')
          AND a.game_mode IN ('3v3', 'aram', '5v5')
        GROUP BY a.game_mode
        """
    ).fetchall()
    return {
        'total': sum(statuses.values()),
        'statuses': statuses,
        'confirmed_labels': confirmed,
        'existing_human_labels': {
            row['game_mode']: {
                'frames': row['count'], 'videos': row['videos'],
            }
            for row in existing_rows
        },
    }


# ---------- 结算页 / 计分板主动学习复核 ----------

KEY_SCREEN_REVIEW_LABELS = {'result_page', 'scoreboard', 'other'}
KEY_SCREEN_REVIEW_STATUSES = {'pending', 'confirmed', 'skipped'}
KEY_SCREEN_VISUAL_CONDITIONS = {'clear', 'occluded', 'unreadable'}


def upsert_key_screen_review_item(
        conn: sqlite3.Connection, *, frame_id: int, model_version: str,
        suggested_label: str, suggestion_confidence: float,
        selection_reason: str, raw_prediction: Dict[str, Any],
        priority: float = 0) -> bool:
    """写入关键画面预标；已人工处理的帧不会退回待确认。"""
    if suggested_label not in KEY_SCREEN_REVIEW_LABELS:
        raise ValueError(f'未知关键画面建议标签: {suggested_label}')
    if not conn.execute(
            'SELECT 1 FROM frames WHERE id = ?', (frame_id,)).fetchone():
        raise KeyError(f'帧不存在: {frame_id}')
    existing = conn.execute(
        'SELECT review_status FROM key_screen_review_items WHERE frame_id = ?',
        (frame_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO key_screen_review_items
            (frame_id, model_version, suggested_label, suggestion_confidence,
             selection_reason, priority, raw_prediction, review_status,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(frame_id) DO UPDATE SET
            model_version=excluded.model_version,
            suggested_label=excluded.suggested_label,
            suggestion_confidence=excluded.suggestion_confidence,
            selection_reason=excluded.selection_reason,
            priority=excluded.priority,
            raw_prediction=excluded.raw_prediction
        """,
        (
            frame_id,
            model_version,
            suggested_label,
            max(0.0, min(1.0, float(suggestion_confidence))),
            selection_reason,
            float(priority),
            json.dumps(raw_prediction, ensure_ascii=False),
            now(),
        ),
    )
    conn.commit()
    return existing is None


def list_key_screen_review_items(
        conn: sqlite3.Connection, *, status: str = 'pending',
        limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
    if status not in KEY_SCREEN_REVIEW_STATUSES and status != 'all':
        raise ValueError(f'未知关键画面复核状态: {status}')
    where = '' if status == 'all' else 'WHERE k.review_status = ?'
    args: List[Any] = [] if status == 'all' else [status]
    args.extend((limit, offset))
    rows = conn.execute(
        f"""
        SELECT k.*, f.video_id, f.timestamp_ms, f.width, f.height,
               f.frame_path, f.thumb_path, f.phash,
               v.streamer, v.filename,
               a.screen_type AS existing_screen_type,
               a.annotation_status AS existing_annotation_status
        FROM key_screen_review_items k
        JOIN frames f ON f.id = k.frame_id
        JOIN videos v ON v.id = f.video_id
        LEFT JOIN annotations a ON a.frame_id = k.frame_id
        {where}
        ORDER BY
            CASE k.review_status WHEN 'pending' THEN 0 ELSE 1 END,
            k.priority DESC, f.video_id, f.timestamp_ms
        LIMIT ? OFFSET ?
        """,
        args,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['raw_prediction'] = json.loads(item['raw_prediction'] or '{}')
        result.append(item)
    return result


def review_key_screen_item(
        conn: sqlite3.Connection, *, frame_id: int, label: Optional[str],
        visual_condition: str = 'clear') -> Dict[str, Any]:
    if label is not None and label not in KEY_SCREEN_REVIEW_LABELS:
        raise ValueError(f'未知关键画面确认标签: {label}')
    if visual_condition not in KEY_SCREEN_VISUAL_CONDITIONS:
        raise ValueError(f'未知关键画面画质情况: {visual_condition}')
    if label is None:
        visual_condition = 'clear'
    if not conn.execute(
            'SELECT 1 FROM key_screen_review_items WHERE frame_id = ?',
            (frame_id,)).fetchone():
        raise KeyError(f'关键画面复核候选不存在: {frame_id}')
    status = 'skipped' if label is None else 'confirmed'
    conn.execute(
        'UPDATE key_screen_review_items SET review_status = ?, '
        'confirmed_label = ?, visual_condition = ?, reviewed_at = ? '
        'WHERE frame_id = ?',
        (status, label, visual_condition, now(), frame_id),
    )
    audit(
        conn,
        'key_screen_review',
        frame_id=frame_id,
        detail=json.dumps(
            {
                'label': label,
                'status': status,
                'visual_condition': visual_condition,
            },
            ensure_ascii=False,
        ),
    )
    items = list_key_screen_review_items(
        conn, status=status, limit=100_000)
    return next(item for item in items if item['frame_id'] == frame_id)


def key_screen_review_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    statuses = {
        row['review_status']: row['count']
        for row in conn.execute(
            'SELECT review_status, COUNT(*) AS count '
            'FROM key_screen_review_items GROUP BY review_status'
        ).fetchall()
    }
    confirmed = {
        row['confirmed_label']: row['count']
        for row in conn.execute(
            'SELECT confirmed_label, COUNT(*) AS count '
            'FROM key_screen_review_items '
            "WHERE review_status = 'confirmed' GROUP BY confirmed_label"
        ).fetchall()
    }
    existing = {
        row['label']: row['count']
        for row in conn.execute(
            """
            SELECT CASE
                     WHEN screen_type = 'result_page' THEN 'result_page'
                     WHEN screen_type IN ('scoreboard', 'death_scoreboard')
                       THEN 'scoreboard'
                     ELSE 'other'
                   END AS label,
                   COUNT(*) AS count
            FROM annotations
            WHERE annotation_status = 'complete'
            GROUP BY label
            """
        ).fetchall()
    }
    return {
        'total': sum(statuses.values()),
        'statuses': statuses,
        'confirmed_labels': confirmed,
        'existing_human_labels': existing,
    }
