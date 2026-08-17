"""虚荣视觉标注工作台 —— SQLite 交互工作库。

权威数据以 JSONL 导出(可版本化);本库只服务标注工作流。
分层标注体系见 README:content_family → game_context → screen_type,
辅助字段 game_mode / match_kind / view_context / quality_flags / black_bars / ocr_usable。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
CREATE INDEX IF NOT EXISTS idx_audit_action_frame
    ON audit_log (action, frame_id);

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

-- Worker 候选的本地镜像与 NAS 双向复核状态。模型建议和人工结论分开；
-- 只有 confirmed 的人工结论才允许进入训练快照。
CREATE TABLE IF NOT EXISTS worker_candidate_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    task TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    suggested_label TEXT NOT NULL,
    suggestion_confidence REAL NOT NULL,
    suggested_boxes_json TEXT NOT NULL DEFAULT '[]',
    raw_metadata_json TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'pending', -- pending | confirmed | skipped | conflict
    confirmed_label TEXT,
    visual_condition TEXT NOT NULL DEFAULT 'clear',
    boxes_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    candidate_created_at INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    remote_reviewed_at TEXT,
    sync_state TEXT NOT NULL DEFAULT 'clean', -- clean | dirty | conflict
    remote_review_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_worker_candidate_queue
    ON worker_candidate_items (task, review_status, candidate_created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_worker_candidate_sync
    ON worker_candidate_items (sync_state, reviewed_at, id);

-- 新模型共用的一图多标签人工结论。NULL 明确表示“尚未人工判断”，不能当作
-- 负样本；模型建议单独存在 training_review_sources，永远不冒充人工真值。
CREATE TABLE IF NOT EXISTS training_review_items (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    match_flow_label TEXT CHECK (
        match_flow_label IS NULL OR match_flow_label IN (
            'match_flow', 'not_match_flow', 'unreadable')),
    match_mode_label TEXT CHECK (
        match_mode_label IS NULL OR match_mode_label IN (
            '3v3', 'aram', '5v5', 'unreadable')),
    hero_select_label TEXT CHECK (
        hero_select_label IS NULL OR hero_select_label IN (
            'not_select', 'select_3v3', 'select_aram', 'select_5v5',
            'unreadable')),
    hero_select_variant TEXT CHECK (
        hero_select_variant IS NULL OR hero_select_variant IN (
            'bp', 'blind', 'random', 'unreadable')),
    hero_select_visibility TEXT CHECK (
        hero_select_visibility IS NULL OR hero_select_visibility IN (
            'clear', 'occluded', 'unknown')),
    result_panel_label TEXT CHECK (
        result_panel_label IS NULL OR result_panel_label IN (
            'result_panel', 'no_result_panel', 'unreadable')),
    hero_layout_label TEXT CHECK (
        hero_layout_label IS NULL OR hero_layout_label IN (
            'gameplay_hud', 'scoreboard', 'result_page', 'none',
            'unreadable')),
    panel_render_state TEXT NOT NULL DEFAULT 'clear' CHECK (
        panel_render_state IN ('clear', 'translucent', 'unknown')),
    ocr_usable TEXT NOT NULL DEFAULT 'yes' CHECK (
        ocr_usable IN ('yes', 'no', 'unknown')),
    result_occlusion TEXT NOT NULL DEFAULT 'none' CHECK (
        result_occlusion IN ('none', 'occluded', 'unknown')),
    occluder_types TEXT NOT NULL DEFAULT '[]',
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        review_status IN ('pending', 'partial', 'confirmed', 'skipped')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_review_status
    ON training_review_items (review_status, updated_at DESC, frame_id);

-- 同一张图片可同时来自旧人工标注、Worker 多个模型和历史结算图。来源与预标
-- 一对多保存，图片和人工标签都只保留一份。
CREATE TABLE IF NOT EXISTS training_review_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    image_path TEXT NOT NULL DEFAULT '',
    suggestions_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_created_at INTEGER NOT NULL DEFAULT 0,
    sync_state TEXT NOT NULL DEFAULT 'clean' CHECK (
        sync_state IN ('clean', 'dirty', 'conflict')),
    remote_review_hash TEXT NOT NULL DEFAULT '',
    remote_reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_training_review_sources_frame
    ON training_review_sources (frame_id, source_created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_training_review_sources_sync
    ON training_review_sources (source_type, sync_state, id);

-- 积分板／结算图的英雄阵容复核。算法预填与人工结论分列保存，避免未确认
-- 的 SIFT 结果直接成为训练真值。
CREATE TABLE IF NOT EXISTS training_review_hero_lineups (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    screen_type TEXT NOT NULL CHECK (
        screen_type IN ('gameplay_hud', 'scoreboard', 'result_page')),
    team_size INTEGER NOT NULL CHECK (team_size IN (3, 5)),
    suggestion_method TEXT NOT NULL DEFAULT '',
    player_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        player_status IN ('pending', 'identified', 'unreadable')),
    player_side TEXT CHECK (
        player_side IS NULL OR player_side IN ('left', 'right')),
    player_slot INTEGER CHECK (
        player_slot IS NULL OR player_slot BETWEEN 1 AND 5),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        review_status IN ('pending', 'confirmed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS training_review_hero_slots (
    frame_id INTEGER NOT NULL REFERENCES training_review_hero_lineups(frame_id)
        ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('left', 'right')),
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
    crop_x REAL NOT NULL,
    crop_y REAL NOT NULL,
    crop_w REAL NOT NULL,
    crop_h REAL NOT NULL,
    suggested_label TEXT NOT NULL DEFAULT '',
    suggestion_confidence REAL NOT NULL DEFAULT 0 CHECK (
        suggestion_confidence BETWEEN 0 AND 1),
    confirmed_label TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (frame_id, side, slot)
);
CREATE INDEX IF NOT EXISTS idx_training_review_hero_status
    ON training_review_hero_lineups (review_status, updated_at DESC, frame_id);

-- 主播在同一类画面和近似宽高比下，英雄头像位置通常保持不变。模板只保存
-- 人工画出的圆框位置，不保存算法预填或英雄真值。
CREATE TABLE IF NOT EXISTS training_review_hero_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer TEXT NOT NULL,
    screen_type TEXT NOT NULL CHECK (
        screen_type IN ('gameplay_hud', 'scoreboard', 'result_page')),
    team_size INTEGER NOT NULL CHECK (team_size IN (3, 5)),
    layout_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (streamer, screen_type, team_size, layout_key)
);

CREATE TABLE IF NOT EXISTS training_review_hero_template_slots (
    template_id INTEGER NOT NULL REFERENCES training_review_hero_templates(id)
        ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('left', 'right')),
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
    crop_x REAL NOT NULL,
    crop_y REAL NOT NULL,
    crop_w REAL NOT NULL,
    crop_h REAL NOT NULL,
    PRIMARY KEY (template_id, side, slot)
);

CREATE TABLE IF NOT EXISTS model_validations (
    run_id TEXT PRIMARY KEY REFERENCES training_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,                    -- pending | passed | failed
    notes TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    tested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_packages (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,                    -- incomplete | ready
    path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES model_packages(id),
    target TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed')),
    previous_package_id TEXT NOT NULL DEFAULT '',
    worker_package_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_deployments_created
    ON model_deployments (created_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_deployments_active_target
    ON model_deployments (target)
    WHERE status IN ('queued', 'running');

-- NAS 上的 Vision Lab 只负责轻量调度。数据集生成、批量预填、训练、验收和
-- 打包等重任务由可暂停的 Vision Worker 领取。
CREATE TABLE IF NOT EXISTS vision_workers (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    version TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'idle',
    active_job_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vision_workers_seen
    ON vision_workers (enabled, last_seen_at DESC, id);

CREATE TABLE IF NOT EXISTS vision_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    related_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    stage TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    worker_id TEXT REFERENCES vision_workers(id),
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vision_jobs_queue
    ON vision_jobs (status, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_vision_jobs_worker
    ON vision_jobs (worker_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vision_jobs_active_related
    ON vision_jobs (kind, related_id)
    WHERE status IN ('queued', 'running') AND related_id != '';

-- 本地工作目录迁移只执行一次，避免每次 API 打开连接都遍历数万张图片。
CREATE TABLE IF NOT EXISTS workspace_migrations (
    id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""

DEFAULT_TASKS = [
    ('result_detector', '结算界面检测(result_panel 边界框)', '检测赛后结算面板边界框'),
    (
        'game_state',
        '游戏画面与游戏状态识别(screen_type 分类)',
        '基于分层 screen_type 的画面分类',
    ),
    ('game_mode', '游戏模式识别(3v3/5v5/aram/blitz)', '地图/玩法模式分类'),
    ('viewport', '游戏窗口/有效画面区域检测(viewport_bbox)', '直播画面中游戏窗口定位'),
    ('same_match', '同局判断(双图配对)', '两张 HUD 是否属于同一局'),
    ('mode_gate', '3V3/大乱斗光栅专项', '圈出大乱斗光栅或 3V3 同位置的开放入口'),
    ('bp_review', 'BP 模式主动学习复核', '模型预标选英雄画面，由人工确认或纠错'),
    (
        'key_screen_review',
        '结算页/计分板主动学习复核',
        '模型预标关键画面，由人工确认结算页、计分板或其他',
    ),
    (
        'screen_state',
        '画面状态分类',
        '区分非虚荣、游戏外、对局前、对局中、天赋选择、赛后与转场',
    ),
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
            "NOT NULL DEFAULT 'draft'"
        )
    if 'talent_mode' not in cols:
        conn.execute(
            'ALTER TABLE annotations ADD COLUMN talent_mode INTEGER '
            "NOT NULL DEFAULT 0"
        )
    for col, ddl in [
        ('result_clarity', 'TEXT'),
        ('result_occlusion', 'TEXT'),
        ('occluder_types', "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        if col not in cols:
            conn.execute(f'ALTER TABLE annotations ADD COLUMN {col} {ddl}')
    # 旧 screen_type 枚举 → 新枚举
    for old, new in config.SCREEN_TYPE_MIGRATION.items():
        conn.execute(
            'UPDATE annotations SET screen_type = ? WHERE screen_type = ?', (new, old)
        )
    # 旧标注记录(以前保存过完整标注)→ 迁移为 complete
    conn.execute(
        "UPDATE annotations SET annotation_status = 'complete' "
        "WHERE annotation_status = 'draft' AND content_family IS NOT NULL "
        "AND (content_family != 'vainglory' "
        "     OR (game_context IS NOT NULL AND screen_type IS NOT NULL))"
    )
    # labeled 是 annotations 的派生索引。目录搬迁前有少量完整标注没有同步该
    # 标志，不能因此从检测数据集里漏掉人工真值。
    conn.execute(
        "UPDATE frames SET labeled = 1 WHERE labeled = 0 AND EXISTS ("
        "SELECT 1 FROM annotations a WHERE a.frame_id = frames.id "
        "AND a.annotation_status = 'complete')"
    )
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
    review_cols = {
        row['name'] for row in conn.execute('PRAGMA table_info(training_review_items)')
    }
    if 'panel_render_state' not in review_cols:
        conn.execute(
            'ALTER TABLE training_review_items ADD COLUMN panel_render_state '
            "TEXT NOT NULL DEFAULT 'clear' CHECK (panel_render_state IN ("
            "'clear', 'translucent', 'unknown'))"
        )
        conn.execute(
            """
            UPDATE training_review_items
            SET panel_render_state = (
                SELECT CASE annotation.result_clarity
                    WHEN 'translucent' THEN 'translucent'
                    WHEN 'unknown' THEN 'unknown'
                    ELSE 'clear'
                END
                FROM annotations annotation
                WHERE annotation.frame_id = training_review_items.frame_id
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1 FROM annotations annotation
                WHERE annotation.frame_id = training_review_items.frame_id
                  AND annotation.result_clarity IN ('translucent', 'unknown')
            )
            """
        )
        review_cols.add('panel_render_state')
    for column, ddl in (
        ('ocr_usable', "TEXT NOT NULL DEFAULT 'yes'"),
        ('result_occlusion', "TEXT NOT NULL DEFAULT 'none'"),
        ('occluder_types', "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if column not in review_cols:
            conn.execute(f'ALTER TABLE training_review_items ADD COLUMN {column} {ddl}')
            review_cols.add(column)
    if 'hero_layout_label' not in review_cols:
        conn.execute(
            'ALTER TABLE training_review_items ADD COLUMN hero_layout_label '
            "TEXT CHECK (hero_layout_label IS NULL OR hero_layout_label IN ("
            "'gameplay_hud', 'scoreboard', 'result_page', 'none', "
            "'unreadable'))"
        )
    if 'hero_select_variant' not in review_cols:
        conn.execute(
            'ALTER TABLE training_review_items ADD COLUMN hero_select_variant '
            'TEXT CHECK (hero_select_variant IS NULL OR '
            "hero_select_variant IN ('bp', 'blind', 'random', 'unreadable'))"
        )
    if 'hero_select_visibility' not in review_cols:
        conn.execute(
            'ALTER TABLE training_review_items '
            'ADD COLUMN hero_select_visibility TEXT CHECK ('
            'hero_select_visibility IS NULL OR '
            "hero_select_visibility IN ('clear', 'occluded', 'unknown'))"
        )
    conn.execute(
        "UPDATE training_review_items SET hero_select_variant = 'random' "
        "WHERE hero_select_label = 'select_aram' "
        'AND hero_select_variant IS NULL'
    )
    conn.execute(
        """
        UPDATE training_review_items
        SET hero_select_variant = (
            SELECT CASE annotation.screen_type
                WHEN 'hero_select_bp' THEN 'bp'
                WHEN 'hero_select_blind' THEN 'blind'
            END
            FROM annotations annotation
            WHERE annotation.frame_id = training_review_items.frame_id
              AND annotation.annotation_status = 'complete'
              AND annotation.screen_type IN (
                  'hero_select_bp', 'hero_select_blind')
            LIMIT 1
        )
        WHERE hero_select_variant IS NULL
          AND EXISTS (
              SELECT 1 FROM annotations annotation
              WHERE annotation.frame_id = training_review_items.frame_id
                AND annotation.annotation_status = 'complete'
                AND annotation.screen_type IN (
                    'hero_select_bp', 'hero_select_blind')
          )
        """
    )
    _migrate_training_review_hero_lineups(conn)
    _migrate_training_review_player_slot(conn)
    repair_managed_paths(conn)


def _migrate_training_review_hero_lineups(conn: sqlite3.Connection) -> None:
    """让旧阵容表接受 HUD，同时原样保留已有阵容和人工结论。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ('training_review_hero_lineups',),
    ).fetchone()
    if row is None or 'gameplay_hud' in str(row['sql'] or ''):
        return
    with conn:
        conn.execute('DROP TABLE IF EXISTS training_review_hero_slots_new')
        conn.execute('DROP TABLE IF EXISTS training_review_hero_lineups_new')
        conn.execute(
            """
            CREATE TABLE training_review_hero_lineups_new (
                frame_id INTEGER PRIMARY KEY REFERENCES frames(id)
                    ON DELETE CASCADE,
                screen_type TEXT NOT NULL CHECK (
                    screen_type IN (
                        'gameplay_hud', 'scoreboard', 'result_page')),
                team_size INTEGER NOT NULL CHECK (team_size IN (3, 5)),
                suggestion_method TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    review_status IN ('pending', 'confirmed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE training_review_hero_slots_new (
                frame_id INTEGER NOT NULL
                    REFERENCES training_review_hero_lineups_new(frame_id)
                    ON DELETE CASCADE,
                side TEXT NOT NULL CHECK (side IN ('left', 'right')),
                slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
                crop_x REAL NOT NULL,
                crop_y REAL NOT NULL,
                crop_w REAL NOT NULL,
                crop_h REAL NOT NULL,
                suggested_label TEXT NOT NULL DEFAULT '',
                suggestion_confidence REAL NOT NULL DEFAULT 0 CHECK (
                    suggestion_confidence BETWEEN 0 AND 1),
                confirmed_label TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (frame_id, side, slot)
            )
            """
        )
        conn.execute(
            'INSERT INTO training_review_hero_lineups_new '
            'SELECT * FROM training_review_hero_lineups'
        )
        conn.execute(
            'INSERT INTO training_review_hero_slots_new '
            'SELECT * FROM training_review_hero_slots'
        )
        conn.execute('DROP TABLE training_review_hero_slots')
        conn.execute('DROP TABLE training_review_hero_lineups')
        conn.execute(
            'ALTER TABLE training_review_hero_lineups_new '
            'RENAME TO training_review_hero_lineups'
        )
        conn.execute(
            'ALTER TABLE training_review_hero_slots_new '
            'RENAME TO training_review_hero_slots'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_training_review_hero_status '
            'ON training_review_hero_lineups '
            '(review_status, updated_at DESC, frame_id)'
        )


def _migrate_training_review_player_slot(conn: sqlite3.Connection) -> None:
    """给既有英雄阵容补充主播英雄位置及其人工判断状态。"""
    columns = {
        row['name']
        for row in conn.execute('PRAGMA table_info(training_review_hero_lineups)')
    }
    if 'player_side' not in columns:
        conn.execute(
            'ALTER TABLE training_review_hero_lineups ADD COLUMN '
            "player_side TEXT CHECK (player_side IS NULL OR player_side IN ("
            "'left', 'right'))"
        )
    if 'player_slot' not in columns:
        conn.execute(
            'ALTER TABLE training_review_hero_lineups ADD COLUMN '
            'player_slot INTEGER CHECK ('
            'player_slot IS NULL OR player_slot BETWEEN 1 AND 5)'
        )
    if 'player_status' not in columns:
        conn.execute(
            'ALTER TABLE training_review_hero_lineups ADD COLUMN '
            "player_status TEXT NOT NULL DEFAULT 'pending' CHECK ("
            "player_status IN ('pending', 'identified', 'unreadable'))"
        )
        conn.execute(
            "UPDATE training_review_hero_lineups SET player_status = CASE "
            "WHEN player_side IN ('left', 'right') "
            "AND player_slot BETWEEN 1 AND team_size THEN 'identified' "
            "ELSE 'pending' END"
        )


def repair_managed_paths(conn: sqlite3.Connection) -> Dict[str, int]:
    """修复迁移到 NAS 后仍指向旧工作目录的受管文件路径。

    只在原路径已经失效、且当前工作目录存在对应文件时更新。记录按工作目录
    区分，因此同一份数据库从 Mac 搬到 /data 后会执行一次，而不会每次连接扫描。
    """
    workspace_key = hashlib.sha256(
        str(config.WORK_DIR.resolve()).encode('utf-8')
    ).hexdigest()[:12]
    migration_id = f'managed-workspace-paths-v2-{workspace_key}'
    if conn.execute(
        'SELECT 1 FROM workspace_migrations WHERE id = ?', (migration_id,)
    ).fetchone():
        return {
            'frames': 0,
            'thumbs': 0,
            'datasets': 0,
            'training_runs': 0,
            'model_packages': 0,
        }
    repaired = {
        'frames': 0,
        'thumbs': 0,
        'datasets': 0,
        'training_runs': 0,
        'model_packages': 0,
    }
    rows = conn.execute(
        "SELECT id, sha256, frame_path, thumb_path FROM frames "
        "WHERE frame_path != '' OR thumb_path != ''"
    ).fetchall()
    for row in rows:
        updates: Dict[str, str] = {}
        frame_path = Path(str(row['frame_path'] or ''))
        managed_frame = config.FRAME_DIR / f"{row['sha256']}.jpg"
        if (
            str(row['frame_path'] or '')
            and not frame_path.is_file()
            and managed_frame.is_file()
        ):
            updates['frame_path'] = str(managed_frame)
            repaired['frames'] += 1
        thumb_path = Path(str(row['thumb_path'] or ''))
        managed_thumb = config.THUMB_DIR / f"{row['sha256']}.jpg"
        if (
            str(row['thumb_path'] or '')
            and not thumb_path.is_file()
            and managed_thumb.is_file()
        ):
            updates['thumb_path'] = str(managed_thumb)
            repaired['thumbs'] += 1
        if updates:
            assignments = ', '.join(f'{field} = ?' for field in updates)
            conn.execute(
                f'UPDATE frames SET {assignments} WHERE id = ?',
                [*updates.values(), int(row['id'])],
            )
    for row in conn.execute('SELECT id, manifest_path FROM dataset_versions'):
        current = Path(str(row['manifest_path'] or ''))
        managed = config.EXPORT_DIR / str(row['id']) / 'samples.jsonl'
        if (
            str(row['manifest_path'] or '')
            and not current.is_file()
            and managed.is_file()
        ):
            conn.execute(
                'UPDATE dataset_versions SET manifest_path = ? WHERE id = ?',
                (str(managed), str(row['id'])),
            )
            repaired['datasets'] += 1
    for row in conn.execute(
        'SELECT id, log_path, artifact_path, published_path FROM training_runs'
    ):
        updates = {}
        run_dir = config.WORK_DIR / 'training-runs' / str(row['id'])
        for column, fallback in (
            ('log_path', 'train.log'),
            ('artifact_path', 'model.onnx'),
        ):
            raw = str(row[column] or '')
            if not raw or Path(raw).is_file():
                continue
            managed = run_dir / (Path(raw).name or fallback)
            if managed.is_file():
                updates[column] = str(managed)
        published = str(row['published_path'] or '')
        if published and not Path(published).is_file():
            managed = config.MODELS_DIR / Path(published).name
            if managed.is_file():
                updates['published_path'] = str(managed)
        if updates:
            assignments = ', '.join(f'{column} = ?' for column in updates)
            conn.execute(
                f'UPDATE training_runs SET {assignments} WHERE id = ?',
                [*updates.values(), str(row['id'])],
            )
            repaired['training_runs'] += 1
    for row in conn.execute('SELECT id, path FROM model_packages'):
        current = Path(str(row['path'] or ''))
        if current.exists():
            continue
        package_id = str(row['id'])
        managed_dir = config.WORK_DIR / 'model-packages' / package_id
        managed_zip = config.WORK_DIR / 'model-packages' / f'{package_id}.zip'
        managed = managed_dir if managed_dir.is_dir() else managed_zip
        if managed.exists():
            conn.execute(
                'UPDATE model_packages SET path = ? WHERE id = ?',
                (str(managed), package_id),
            )
            repaired['model_packages'] += 1
    conn.execute(
        'INSERT OR IGNORE INTO workspace_migrations (id, applied_at, detail_json) '
        'VALUES (?, ?, ?)',
        (migration_id, now(), json.dumps(repaired, ensure_ascii=False)),
    )
    return repaired


def now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def audit(
    conn: sqlite3.Connection,
    action: str,
    *,
    frame_id: int = None,
    event_id: int = None,
    detail: str = '',
) -> None:
    conn.execute(
        'INSERT INTO audit_log (frame_id, event_id, action, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (frame_id, event_id, action, detail, now()),
    )
    conn.commit()


# ---------- 视频 ----------


def upsert_video(
    conn: sqlite3.Connection,
    *,
    remote_path: str,
    streamer: str,
    room_id: str,
    filename: str,
    duration_seconds: float,
    size_bytes: int,
    bvid: str = '',
    part_index: int = None,
    part_total: int = None,
) -> int:
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
        (
            remote_path,
            streamer,
            room_id,
            filename,
            bvid,
            part_index,
            part_total,
            duration_seconds,
            size_bytes,
        ),
    )
    conn.commit()
    row = conn.execute(
        'SELECT id FROM videos WHERE remote_path = ?', (remote_path,)
    ).fetchone()
    return int(row['id'])


def list_videos(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    streamer: Optional[str] = None,
    room_id: Optional[str] = None,
    bvid: Optional[str] = None,
    min_size_bytes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    sql = (
        'SELECT v.*, '
        '(SELECT COUNT(*) FROM frames f WHERE f.video_id = v.id) AS frame_count, '
        '(SELECT COUNT(*) FROM frames f WHERE f.video_id = v.id AND f.labeled = 1) AS labeled_count '
        'FROM videos v'
    )
    where: List[str] = [
        "v.remote_path NOT LIKE 'worker-candidate://%'",
        "v.remote_path NOT LIKE 'result-archive://%'",
    ]
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


def set_video_status(
    conn: sqlite3.Connection, video_id: int, status: str, error: str = ''
) -> None:
    conn.execute(
        'UPDATE videos SET status = ?, error = ?, extracted_at = ? WHERE id = ?',
        (status, error, now() if status in ('done', 'failed') else None, video_id),
    )
    conn.commit()


# ---------- 帧 ----------


def add_frames(
    conn: sqlite3.Connection, video_id: int, entries: List[Dict[str, Any]]
) -> List[int]:
    """批量插入帧(sha256 去重)。返回新插入的帧 id 列表。"""
    ids: List[int] = []
    defaults = {'part_index': None, 'part_offset_ms': None, 'session_offset_ms': None}
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


def query_frames(
    conn: sqlite3.Connection,
    *,
    video_id: Optional[int] = None,
    event_id: Optional[int] = None,
    labeled: Optional[int] = None,
    status: Optional[str] = None,
    screen_type: Optional[str] = None,
    strategy: Optional[str] = None,
    representative_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    sql = (
        'SELECT f.*, v.streamer, v.remote_path, v.filename, a.content_family, '
        'a.game_context, a.screen_type, a.game_mode, a.match_kind, '
        'a.view_context, a.quality_flags, a.black_bars, a.ocr_usable, '
        'a.annotation_status, a.notes '
        'FROM frames f JOIN videos v ON v.id = f.video_id '
        'LEFT JOIN annotations a ON a.frame_id = f.id'
    )
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


def create_event(
    conn: sqlite3.Connection,
    video_id: int,
    start_ms: int,
    end_ms: int,
    kind: str = 'candidate',
    notes: str = '',
) -> int:
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


def assign_event(conn: sqlite3.Connection, frame_ids: List[int], event_id: int) -> None:
    for fid in frame_ids:
        conn.execute('UPDATE frames SET event_id = ? WHERE id = ?', (event_id, fid))
    conn.commit()


def merge_events(conn: sqlite3.Connection, event_ids: List[int]) -> int:
    """合并多个事件:时间范围取并集,帧归入最小 id 事件。返回保留的事件 id。"""
    keep = min(event_ids)
    evs = [
        dict(conn.execute('SELECT * FROM events WHERE id = ?', (eid,)).fetchone())
        for eid in event_ids
    ]
    start = min(e['start_ms'] for e in evs)
    end = max(e['end_ms'] for e in evs)
    conn.execute(
        'UPDATE events SET start_ms = ?, end_ms = ? WHERE id = ?', (start, end, keep)
    )
    for eid in event_ids:
        if eid != keep:
            conn.execute(
                'UPDATE frames SET event_id = ? WHERE event_id = ?', (keep, eid)
            )
            conn.execute('DELETE FROM events WHERE id = ?', (eid,))
    conn.commit()
    return keep


def split_event(conn: sqlite3.Connection, event_id: int, split_at_ms: int) -> int:
    """把事件在 split_at_ms 处拆成两个,返回新事件 id。"""
    ev = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    if not ev:
        raise KeyError(event_id)
    # 原事件保留前半段
    conn.execute('UPDATE events SET end_ms = ? WHERE id = ?', (split_at_ms, event_id))
    new_id = create_event(
        conn, ev['video_id'], split_at_ms, ev['end_ms'], ev['kind'], ev['notes']
    )
    conn.execute(
        'UPDATE frames SET event_id = ? WHERE event_id = ? AND timestamp_ms >= ?',
        (new_id, event_id, split_at_ms),
    )
    conn.commit()
    return new_id


# ---------- 标注 ----------


def save_annotation(
    conn: sqlite3.Connection,
    frame_id: int,
    values: Dict[str, Any],
    *,
    label_version: str = 'v1',
    status: str = 'draft',
) -> None:
    if status not in config.ANNOTATION_STATUSES:
        status = 'draft'
    fields = {
        'content_family',
        'non_vainglory_type',
        'game_context',
        'screen_type',
        'game_mode',
        'match_kind',
        'view_context',
        'quality_flags',
        'black_bars',
        'ocr_usable',
        'result_clarity',
        'result_occlusion',
        'occluder_types',
        'notes',
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


def save_box(
    conn: sqlite3.Connection,
    frame_id: int,
    box_type: str,
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
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
        'WHERE f.id = ?',
        (frame_id,),
    ).fetchone()
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
    conn.execute(
        'DELETE FROM boxes WHERE frame_id = ? AND box_type = ?', (frame_id, box_type)
    )
    conn.commit()


def get_boxes(conn: sqlite3.Connection, frame_id: int) -> Dict[str, Dict[str, float]]:
    rows = conn.execute(
        'SELECT box_type, x, y, w, h FROM boxes WHERE frame_id = ?', (frame_id,)
    ).fetchall()
    return {r['box_type']: dict(r) for r in rows}


# ---------- 同局配对 ----------


def save_pair(
    conn: sqlite3.Connection, frame_a_id: int, frame_b_id: int, label: str
) -> None:
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


def add_prediction(
    conn: sqlite3.Connection,
    frame_id: int,
    *,
    model_version: str,
    pred_type: str,
    confidence: float,
    bbox: Optional[Dict[str, float]] = None,
) -> None:
    conn.execute(
        'INSERT INTO model_predictions (frame_id, model_version, pred_type, '
        'confidence, bbox) VALUES (?, ?, ?, ?, ?)',
        (
            frame_id,
            model_version,
            pred_type,
            confidence,
            json.dumps(bbox) if bbox else None,
        ),
    )
    conn.commit()


# ---------- 数据集版本 ----------


def create_dataset_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    task_id: str,
    filter_json: Dict[str, Any],
    counts: Dict[str, Any],
    manifest_path: str,
    git_commit: str = '',
) -> None:
    conn.execute(
        'INSERT INTO dataset_versions (id, task_id, created_at, filter_json, '
        'counts_json, manifest_path, git_commit) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (
            version_id,
            task_id,
            now(),
            json.dumps(filter_json, ensure_ascii=False),
            json.dumps(counts, ensure_ascii=False),
            str(manifest_path),
            git_commit,
        ),
    )
    conn.commit()


def list_dataset_versions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM dataset_versions ORDER BY created_at DESC'
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['filter_json'] = json.loads(d['filter_json'])
        d['counts_json'] = json.loads(d['counts_json'])
        out.append(d)
    return out


# ---------- 训练记录 ----------

TRAINING_RUN_STATUSES = {
    'queued',
    'running',
    'succeeded',
    'failed',
    'cancelled',
    'interrupted',
}


def create_training_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str,
    dataset_version_id: str,
    epochs: int,
    config_json: Dict[str, Any],
    log_path: str,
) -> None:
    if not run_id.strip():
        raise ValueError('训练记录 id 不能为空')
    if epochs <= 0:
        raise ValueError('训练轮数必须为正数')
    dataset = conn.execute(
        'SELECT task_id FROM dataset_versions WHERE id = ?', (dataset_version_id,)
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


def update_training_run(conn: sqlite3.Connection, run_id: str, **updates: Any) -> None:
    allowed = {
        'status',
        'current_epoch',
        'progress',
        'metrics',
        'artifact_path',
        'error',
        'published_path',
        'started_at',
        'finished_at',
    }
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError('未知训练记录字段: {}'.format(', '.join(sorted(unknown))))
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
            values.pop('metrics') or {}, ensure_ascii=False
        )
    assignments = ', '.join(f'{field} = ?' for field in values)
    params = [values[field] for field in values]
    params.append(run_id)
    cursor = conn.execute(
        f'UPDATE training_runs SET {assignments} WHERE id = ?', params
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise KeyError(f'训练记录不存在: {run_id}')
    conn.commit()


def _training_run_dict(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result['metrics_json'] = json.loads(result['metrics_json'] or '{}')
    result['config_json'] = json.loads(result['config_json'] or '{}')
    return result


def get_training_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute('SELECT * FROM training_runs WHERE id = ?', (run_id,)).fetchone()
    return _training_run_dict(row) if row else None


def list_training_runs(
    conn: sqlite3.Connection, *, limit: int = 100
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM training_runs ORDER BY created_at DESC, id DESC LIMIT ?',
        (max(1, min(1_000, int(limit))),),
    ).fetchall()
    return [_training_run_dict(row) for row in rows]


def set_model_validation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    notes: str = '',
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if status not in {'pending', 'passed', 'failed'}:
        raise ValueError(f'未知模型验收状态: {status}')
    run = get_training_run(conn, run_id)
    if run is None:
        raise KeyError(f'训练记录不存在: {run_id}')
    if run['status'] != 'succeeded':
        raise ValueError('只有训练成功的模型才能验收')
    conn.execute(
        """
        INSERT INTO model_validations
            (run_id, status, notes, metrics_json, tested_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status=excluded.status,
            notes=excluded.notes,
            metrics_json=excluded.metrics_json,
            tested_at=excluded.tested_at
        """,
        (
            run_id,
            status,
            notes[:2000],
            json.dumps(metrics or {}, ensure_ascii=False),
            now(),
        ),
    )
    conn.commit()
    return get_model_validation(conn, run_id) or {}


def get_model_validation(
    conn: sqlite3.Connection, run_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM model_validations WHERE run_id = ?', (run_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['metrics_json'] = json.loads(result['metrics_json'] or '{}')
    return result


def create_model_package(
    conn: sqlite3.Connection,
    *,
    package_id: str,
    status: str,
    path: str,
    manifest: Dict[str, Any],
) -> None:
    if status not in {'incomplete', 'ready'}:
        raise ValueError(f'未知模型包状态: {status}')
    conn.execute(
        'INSERT INTO model_packages '
        '(id, status, path, manifest_json, created_at) VALUES (?, ?, ?, ?, ?)',
        (package_id, status, path, json.dumps(manifest, ensure_ascii=False), now()),
    )
    conn.commit()


def list_model_packages(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM model_packages ORDER BY created_at DESC, id DESC'
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['manifest_json'] = json.loads(item['manifest_json'] or '{}')
        result.append(item)
    return result


_MODEL_DEPLOYMENT_STATUSES = {'queued', 'running', 'succeeded', 'failed'}
_MODEL_DEPLOYMENT_TRANSITIONS = {
    'queued': {'running', 'failed'},
    'running': {'succeeded', 'failed'},
    'succeeded': set(),
    'failed': set(),
}


def _model_deployment_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item['detail_json'] = json.loads(item['detail_json'] or '{}')
    return item


def create_model_deployment(
    conn: sqlite3.Connection, *, package_id: str, target: str
) -> Dict[str, Any]:
    package = conn.execute(
        'SELECT status FROM model_packages WHERE id = ?', (package_id,)
    ).fetchone()
    if package is None:
        raise KeyError(f'模型包不存在: {package_id}')
    if str(package['status']) != 'ready':
        raise ValueError('模型包尚未达到发布条件')
    active = conn.execute(
        "SELECT id FROM model_deployments WHERE target = ? "
        "AND status IN ('queued', 'running')",
        (target,),
    ).fetchone()
    if active is not None:
        raise ValueError('这个 Worker 正在部署另一个模型包')
    cursor = conn.execute(
        'INSERT INTO model_deployments '
        '(package_id, target, status, created_at) VALUES (?, ?, ?, ?)',
        (package_id, target, 'queued', now()),
    )
    conn.commit()
    return get_model_deployment(conn, int(cursor.lastrowid)) or {}


def get_model_deployment(
    conn: sqlite3.Connection, deployment_id: int
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM model_deployments WHERE id = ?', (deployment_id,)
    ).fetchone()
    return None if row is None else _model_deployment_dict(row)


def update_model_deployment(
    conn: sqlite3.Connection,
    *,
    deployment_id: int,
    status: str,
    previous_package_id: Optional[str] = None,
    worker_package_id: Optional[str] = None,
    error: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if status not in _MODEL_DEPLOYMENT_STATUSES:
        raise ValueError(f'未知模型部署状态: {status}')
    current = get_model_deployment(conn, deployment_id)
    if current is None:
        raise KeyError(f'模型部署记录不存在: {deployment_id}')
    if status not in _MODEL_DEPLOYMENT_TRANSITIONS[str(current['status'])]:
        raise ValueError(
            '模型部署状态不能从 {} 变为 {}'.format(current['status'], status)
        )
    timestamp = now()
    conn.execute(
        'UPDATE model_deployments SET status = ?, '
        'previous_package_id = COALESCE(?, previous_package_id), '
        'worker_package_id = COALESCE(?, worker_package_id), '
        'error = COALESCE(?, error), '
        'detail_json = COALESCE(?, detail_json), '
        'started_at = CASE WHEN ? = \'running\' THEN ? ELSE started_at END, '
        "finished_at = CASE WHEN ? IN ('succeeded', 'failed') "
        'THEN ? ELSE finished_at END WHERE id = ?',
        (
            status,
            previous_package_id,
            worker_package_id,
            None if error is None else error[:4000],
            None if detail is None else json.dumps(detail, ensure_ascii=False),
            status,
            timestamp,
            status,
            timestamp,
            deployment_id,
        ),
    )
    conn.commit()
    return get_model_deployment(conn, deployment_id) or {}


def list_model_deployments(
    conn: sqlite3.Connection, *, limit: int = 20
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM model_deployments ORDER BY id DESC LIMIT ?',
        (max(1, min(200, int(limit))),),
    ).fetchall()
    return [_model_deployment_dict(row) for row in rows]


def fail_interrupted_model_deployments(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE model_deployments SET status = 'failed', "
        "error = '标注服务重启，无法确认部署是否完成', finished_at = ? "
        "WHERE status IN ('queued', 'running')",
        (now(),),
    )
    conn.commit()
    return int(cursor.rowcount)


def audit_recent(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- 实时打标进度 ----------


def save_live_state(
    conn: sqlite3.Connection,
    *,
    queue: List[int],
    queue_index: int,
    video_id: Optional[int],
    last_pts_ms: Optional[int],
    last_frame_id: Optional[int],
) -> None:
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
        (json.dumps(queue), queue_index, video_id, last_pts_ms, last_frame_id, now()),
    )
    conn.commit()


def load_live_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute('SELECT * FROM live_state WHERE id = 1').fetchone()
    if not row:
        return {
            'queue': [],
            'queue_index': 0,
            'video_id': None,
            'last_pts_ms': None,
            'last_frame_id': None,
        }
    d = dict(row)
    d['queue'] = json.loads(d.pop('queue_json') or '[]')
    return d


# ---------- 每视频实时打标进度 ----------


def save_video_progress(
    conn: sqlite3.Connection,
    video_id: int,
    *,
    last_pts_ms: Optional[int],
    last_frame_id: Optional[int],
) -> None:
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


def load_video_progress(conn: sqlite3.Connection, video_id: int) -> Dict[str, Any]:
    row = conn.execute(
        'SELECT * FROM live_video_progress WHERE video_id = ?', (video_id,)
    ).fetchone()
    return (
        dict(row)
        if row
        else {'video_id': video_id, 'last_pts_ms': None, 'last_frame_id': None}
    )


def all_video_progress(conn: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    rows = conn.execute('SELECT * FROM live_video_progress').fetchall()
    return {r['video_id']: dict(r) for r in rows}


# ---------- 3V3 / 大乱斗光栅专项 ----------

MODE_GATE_EVIDENCE = {'blocked_gate', 'open_entrance', 'no_evidence'}


def save_mode_gate_round(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    name: str,
    description: str = '',
    active: bool = True,
) -> None:
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


def add_mode_gate_round_video(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    video_id: int,
    expected_mode: str,
    start_ms: int = 0,
    sort_order: int = 0,
    notes: str = '',
) -> None:
    if expected_mode not in {'aram', '3v3'}:
        raise ValueError('expected_mode 必须是 aram 或 3v3')
    if not conn.execute(
        'SELECT 1 FROM mode_gate_rounds WHERE id = ?', (round_id,)
    ).fetchone():
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


def get_mode_gate_round(
    conn: sqlite3.Connection, round_id: str
) -> Optional[Dict[str, Any]]:
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
        int(video['annotation_count']) for video in result['videos']
    )
    return result


def get_active_mode_gate_round(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT id FROM mode_gate_rounds WHERE active = 1 '
        'ORDER BY created_at DESC LIMIT 1'
    ).fetchone()
    return get_mode_gate_round(conn, row['id']) if row else None


def get_mode_gate_annotation(
    conn: sqlite3.Connection, *, round_id: str, frame_id: int
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM mode_gate_annotations ' 'WHERE round_id = ? AND frame_id = ?',
        (round_id, frame_id),
    ).fetchone()
    if not row:
        return None
    annotation = dict(row)
    annotation['boxes'] = [
        dict(box)
        for box in conn.execute(
            'SELECT id, x, y, w, h FROM mode_gate_boxes '
            'WHERE round_id = ? AND frame_id = ? ORDER BY sort_order, id',
            (round_id, frame_id),
        ).fetchall()
    ]
    return annotation


def _validate_mode_gate_boxes(
    evidence: str,
    boxes: Optional[List[Dict[str, Any]]],
    *,
    x: Optional[float],
    y: Optional[float],
    w: Optional[float],
    h: Optional[float],
) -> List[Dict[str, float]]:
    if boxes is None:
        legacy_coords = (x, y, w, h)
        boxes = (
            []
            if all(value is None for value in legacy_coords)
            else [{'x': x, 'y': y, 'w': w, 'h': h}]
        )
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
        bx, by, bw, bh = (coords['x'], coords['y'], coords['w'], coords['h'])
        if not (
            0 <= bx <= 1
            and 0 <= by <= 1
            and 0 < bw <= 1
            and 0 < bh <= 1
            and bx + bw <= 1.001
            and by + bh <= 1.001
        ):
            raise ValueError('框坐标必须归一化到 [0,1]')
        normalized.append(coords)

    if evidence == 'no_evidence':
        if normalized:
            raise ValueError('no_evidence 不能带边界框')
    elif not normalized:
        raise ValueError('光栅或开放入口必须至少有一个边界框')
    return normalized


def save_mode_gate_annotation(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    frame_id: int,
    evidence: str,
    boxes: Optional[List[Dict[str, Any]]] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    notes: str = '',
) -> Dict[str, Any]:
    if evidence not in MODE_GATE_EVIDENCE:
        raise ValueError('未知的光栅证据类型')
    normalized_boxes = _validate_mode_gate_boxes(evidence, boxes, x=x, y=y, w=w, h=h)
    frame = conn.execute(
        'SELECT video_id, timestamp_ms FROM frames WHERE id = ?', (frame_id,)
    ).fetchone()
    if not frame:
        raise KeyError(f'帧不存在: {frame_id}')
    member = conn.execute(
        'SELECT 1 FROM mode_gate_round_videos ' 'WHERE round_id = ? AND video_id = ?',
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
            (
                round_id,
                frame_id,
                evidence,
                first.get('x'),
                first.get('y'),
                first.get('w'),
                first.get('h'),
                notes,
                updated_at,
            ),
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
                (
                    round_id,
                    frame_id,
                    index,
                    box['x'],
                    box['y'],
                    box['w'],
                    box['h'],
                    updated_at,
                )
                for index, box in enumerate(normalized_boxes)
            ],
        )
        conn.execute(
            'UPDATE mode_gate_round_videos '
            'SET last_pts_ms = ?, last_frame_id = ?, updated_at = ? '
            'WHERE round_id = ? AND video_id = ?',
            (frame['timestamp_ms'], frame_id, updated_at, round_id, frame['video_id']),
        )
    annotation = get_mode_gate_annotation(conn, round_id=round_id, frame_id=frame_id)
    assert annotation is not None
    return annotation


def delete_mode_gate_annotation(
    conn: sqlite3.Connection, *, round_id: str, frame_id: int
) -> None:
    with conn:
        conn.execute(
            'DELETE FROM mode_gate_boxes WHERE round_id = ? AND frame_id = ?',
            (round_id, frame_id),
        )
        conn.execute(
            'DELETE FROM mode_gate_annotations ' 'WHERE round_id = ? AND frame_id = ?',
            (round_id, frame_id),
        )


def list_mode_gate_frames(
    conn: sqlite3.Connection, *, round_id: str, video_id: int
) -> List[Dict[str, Any]]:
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
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    model_version: str,
    suggested_label: str,
    suggestion_confidence: float,
    stage_class: str,
    stage_confidence: float,
    pre_match_confidence: float,
    mode_class: str,
    mode_confidence: float,
    mode_margin: float,
    selection_reason: str,
    priority: float,
    raw_prediction: Dict[str, Any],
) -> bool:
    """写入一个待复核候选；已经人工处理的帧只更新模型信息，不退回队列。"""
    if suggested_label not in BP_REVIEW_LABELS:
        raise ValueError(f'未知 BP 建议标签: {suggested_label}')
    if not conn.execute('SELECT 1 FROM frames WHERE id = ?', (frame_id,)).fetchone():
        raise KeyError(f'帧不存在: {frame_id}')
    existing = conn.execute(
        'SELECT review_status FROM bp_review_items WHERE frame_id = ?', (frame_id,)
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
            frame_id,
            model_version,
            suggested_label,
            max(0.0, min(1.0, float(suggestion_confidence))),
            stage_class,
            max(0.0, min(1.0, float(stage_confidence))),
            max(0.0, min(1.0, float(pre_match_confidence))),
            mode_class,
            max(0.0, min(1.0, float(mode_confidence))),
            max(0.0, min(1.0, float(mode_margin))),
            selection_reason,
            float(priority),
            json.dumps(raw_prediction, ensure_ascii=False),
            created_at,
        ),
    )
    conn.commit()
    return existing is None


def list_bp_review_items(
    conn: sqlite3.Connection,
    *,
    status: str = 'pending',
    limit: int = 500,
    offset: int = 0,
) -> List[Dict[str, Any]]:
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
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    label: Optional[str],
    visual_condition: str = 'clear',
) -> Dict[str, Any]:
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
        conn,
        'bp_review',
        frame_id=frame_id,
        detail=json.dumps(
            {'label': label, 'status': status, 'visual_condition': visual_condition},
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
            row['game_mode']: {'frames': row['count'], 'videos': row['videos']}
            for row in existing_rows
        },
    }


# ---------- 结算页 / 计分板主动学习复核 ----------

KEY_SCREEN_REVIEW_LABELS = {'result_page', 'scoreboard', 'other'}
KEY_SCREEN_REVIEW_STATUSES = {'pending', 'confirmed', 'skipped'}
KEY_SCREEN_VISUAL_CONDITIONS = {'clear', 'occluded', 'unreadable'}


def upsert_key_screen_review_item(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    model_version: str,
    suggested_label: str,
    suggestion_confidence: float,
    selection_reason: str,
    raw_prediction: Dict[str, Any],
    priority: float = 0,
) -> bool:
    """写入关键画面预标；已人工处理的帧不会退回待确认。"""
    if suggested_label not in KEY_SCREEN_REVIEW_LABELS:
        raise ValueError(f'未知关键画面建议标签: {suggested_label}')
    if not conn.execute('SELECT 1 FROM frames WHERE id = ?', (frame_id,)).fetchone():
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
    conn: sqlite3.Connection,
    *,
    status: str = 'pending',
    limit: int = 500,
    offset: int = 0,
) -> List[Dict[str, Any]]:
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
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    label: Optional[str],
    visual_condition: str = 'clear',
) -> Dict[str, Any]:
    if label is not None and label not in KEY_SCREEN_REVIEW_LABELS:
        raise ValueError(f'未知关键画面确认标签: {label}')
    if visual_condition not in KEY_SCREEN_VISUAL_CONDITIONS:
        raise ValueError(f'未知关键画面画质情况: {visual_condition}')
    if label is None:
        visual_condition = 'clear'
    if not conn.execute(
        'SELECT 1 FROM key_screen_review_items WHERE frame_id = ?', (frame_id,)
    ).fetchone():
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
            {'label': label, 'status': status, 'visual_condition': visual_condition},
            ensure_ascii=False,
        ),
    )
    items = list_key_screen_review_items(conn, status=status, limit=100_000)
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


# ---------- Worker 训练候选与 NAS 双向复核 ----------

WORKER_CANDIDATE_LABELS = {
    'screen_state': {
        'not_vainglory',
        'out_of_match',
        'pre_match',
        'in_match',
        'talent_select',
        'post_match',
        'transition',
    },
    'bp_review': BP_REVIEW_LABELS,
    'key_screen_review': KEY_SCREEN_REVIEW_LABELS,
    'result_detector': {'result_panel', 'no_result_panel'},
    'mode_gate': {'blocked_gate', 'open_entrance', 'no_evidence'},
}
WORKER_CANDIDATE_STATUSES = {'pending', 'confirmed', 'skipped', 'conflict'}
WORKER_CANDIDATE_SYNC_STATES = {'clean', 'dirty', 'conflict'}
WORKER_VISUAL_CONDITIONS = {
    'clear',
    'occluded',
    'windowed',
    'occluded_windowed',
    'unreadable',
}


def normalize_candidate_boxes(boxes: Any) -> List[Dict[str, Any]]:
    if boxes is None:
        return []
    if not isinstance(boxes, list):
        raise ValueError('boxes 必须是边界框数组')
    normalized = []
    for raw in boxes:
        if not isinstance(raw, dict):
            raise ValueError('每个边界框必须是对象')
        try:
            values = {name: float(raw[name]) for name in ('x', 'y', 'w', 'h')}
        except (KeyError, TypeError, ValueError):
            raise ValueError('每个边界框都必须包含数字 x/y/w/h')
        if not (
            0 <= values['x'] <= 1
            and 0 <= values['y'] <= 1
            and 0 < values['w'] <= 1
            and 0 < values['h'] <= 1
            and values['x'] + values['w'] <= 1.001
            and values['y'] + values['h'] <= 1.001
        ):
            raise ValueError('框坐标必须归一化到 [0,1]')
        box_type = str(raw.get('type') or raw.get('box_type') or '')[:80]
        normalized.append({**values, 'type': box_type})
    return normalized


def upsert_worker_candidate(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    frame_id: int,
    task: str,
    schema_version: int,
    image_path: str,
    image_sha256: str,
    suggested_label: str,
    suggestion_confidence: float,
    suggested_boxes: List[Dict[str, Any]],
    raw_metadata: Dict[str, Any],
    candidate_created_at: int,
) -> bool:
    if task not in WORKER_CANDIDATE_LABELS:
        raise ValueError(f'未知 worker 候选任务: {task}')
    if suggested_label not in WORKER_CANDIDATE_LABELS[task]:
        raise ValueError(f'未知 worker 建议标签: {suggested_label}')
    if not conn.execute('SELECT 1 FROM frames WHERE id = ?', (frame_id,)).fetchone():
        raise KeyError(f'帧不存在: {frame_id}')
    existing = conn.execute(
        'SELECT id FROM worker_candidate_items WHERE source_id = ?', (source_id,)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO worker_candidate_items
            (source_id, frame_id, task, schema_version, image_path,
             image_sha256, suggested_label, suggestion_confidence,
             suggested_boxes_json, raw_metadata_json, candidate_created_at,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            frame_id=excluded.frame_id,
            task=excluded.task,
            schema_version=excluded.schema_version,
            image_path=excluded.image_path,
            image_sha256=excluded.image_sha256,
            suggested_label=excluded.suggested_label,
            suggestion_confidence=excluded.suggestion_confidence,
            suggested_boxes_json=excluded.suggested_boxes_json,
            raw_metadata_json=excluded.raw_metadata_json,
            candidate_created_at=excluded.candidate_created_at
        """,
        (
            source_id,
            frame_id,
            task,
            int(schema_version),
            image_path,
            image_sha256,
            suggested_label,
            max(0.0, min(1.0, float(suggestion_confidence))),
            json.dumps(normalize_candidate_boxes(suggested_boxes), ensure_ascii=False),
            json.dumps(raw_metadata, ensure_ascii=False),
            max(0, int(candidate_created_at)),
            now(),
        ),
    )
    conn.commit()
    return existing is None


def _worker_candidate_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item['suggested_boxes'] = json.loads(item.pop('suggested_boxes_json') or '[]')
    item['boxes'] = json.loads(item.pop('boxes_json') or '[]')
    item['raw_metadata'] = json.loads(item.pop('raw_metadata_json') or '{}')
    return item


def get_worker_candidate(
    conn: sqlite3.Connection, candidate_id: int
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT c.*, f.video_id, f.timestamp_ms, f.width, f.height,
               f.frame_path, f.thumb_path, v.streamer, v.filename
        FROM worker_candidate_items c
        JOIN frames f ON f.id = c.frame_id
        JOIN videos v ON v.id = f.video_id
        WHERE c.id = ?
        """,
        (int(candidate_id),),
    ).fetchone()
    return _worker_candidate_dict(row) if row else None


def get_worker_candidate_by_source(
    conn: sqlite3.Connection, source_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT id FROM worker_candidate_items WHERE source_id = ?', (source_id,)
    ).fetchone()
    return get_worker_candidate(conn, int(row['id'])) if row else None


def list_worker_candidates(
    conn: sqlite3.Connection,
    *,
    task: str = '',
    status: str = 'pending',
    limit: int = 500,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    if task and task not in WORKER_CANDIDATE_LABELS:
        raise ValueError(f'未知 worker 候选任务: {task}')
    if status != 'all' and status not in WORKER_CANDIDATE_STATUSES:
        raise ValueError(f'未知 worker 候选状态: {status}')
    where = []
    args: List[Any] = []
    if task:
        where.append('c.task = ?')
        args.append(task)
    if status == 'conflict':
        where.append("c.sync_state = 'conflict'")
    elif status != 'all':
        where.append('c.review_status = ?')
        args.append(status)
    clause = 'WHERE ' + ' AND '.join(where) if where else ''
    args.extend((max(1, min(2_000, int(limit))), max(0, int(offset))))
    rows = conn.execute(
        f"""
        SELECT c.*, f.video_id, f.timestamp_ms, f.width, f.height,
               f.frame_path, f.thumb_path, v.streamer, v.filename
        FROM worker_candidate_items c
        JOIN frames f ON f.id = c.frame_id
        JOIN videos v ON v.id = f.video_id
        {clause}
        ORDER BY
            CASE c.review_status WHEN 'conflict' THEN 0 WHEN 'pending' THEN 1
                 ELSE 2 END,
            c.candidate_created_at DESC, c.id DESC
        LIMIT ? OFFSET ?
        """,
        args,
    ).fetchall()
    return [_worker_candidate_dict(row) for row in rows]


def review_worker_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    label: Optional[str],
    visual_condition: str = 'clear',
    boxes: Optional[List[Dict[str, Any]]] = None,
    notes: str = '',
    reviewed_at: Optional[str] = None,
    sync_state: str = 'dirty',
    remote_review_hash: str = '',
) -> Dict[str, Any]:
    item = get_worker_candidate(conn, candidate_id)
    if item is None:
        raise KeyError(f'worker 候选不存在: {candidate_id}')
    task = str(item['task'])
    if label is not None and label not in WORKER_CANDIDATE_LABELS[task]:
        raise ValueError(f'未知 {task} 确认标签: {label}')
    if visual_condition not in WORKER_VISUAL_CONDITIONS:
        raise ValueError(f'未知画面情况: {visual_condition}')
    if sync_state not in WORKER_CANDIDATE_SYNC_STATES:
        raise ValueError(f'未知同步状态: {sync_state}')
    normalized_boxes = normalize_candidate_boxes(boxes)
    if label in {'result_panel', 'blocked_gate', 'open_entrance'}:
        if not normalized_boxes:
            raise ValueError('这个标签必须至少画一个框')
    elif normalized_boxes:
        raise ValueError('当前标签不能带边界框')
    if label is None:
        visual_condition = 'clear'
        status = 'skipped'
    else:
        status = 'confirmed'
    timestamp = reviewed_at or now()
    conn.execute(
        'UPDATE worker_candidate_items SET review_status = ?, '
        'confirmed_label = ?, visual_condition = ?, boxes_json = ?, notes = ?, '
        'reviewed_at = ?, remote_reviewed_at = CASE WHEN ? = \'clean\' '
        'THEN ? ELSE remote_reviewed_at END, sync_state = ?, '
        'remote_review_hash = ? WHERE id = ?',
        (
            status,
            label,
            visual_condition,
            json.dumps(normalized_boxes, ensure_ascii=False),
            notes[:1000],
            timestamp,
            sync_state,
            timestamp,
            sync_state,
            remote_review_hash,
            int(candidate_id),
        ),
    )
    audit(
        conn,
        'worker_candidate_review',
        frame_id=int(item['frame_id']),
        detail=json.dumps(
            {
                'source_id': item['source_id'],
                'task': task,
                'label': label,
                'status': status,
                'visual_condition': visual_condition,
                'boxes': normalized_boxes,
                'sync_state': sync_state,
            },
            ensure_ascii=False,
        ),
    )
    updated = get_worker_candidate(conn, candidate_id)
    assert updated is not None
    return updated


def mark_worker_candidate_review_for_frame(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    task: str,
    label: Optional[str],
    visual_condition: str = 'clear',
) -> int:
    """兼容旧 BP/关键界面复核页：把人工结果同时标成待回传 NAS。"""
    rows = conn.execute(
        'SELECT id FROM worker_candidate_items WHERE frame_id = ? AND task = ?',
        (int(frame_id), task),
    ).fetchall()
    for row in rows:
        review_worker_candidate(
            conn,
            candidate_id=int(row['id']),
            label=label,
            visual_condition=visual_condition,
        )
    return len(rows)


def worker_candidate_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    by_status = {
        row['review_status']: int(row['count'])
        for row in conn.execute(
            'SELECT review_status, COUNT(*) AS count '
            'FROM worker_candidate_items GROUP BY review_status'
        ).fetchall()
    }
    by_task = {
        row['task']: {
            'total': int(row['total']),
            'pending': int(row['pending']),
            'confirmed': int(row['confirmed']),
        }
        for row in conn.execute(
            """
            SELECT task, COUNT(*) AS total,
                   SUM(review_status = 'pending') AS pending,
                   SUM(review_status = 'confirmed') AS confirmed
            FROM worker_candidate_items GROUP BY task
            """
        ).fetchall()
    }
    dirty = int(
        conn.execute(
            "SELECT COUNT(*) FROM worker_candidate_items WHERE sync_state = 'dirty'"
        ).fetchone()[0]
    )
    conflicts = int(
        conn.execute(
            "SELECT COUNT(*) FROM worker_candidate_items "
            "WHERE sync_state = 'conflict' OR review_status = 'conflict'"
        ).fetchone()[0]
    )
    return {
        'total': sum(by_status.values()),
        'statuses': by_status,
        'by_task': by_task,
        'dirty': dirty,
        'conflicts': conflicts,
    }


# ---------- 新模型共用复核 ----------

_TRAINING_REVIEW_LABELS = {
    'match_flow': {'match_flow', 'not_match_flow', 'unreadable'},
    'match_mode': {'3v3', 'aram', '5v5', 'unreadable'},
    'hero_select': {
        'not_select',
        'select_3v3',
        'select_aram',
        'select_5v5',
        'unreadable',
    },
    'result_panel': {'result_panel', 'no_result_panel', 'unreadable'},
}
_TRAINING_REVIEW_STATUSES = {'pending', 'partial', 'confirmed', 'skipped'}
_TRAINING_REVIEW_SOURCE_SCOPES = {'all', 'new', 'legacy'}
_HERO_SCREEN_TYPES = {'gameplay_hud', 'scoreboard', 'result_page'}
_HERO_LAYOUT_LABELS = _HERO_SCREEN_TYPES | {'none', 'unreadable'}
_HERO_SELECT_VARIANTS = {'bp', 'blind', 'random', 'unreadable'}
_MISSING_PLAYER_HERO_REVIEW = """
item.review_status = 'confirmed'
AND (
    (
        item.result_panel_label = 'result_panel'
        AND (
            COALESCE(item.hero_layout_label, '') != 'result_page'
            OR NOT EXISTS (
                SELECT 1
                FROM training_review_hero_lineups lineup
                WHERE lineup.frame_id = item.frame_id
                  AND lineup.screen_type = 'result_page'
                  AND lineup.review_status = 'confirmed'
                  AND (
                      lineup.player_status = 'unreadable'
                      OR (
                          lineup.player_status = 'identified'
                          AND lineup.player_side IN ('left', 'right')
                          AND lineup.player_slot BETWEEN 1 AND lineup.team_size
                      )
                  )
            )
        )
    )
    OR (
        item.hero_layout_label = 'scoreboard'
        AND NOT EXISTS (
            SELECT 1
            FROM training_review_hero_lineups lineup
            WHERE lineup.frame_id = item.frame_id
              AND lineup.screen_type = 'scoreboard'
              AND lineup.review_status = 'confirmed'
              AND (
                  lineup.player_status = 'unreadable'
                  OR (
                      lineup.player_status = 'identified'
                      AND lineup.player_side IN ('left', 'right')
                      AND lineup.player_slot BETWEEN 1 AND lineup.team_size
                  )
              )
        )
    )
)
"""
_UNIFIED_MANUAL_REVIEWED = """
EXISTS (
    SELECT 1
    FROM audit_log manual_review
    WHERE manual_review.frame_id = item.frame_id
      AND manual_review.action = 'training_review'
)
"""
_TRAINING_REVIEW_ARAM_PRIORITY = """
CASE
    WHEN EXISTS (
        SELECT 1
        FROM training_review_sources source
        WHERE source.frame_id = item.frame_id
          AND json_extract(
              source.suggestions_json, '$.hero_select.label'
          ) = 'select_aram'
    ) THEN 0
    WHEN EXISTS (
        SELECT 1
        FROM training_review_sources source
        WHERE source.frame_id = item.frame_id
          AND (
              json_extract(
                  source.suggestions_json, '$.match_mode.label'
              ) = 'aram'
              OR json_extract(source.metadata_json, '$.game_mode') = 'aram'
              OR json_extract(source.metadata_json, '$.mode_class') = 'aram'
              OR json_extract(
                  source.metadata_json, '$.model_outputs[0].mode_class'
              ) = 'aram'
          )
    ) THEN 1
    WHEN EXISTS (
        SELECT 1
        FROM frames current_frame
        JOIN frames known_frame
          ON known_frame.video_id = current_frame.video_id
        JOIN training_review_items known
          ON known.frame_id = known_frame.id
        WHERE current_frame.id = item.frame_id
          AND known.review_status = 'confirmed'
          AND (
              known.match_mode_label = 'aram'
              OR known.hero_select_label = 'select_aram'
          )
    ) THEN 2
    ELSE 3
END
"""
_TRAINING_REVIEW_SOURCE_CREATED_AT = """
COALESCE((
    SELECT MAX(source.source_created_at)
    FROM training_review_sources source
    WHERE source.frame_id = item.frame_id
), 0)
"""
_TRAINING_REVIEW_SOURCE_OFFSET = """
COALESCE((
    SELECT MAX(COALESCE(
        CAST(json_extract(source.metadata_json, '$.at_ms') AS INTEGER),
        CAST(json_extract(source.metadata_json, '$.result_at_ms') AS INTEGER),
        0
    ))
    FROM training_review_sources source
    WHERE source.frame_id = item.frame_id
), 0)
"""


def validate_training_suggestions(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError('模型建议必须是对象')
    result: Dict[str, Dict[str, Any]] = {}
    for task, raw in value.items():
        if task not in _TRAINING_REVIEW_LABELS or not isinstance(raw, dict):
            raise ValueError('模型建议任务无效')
        label = str(raw.get('label') or '')
        if label not in _TRAINING_REVIEW_LABELS[task]:
            raise ValueError('模型建议标签无效')
        confidence = float(raw.get('confidence', 0))
        if not 0 <= confidence <= 1:
            raise ValueError('模型建议置信度必须在 0 到 1 之间')
        result[task] = {**raw, 'label': label, 'confidence': confidence}
    return result


def add_training_review_source(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    source_type: str,
    source_id: str,
    suggestions: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    image_path: str = '',
    source_created_at: int = 0,
) -> bool:
    """增加一条来源/预标；不会改写任何人工标签。"""
    if not conn.execute(
        'SELECT 1 FROM frames WHERE id = ?', (int(frame_id),)
    ).fetchone():
        raise KeyError(frame_id)
    normalized_type = source_type.strip()[:80]
    normalized_id = source_id.strip()[:300]
    if not normalized_type or not normalized_id:
        raise ValueError('训练复核来源无效')
    normalized_suggestions = validate_training_suggestions(suggestions or {})
    timestamp = now()
    existing = conn.execute(
        'SELECT id FROM training_review_sources '
        'WHERE source_type = ? AND source_id = ?',
        (normalized_type, normalized_id),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO training_review_items
            (frame_id, review_status, created_at, updated_at)
        VALUES (?, 'pending', ?, ?)
        ON CONFLICT(frame_id) DO NOTHING
        """,
        (int(frame_id), timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO training_review_sources
            (frame_id, source_type, source_id, image_path, suggestions_json,
             metadata_json, source_created_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            frame_id=excluded.frame_id,
            image_path=excluded.image_path,
            suggestions_json=excluded.suggestions_json,
            metadata_json=excluded.metadata_json,
            source_created_at=excluded.source_created_at,
            updated_at=excluded.updated_at
        """,
        (
            int(frame_id),
            normalized_type,
            normalized_id,
            image_path[:500],
            json.dumps(
                normalized_suggestions,
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            ),
            json.dumps(
                metadata or {},
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            ),
            max(0, int(source_created_at)),
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    return existing is None


def _training_review_item_dict(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> Dict[str, Any]:
    item = dict(row)
    try:
        occluder_types = json.loads(str(item.get('occluder_types') or '[]'))
    except (TypeError, ValueError, json.JSONDecodeError):
        occluder_types = []
    item['occluder_types'] = (
        [str(value) for value in occluder_types]
        if isinstance(occluder_types, list)
        else []
    )
    source_rows = conn.execute(
        'SELECT id, source_type, source_id, image_path, suggestions_json, '
        'metadata_json, source_created_at, sync_state, remote_reviewed_at '
        'FROM training_review_sources WHERE frame_id = ? '
        'ORDER BY source_created_at DESC, id DESC',
        (int(row['frame_id']),),
    ).fetchall()
    suggestions: Dict[str, Dict[str, Any]] = {}
    suggestion_ranks: Dict[str, tuple[int, float]] = {}
    sources = []
    for source_row in source_rows:
        source = dict(source_row)
        source_suggestions = json.loads(source.pop('suggestions_json') or '{}')
        source['metadata'] = json.loads(source.pop('metadata_json') or '{}')
        source['suggestions'] = source_suggestions
        sources.append(source)
        for task, suggestion in source_suggestions.items():
            origin = str(suggestion.get('origin') or '')
            source_priority = (
                2
                if source['source_type'] == 'manual_correction'
                else int(
                    source['source_type'] == 'new_model_prefill'
                    or origin in {'new_model_prefill', 'model_package'}
                )
            )
            rank = (source_priority, float(suggestion.get('confidence', 0)))
            if rank > suggestion_ranks.get(task, (-1, -1.0)):
                suggestions[task] = dict(suggestion)
                suggestion_ranks[task] = rank
    item['suggestions'] = suggestions
    item['sources'] = sources
    item['source_count'] = len(sources)
    item['source_categories'] = sorted(
        {_training_review_source_category(source['source_type']) for source in sources}
    )
    item['boxes'] = get_boxes(conn, int(row['frame_id']))
    item['needs_player_hero_review'] = bool(item['needs_player_hero_review'])
    item['unified_manual_reviewed'] = bool(item['unified_manual_reviewed'])
    item['legacy_migration_needs_review'] = bool(
        item['review_status'] == 'confirmed'
        and 'legacy' in item['source_categories']
        and not item['unified_manual_reviewed']
    )
    return item


def training_review_result_groups(
    conn: sqlite3.Connection,
) -> Dict[int, Dict[str, Any]]:
    """把同一局的多张结算候选折叠为一个复核／训练代表图。"""
    rows = conn.execute(
        """
        SELECT item.frame_id, item.review_status, item.result_panel_label,
               item.hero_layout_label, item.panel_render_state, item.ocr_usable,
               item.result_occlusion, frame.event_id, frame.timestamp_ms,
               frame.is_representative, frame.model_confidence,
               EXISTS (
                   SELECT 1 FROM boxes box
                   WHERE box.frame_id = item.frame_id
                     AND box.box_type = 'result_panel'
               ) AS has_result_box,
               EXISTS (
                   SELECT 1 FROM training_review_hero_lineups lineup
                   WHERE lineup.frame_id = item.frame_id
                     AND lineup.review_status = 'confirmed'
                     AND lineup.player_status = 'identified'
                     AND lineup.player_side IN ('left', 'right')
                     AND lineup.player_slot BETWEEN 1 AND lineup.team_size
               ) AS has_complete_lineup
        FROM training_review_items item
        JOIN frames frame ON frame.id = item.frame_id
        """
    ).fetchall()
    if not rows:
        return {}
    by_frame = {int(row['frame_id']): row for row in rows}
    positive = {
        frame_id
        for frame_id, row in by_frame.items()
        if row['result_panel_label'] == 'result_panel'
    }
    sources_by_frame: Dict[int, List[Tuple[str, Dict[str, Any]]]] = {}
    for source in conn.execute(
        'SELECT frame_id, source_type, suggestions_json, metadata_json '
        'FROM training_review_sources'
    ).fetchall():
        frame_id = int(source['frame_id'])
        if frame_id not in by_frame:
            continue
        try:
            suggestions = json.loads(source['suggestions_json'] or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            suggestions = {}
        suggestion = suggestions.get('result_panel')
        if (
            by_frame[frame_id]['result_panel_label'] is None
            and isinstance(suggestion, dict)
            and suggestion.get('label') == 'result_panel'
        ):
            positive.add(frame_id)
        try:
            metadata = json.loads(source['metadata_json'] or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            sources_by_frame.setdefault(frame_id, []).append(
                (str(source['source_type']), metadata)
            )
    if not positive:
        return {}

    parent = {frame_id: frame_id for frame_id in positive}

    def find(frame_id: int) -> int:
        while parent[frame_id] != frame_id:
            parent[frame_id] = parent[parent[frame_id]]
            frame_id = parent[frame_id]
        return frame_id

    def union(frame_ids: Sequence[int]) -> None:
        ids = [frame_id for frame_id in frame_ids if frame_id in parent]
        if len(ids) < 2:
            return
        root = find(ids[0])
        for frame_id in ids[1:]:
            other = find(frame_id)
            if other != root:
                parent[other] = root

    event_groups: Dict[int, List[int]] = {}
    for frame_id in positive:
        event_id = by_frame[frame_id]['event_id']
        if event_id is not None:
            event_groups.setdefault(int(event_id), []).append(frame_id)
    for frame_ids in event_groups.values():
        union(frame_ids)

    worker_groups: Dict[Tuple[int, int, int], List[int]] = {}
    archive_rows: Dict[Tuple[int, int], List[Tuple[int, int, bool]]] = {}
    for frame_id in positive:
        for source_type, metadata in sources_by_frame.get(frame_id, []):
            if source_type == 'worker':
                try:
                    worker_key = (
                        int(metadata.get('session_id') or 0),
                        int(metadata.get('part_id') or 0),
                        int(metadata.get('segment_start_ms')),
                    )
                except (TypeError, ValueError):
                    continue
                worker_groups.setdefault(worker_key, []).append(frame_id)
            elif source_type == 'result_archive':
                try:
                    session_id = int(metadata.get('session_id') or 0)
                    part_id = int(metadata.get('part_id') or 0)
                    result_at_ms = int(metadata.get('result_at_ms'))
                    duration_seconds = int(metadata.get('duration_seconds') or 0)
                    started_at = metadata.get('started_at_ms')
                    if started_at is not None:
                        estimated_start = int(started_at)
                        has_start = True
                    elif duration_seconds > 0:
                        estimated_start = max(
                            0, result_at_ms - duration_seconds * 1_000
                        )
                        has_start = True
                    else:
                        estimated_start = result_at_ms
                        has_start = False
                except (TypeError, ValueError):
                    continue
                archive_rows.setdefault((session_id, part_id), []).append(
                    (estimated_start, frame_id, has_start)
                )
    for frame_ids in worker_groups.values():
        union(frame_ids)
    for candidates in archive_rows.values():
        for has_start, maximum_gap in ((True, 90_000), (False, 5_000)):
            clusters: List[Tuple[int, List[int]]] = []
            for value, frame_id, candidate_has_start in sorted(candidates):
                if candidate_has_start != has_start:
                    continue
                target = next(
                    (
                        frame_ids
                        for anchor, frame_ids in clusters
                        if abs(anchor - value) <= maximum_gap
                    ),
                    None,
                )
                if target is None:
                    clusters.append((value, [frame_id]))
                else:
                    target.append(frame_id)
            for _anchor, frame_ids in clusters:
                union(frame_ids)

    components: Dict[int, List[int]] = {}
    for frame_id in positive:
        components.setdefault(find(frame_id), []).append(frame_id)
    result: Dict[int, Dict[str, Any]] = {}
    for frame_ids in components.values():
        timestamps = sorted(int(by_frame[value]['timestamp_ms']) for value in frame_ids)
        median = timestamps[len(timestamps) // 2]

        def rank(frame_id: int) -> Tuple[Any, ...]:
            row = by_frame[frame_id]
            return (
                int(row['has_complete_lineup']),
                int(row['review_status'] == 'confirmed'),
                int(row['hero_layout_label'] == 'result_page'),
                int(row['has_result_box']),
                int(row['panel_render_state'] == 'clear'),
                int(row['ocr_usable'] == 'yes'),
                int(row['result_occlusion'] == 'none'),
                int(row['is_representative']),
                float(row['model_confidence'] or 0),
                -abs(int(row['timestamp_ms']) - median),
                -frame_id,
            )

        representative = max(frame_ids, key=rank)
        info = {
            'result_group_size': len(frame_ids),
            'result_group_representative_frame_id': representative,
        }
        for frame_id in frame_ids:
            result[frame_id] = info
    return result


def _training_review_source_category(source_type: Any) -> str:
    normalized = str(source_type or '')
    if normalized == 'manual_correction':
        return 'manual_correction'
    if normalized == 'worker':
        return 'worker'
    if normalized == 'result_archive':
        return 'result_archive'
    if normalized == 'new_model_prefill':
        return 'model_prefill'
    if normalized == 'new_model_hero_prefill':
        return 'hero_model_prefill'
    if normalized == 'legacy_annotation' or normalized.startswith('legacy_'):
        return 'legacy'
    return 'other'


def _training_review_origin_frame_ids(
    conn: sqlite3.Connection, source_scope: str
) -> Optional[set[int]]:
    if source_scope not in _TRAINING_REVIEW_SOURCE_SCOPES:
        raise ValueError('训练复核数据来源无效')
    if source_scope == 'all':
        return None
    if source_scope == 'legacy':
        rows = conn.execute(
            'SELECT DISTINCT frame_id FROM training_review_sources '
            "WHERE source_type LIKE 'legacy_%'"
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT DISTINCT frame_id FROM training_review_sources '
            "WHERE source_type IN ('worker', 'result_archive', "
            "'manual_correction')"
        ).fetchall()
    return {int(row['frame_id']) for row in rows}


def training_review_duplicate_result_frame_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        frame_id
        for frame_id, group in training_review_result_groups(conn).items()
        if frame_id != group['result_group_representative_frame_id']
    }


def get_training_review_item(
    conn: sqlite3.Connection,
    frame_id: int,
    *,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"""
        SELECT item.*, frame.video_id, frame.timestamp_ms, frame.width,
               frame.height, frame.frame_path, frame.thumb_path, frame.sha256,
               video.streamer, video.filename, video.remote_path,
               CASE WHEN ({_MISSING_PLAYER_HERO_REVIEW})
                    THEN 1 ELSE 0 END AS needs_player_hero_review,
               CASE WHEN ({_UNIFIED_MANUAL_REVIEWED})
                    THEN 1 ELSE 0 END AS unified_manual_reviewed
        FROM training_review_items item
        JOIN frames frame ON frame.id = item.frame_id
        JOIN videos video ON video.id = frame.video_id
        WHERE item.frame_id = ?
        """,
        (int(frame_id),),
    ).fetchone()
    if row is None:
        return None
    item = _training_review_item_dict(conn, row)
    groups = (
        training_review_result_groups(conn) if result_groups is None else result_groups
    )
    item.update(
        groups.get(
            int(frame_id),
            {
                'result_group_size': 1,
                'result_group_representative_frame_id': int(frame_id),
            },
        )
    )
    return item


_LEGACY_HERO_SCREEN_TYPES = {
    'gameplay': 'gameplay_hud',
    'scoreboard': 'scoreboard',
    'result_page': 'result_page',
}


def _legacy_hero_review_groups(
    conn: sqlite3.Connection, *, streamer: str = '', screen_type: str = ''
) -> List[Dict[str, Any]]:
    if screen_type and screen_type not in _HERO_SCREEN_TYPES:
        raise ValueError('历史英雄补标画面类型无效')
    normalized_streamer = streamer.strip()
    rows = conn.execute(
        """
        SELECT annotation.frame_id, annotation.game_context,
               annotation.screen_type, annotation.game_mode,
               frame.video_id, frame.timestamp_ms, frame.width, frame.height,
               frame.is_representative, frame.model_confidence,
               video.streamer, video.filename,
               COALESCE(lineup.screen_type, '') AS lineup_screen_type,
               COALESCE(lineup.review_status, '') AS lineup_review_status,
               (
                   SELECT COUNT(*)
                   FROM training_review_hero_slots slot
                   WHERE slot.frame_id = annotation.frame_id
               ) AS hero_slot_count
        FROM annotations annotation
        JOIN frames frame ON frame.id = annotation.frame_id
        JOIN videos video ON video.id = frame.video_id
        JOIN training_review_items item
          ON item.frame_id = annotation.frame_id
        LEFT JOIN training_review_hero_lineups lineup
          ON lineup.frame_id = annotation.frame_id
        WHERE annotation.annotation_status = 'complete'
          AND annotation.content_family = 'vainglory'
          AND (? = '' OR video.streamer = ?)
        ORDER BY frame.video_id, frame.timestamp_ms, annotation.frame_id
        """,
        (normalized_streamer, normalized_streamer),
    ).fetchall()
    groups: Dict[Tuple[int, int, str], List[sqlite3.Row]] = {}
    current_video: Optional[int] = None
    match_index = 0
    match_active = False
    match_finished = False
    for row in rows:
        video_id = int(row['video_id'])
        if video_id != current_video:
            current_video = video_id
            match_index = 0
            match_active = False
            match_finished = False
        context = str(row['game_context'] or '')
        if context in {'pre_match', 'out_of_match'}:
            match_active = False
            match_finished = False
        elif context == 'in_match':
            if not match_active or match_finished:
                match_index += 1
                match_active = True
            match_finished = False
        elif context == 'post_match':
            if not match_active:
                match_index += 1
                match_active = True
            match_finished = True
        target_screen = _LEGACY_HERO_SCREEN_TYPES.get(str(row['screen_type'] or ''))
        if target_screen is None or (screen_type and target_screen != screen_type):
            continue
        if not match_active:
            match_index += 1
            match_active = True
        groups.setdefault((video_id, match_index, target_screen), []).append(row)

    result: List[Dict[str, Any]] = []
    for (video_id, sequence, target_screen), members in groups.items():
        completed = any(
            str(row['lineup_screen_type']) == target_screen
            and str(row['lineup_review_status']) == 'confirmed'
            for row in members
        )
        timestamps = sorted(int(row['timestamp_ms']) for row in members)
        median = timestamps[len(timestamps) // 2]

        def rank(row: sqlite3.Row) -> Tuple[Any, ...]:
            matching_layout = str(row['lineup_screen_type']) == target_screen
            return (
                int(matching_layout),
                int(row['hero_slot_count'] or 0),
                int(row['width']) * int(row['height']),
                int(row['is_representative']),
                float(row['model_confidence'] or 0),
                -abs(int(row['timestamp_ms']) - median),
                -int(row['frame_id']),
            )

        representative = max(members, key=rank)
        mode_counts: Dict[str, int] = {}
        for row in members:
            mode = str(row['game_mode'] or '')
            if mode:
                mode_counts[mode] = mode_counts.get(mode, 0) + 1
        mode = (
            max(mode_counts, key=lambda value: mode_counts[value])
            if mode_counts
            else ''
        )
        team_size = 5 if mode == '5v5' else 3 if mode in {'3v3', 'aram'} else None
        result.append(
            {
                'frame_id': int(representative['frame_id']),
                'video_id': video_id,
                'streamer': str(representative['streamer'] or ''),
                'filename': str(representative['filename'] or ''),
                'screen_type': target_screen,
                'team_size': team_size,
                'match_mode': mode or None,
                'match_index': sequence,
                'frame_count': len(members),
                'start_ms': timestamps[0],
                'end_ms': timestamps[-1],
                'completed': completed,
            }
        )
    screen_order = {'gameplay_hud': 0, 'scoreboard': 1, 'result_page': 2}
    remaining_by_streamer: Dict[str, int] = {}
    for group in result:
        if not group['completed']:
            name = str(group['streamer'])
            remaining_by_streamer[name] = remaining_by_streamer.get(name, 0) + 1
    result.sort(
        key=lambda group: (
            -remaining_by_streamer.get(str(group['streamer']), 0),
            group['streamer'],
            group['video_id'],
            group['match_index'],
            screen_order[group['screen_type']],
        )
    )
    return result


def list_legacy_hero_review_items(
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    screen_type: str = '',
    limit: int = 1000,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    if limit < 1 or limit > 10_000 or offset < 0:
        raise ValueError('训练复核分页参数无效')
    groups = [
        group
        for group in _legacy_hero_review_groups(
            conn, streamer=streamer, screen_type=screen_type
        )
        if not group['completed']
    ]
    result_groups = training_review_result_groups(conn)
    result = []
    for group in groups[offset : offset + limit]:
        item = get_training_review_item(
            conn, group['frame_id'], result_groups=result_groups
        )
        if item is None:
            continue
        item.update(
            {
                'legacy_hero_needs_review': True,
                'legacy_hero_group_key': 'legacy:{}:{}:{}'.format(
                    group['video_id'], group['match_index'], group['screen_type']
                ),
                'legacy_hero_group_size': group['frame_count'],
                'legacy_hero_group_start_ms': group['start_ms'],
                'legacy_hero_group_end_ms': group['end_ms'],
                'legacy_hero_screen_type': group['screen_type'],
                'legacy_hero_team_size': group['team_size'],
                'legacy_hero_match_index': group['match_index'],
            }
        )
        result.append(item)
    return result


def legacy_hero_review_stats(
    conn: sqlite3.Connection, *, streamer: str = '', screen_type: str = ''
) -> Dict[str, Any]:
    groups = _legacy_hero_review_groups(
        conn, streamer=streamer, screen_type=screen_type
    )
    remaining = [group for group in groups if not group['completed']]
    by_screen_type = {
        screen: sum(1 for group in remaining if group['screen_type'] == screen)
        for screen in ('gameplay_hud', 'scoreboard', 'result_page')
    }
    streamers: Dict[str, Dict[str, Any]] = {}
    for group in remaining:
        streamer = str(group['streamer'] or '')
        value = streamers.setdefault(
            streamer,
            {
                'streamer': streamer,
                'groups': 0,
                'frames': 0,
                'by_screen_type': {
                    'gameplay_hud': 0,
                    'scoreboard': 0,
                    'result_page': 0,
                },
            },
        )
        value['groups'] += 1
        value['frames'] += int(group['frame_count'])
        value['by_screen_type'][group['screen_type']] += 1
    return {
        'total_groups': len(groups),
        'completed_groups': sum(1 for group in groups if group['completed']),
        'remaining_groups': len(remaining),
        'remaining_frames': sum(int(group['frame_count']) for group in remaining),
        'by_screen_type': by_screen_type,
        'by_streamer': sorted(
            streamers.values(),
            key=lambda value: (-int(value['groups']), str(value['streamer'])),
        ),
    }


def _training_review_attribute_frame_ids(
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    hero: str = '',
    confidence: str = '',
) -> Optional[set[int]]:
    if confidence not in {'', 'low', 'boundary', 'high'}:
        raise ValueError('模型置信度筛选无效')
    conditions: List[str] = []
    parameters: List[Any] = []
    if streamer:
        conditions.append('video.streamer = ?')
        parameters.append(streamer)
    if source_type:
        conditions.append(
            'EXISTS (SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id = item.frame_id AND source.source_type = ?)'
        )
        parameters.append(source_type)
    if match_mode:
        conditions.append(
            '(item.match_mode_label = ? OR EXISTS ('
            'SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id = item.frame_id AND ('
            "json_extract(source.suggestions_json, '$.match_mode.label') = ? OR "
            "json_extract(source.metadata_json, "
            "'$.manual_correction.after.game_mode') = ?)))"
        )
        parameters.extend((match_mode, match_mode, match_mode))
    if scene:
        if scene in _HERO_SCREEN_TYPES:
            conditions.append('item.hero_layout_label = ?')
            parameters.append(scene)
        elif scene == 'hero_select':
            conditions.append(
                "(item.hero_select_label LIKE 'select_%' OR EXISTS ("
                'SELECT 1 FROM training_review_sources source '
                'WHERE source.frame_id = item.frame_id AND '
                "json_extract(source.suggestions_json, '$.hero_select.label') "
                "LIKE 'select_%'))"
            )
        elif scene == 'other':
            conditions.append(
                "COALESCE(item.hero_layout_label, '') NOT IN "
                "('gameplay_hud', 'scoreboard', 'result_page') AND "
                "COALESCE(item.hero_select_label, '') NOT LIKE 'select_%'"
            )
        else:
            raise ValueError('画面类型筛选无效')
    if hero:
        conditions.append(
            'EXISTS (SELECT 1 FROM training_review_hero_slots slot '
            'WHERE slot.frame_id = item.frame_id AND '
            '(slot.confirmed_label = ? OR slot.suggested_label = ?))'
        )
        parameters.extend((hero, hero))
    if confidence:
        ranges = {
            'low': ('<', 0.6),
            'boundary': ('BETWEEN', (0.6, 0.85)),
            'high': ('>=', 0.85),
        }
        operator, value = ranges[confidence]
        if operator == 'BETWEEN':
            confidence_sql = (
                "CAST(json_extract(suggestion.value, '$.confidence') AS REAL) "
                'BETWEEN ? AND ?'
            )
            assert isinstance(value, tuple)
            parameters.extend(value)
        else:
            confidence_sql = (
                "CAST(json_extract(suggestion.value, '$.confidence') AS REAL) "
                f'{operator} ?'
            )
            assert isinstance(value, float)
            parameters.append(value)
        conditions.append(
            'EXISTS (SELECT 1 FROM training_review_sources source, '
            'json_each(source.suggestions_json) suggestion '
            'WHERE source.frame_id = item.frame_id AND '
            f'{confidence_sql})'
        )
    if not conditions:
        return None
    rows = conn.execute(
        'SELECT DISTINCT item.frame_id FROM training_review_items item '
        'JOIN frames frame ON frame.id = item.frame_id '
        'JOIN videos video ON video.id = frame.video_id WHERE '
        + ' AND '.join(conditions),
        parameters,
    ).fetchall()
    return {int(row['frame_id']) for row in rows}


def _training_review_visible_frame_ids(
    conn: sqlite3.Connection,
    *,
    status: str,
    source_scope: str,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    hero: str = '',
    confidence: str = '',
) -> Tuple[List[int], Dict[int, Dict[str, Any]]]:
    if status not in _TRAINING_REVIEW_STATUSES | {
        'all',
        'needs_review',
        'missing_player',
        'legacy_hero',
        'migration_review',
        'human_confirmed',
    }:
        raise ValueError('训练复核状态无效')
    source_frame_ids = _training_review_origin_frame_ids(conn, source_scope)
    attribute_frame_ids = _training_review_attribute_frame_ids(
        conn,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        hero=hero,
        confidence=confidence,
    )
    base = (
        'SELECT frame_id FROM training_review_items '
        "ORDER BY CASE review_status WHEN 'pending' THEN 0 WHEN 'partial' THEN 1 "
        "WHEN 'confirmed' THEN 2 ELSE 3 END, updated_at DESC, frame_id DESC"
    )
    parameters: tuple[Any, ...] = ()
    if status == 'needs_review':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            "WHERE item.review_status IN ('pending', 'partial') "
            f'ORDER BY {_TRAINING_REVIEW_ARAM_PRIORITY}, '
            "CASE WHEN item.review_status = 'pending' THEN 0 ELSE 1 END, "
            f'{_TRAINING_REVIEW_SOURCE_CREATED_AT} DESC, '
            f'{_TRAINING_REVIEW_SOURCE_OFFSET} DESC, '
            'item.updated_at DESC, item.frame_id DESC'
        )
    elif status == 'missing_player':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            f'WHERE {_MISSING_PLAYER_HERO_REVIEW} '
            'ORDER BY item.updated_at DESC, item.frame_id DESC'
        )
    elif status == 'migration_review':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            "WHERE item.review_status = 'confirmed' "
            'AND EXISTS ('
            'SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id = item.frame_id '
            "AND source.source_type LIKE 'legacy_%') "
            f'AND NOT ({_UNIFIED_MANUAL_REVIEWED}) '
            f'ORDER BY {_TRAINING_REVIEW_ARAM_PRIORITY}, '
            'item.updated_at DESC, item.frame_id DESC'
        )
    elif status == 'human_confirmed':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            "WHERE item.review_status = 'confirmed' "
            f'AND ({_UNIFIED_MANUAL_REVIEWED}) '
            'ORDER BY item.reviewed_at DESC, item.frame_id DESC'
        )
    elif status == 'pending':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            "WHERE item.review_status = 'pending' "
            f'ORDER BY {_TRAINING_REVIEW_ARAM_PRIORITY}, '
            f'{_TRAINING_REVIEW_SOURCE_CREATED_AT} DESC, '
            f'{_TRAINING_REVIEW_SOURCE_OFFSET} DESC, '
            'item.updated_at DESC, item.frame_id DESC'
        )
    elif status != 'all':
        base = (
            'SELECT frame_id FROM training_review_items WHERE review_status = ? '
            'ORDER BY updated_at DESC, frame_id DESC'
        )
        parameters = (status,)
    rows = conn.execute(base, parameters).fetchall()
    result_groups = training_review_result_groups(conn)
    visible = [
        int(row['frame_id'])
        for row in rows
        if (source_frame_ids is None or int(row['frame_id']) in source_frame_ids)
        if (attribute_frame_ids is None or int(row['frame_id']) in attribute_frame_ids)
        if result_groups.get(int(row['frame_id']), {}).get(
            'result_group_representative_frame_id', int(row['frame_id'])
        )
        == int(row['frame_id'])
    ]
    return visible, result_groups


def list_training_review_items(
    conn: sqlite3.Connection,
    *,
    status: str = 'pending',
    limit: int = 1000,
    offset: int = 0,
    source_scope: str = 'all',
    streamer: str = '',
    hero_screen_type: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    hero: str = '',
    confidence: str = '',
) -> List[Dict[str, Any]]:
    if limit < 1 or limit > 10_000 or offset < 0:
        raise ValueError('训练复核分页参数无效')
    if status == 'legacy_hero':
        if source_scope == 'new':
            return []
        return list_legacy_hero_review_items(
            conn,
            streamer=streamer,
            screen_type=hero_screen_type,
            limit=limit,
            offset=offset,
        )
    visible, result_groups = _training_review_visible_frame_ids(
        conn,
        status=status,
        source_scope=source_scope,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        hero=hero,
        confidence=confidence,
    )
    result = []
    for frame_id in visible[offset : offset + limit]:
        item = get_training_review_item(conn, frame_id, result_groups=result_groups)
        if item is not None:
            result.append(item)
    return result


def count_training_review_items(
    conn: sqlite3.Connection,
    *,
    status: str,
    source_scope: str,
    streamer: str = '',
    hero_screen_type: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    hero: str = '',
    confidence: str = '',
) -> int:
    if status == 'legacy_hero':
        if source_scope == 'new':
            return 0
        return int(
            legacy_hero_review_stats(
                conn, streamer=streamer, screen_type=hero_screen_type
            )['remaining_groups']
        )
    visible, _groups = _training_review_visible_frame_ids(
        conn,
        status=status,
        source_scope=source_scope,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        hero=hero,
        confidence=confidence,
    )
    return len(visible)


def save_training_review(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    match_flow_label: Optional[str],
    match_mode_label: Optional[str],
    hero_select_label: Optional[str],
    result_panel_label: Optional[str],
    hero_select_variant: Optional[str] = None,
    hero_select_visibility: Optional[str] = None,
    hero_layout_label: Optional[str] = None,
    panel_render_state: str = 'clear',
    ocr_usable: str = 'yes',
    result_occlusion: str = 'none',
    occluder_types: Sequence[str] = (),
    status: str = 'confirmed',
    notes: str = '',
) -> Dict[str, Any]:
    labels = {
        'match_flow': match_flow_label,
        'match_mode': match_mode_label,
        'hero_select': hero_select_label,
        'result_panel': result_panel_label,
    }
    for task, label in labels.items():
        if label is not None and label not in _TRAINING_REVIEW_LABELS[task]:
            raise ValueError(f'{task} 标签无效')
    if (
        hero_select_variant is not None
        and hero_select_variant not in _HERO_SELECT_VARIANTS
    ):
        raise ValueError('英雄选择类型无效')
    if hero_select_label == 'select_aram':
        if hero_select_variant not in (None, 'random'):
            raise ValueError('大乱斗英雄选择只能标记为随机选英雄')
    elif hero_select_label in ('select_3v3', 'select_5v5'):
        if hero_select_variant not in (None, 'bp', 'blind', 'unreadable'):
            raise ValueError('3V3/5V5 英雄选择类型无效')
    elif hero_select_variant is not None:
        raise ValueError('不是英雄选择界面时不能填写英雄选择类型')
    normalized_select_visibility = (
        str(hero_select_visibility) if hero_select_visibility else None
    )
    if (
        normalized_select_visibility is not None
        and normalized_select_visibility not in config.HERO_SELECT_VISIBILITY
    ):
        raise ValueError('英雄选择画面状态无效')
    if hero_select_label is not None and hero_select_label.startswith('select_'):
        normalized_select_visibility = normalized_select_visibility or 'unknown'
    else:
        normalized_select_visibility = None
    if hero_layout_label is not None and hero_layout_label not in _HERO_LAYOUT_LABELS:
        raise ValueError('英雄头像画面类型无效')
    if status not in _TRAINING_REVIEW_STATUSES:
        raise ValueError('训练复核状态无效')
    normalized_render_state = str(panel_render_state or 'clear')
    normalized_ocr = str(ocr_usable or 'yes')
    normalized_occlusion = str(result_occlusion or 'none')
    allowed_occluders = {value for value, _label in config.OCCLUDER_TYPES}
    normalized_occluders: List[str] = []
    for value in occluder_types or ():
        normalized = str(value)
        if normalized not in allowed_occluders:
            raise ValueError('结算遮挡物类型无效')
        if normalized not in normalized_occluders:
            normalized_occluders.append(normalized)
    if normalized_ocr not in config.OCR_USABLE:
        raise ValueError('结算 OCR 可用性无效')
    if normalized_render_state not in config.PANEL_RENDER_STATES:
        raise ValueError('面板显示状态无效')
    if normalized_occlusion not in config.RESULT_OCCLUSION:
        raise ValueError('结算遮挡状态无效')
    if not (
        result_panel_label == 'result_panel'
        or hero_layout_label in ('gameplay_hud', 'scoreboard', 'result_page')
    ):
        normalized_render_state = 'clear'
    if result_panel_label != 'result_panel':
        normalized_ocr = 'yes'
        normalized_occlusion = 'none'
        normalized_occluders = []
    elif normalized_occlusion != 'occluded':
        normalized_occluders = []
    if status == 'confirmed':
        if match_flow_label is None or result_panel_label is None:
            raise ValueError('确认前必须判断对局流程和结算面板')
        if (
            hero_select_label is not None
            and hero_select_label.startswith('select_')
            and result_panel_label != 'no_result_panel'
        ):
            raise ValueError('英雄选择界面的结算标签必须是“没有结算面板”')
        if result_panel_label == 'result_panel' and match_flow_label != 'match_flow':
            raise ValueError('结算面板必须属于对局流程画面')
        if match_flow_label == 'match_flow':
            if match_mode_label is None:
                raise ValueError('对局画面必须判断本帧能否看出模式')
            if hero_select_label != 'not_select':
                raise ValueError('对局画面不能同时标成英雄选择界面')
        elif match_flow_label == 'not_match_flow':
            if hero_select_label is None:
                raise ValueError('非对局画面必须判断英雄选择类型')
            if match_mode_label is not None:
                raise ValueError('非对局画面不能填写对局模式')
        elif match_mode_label is not None:
            raise ValueError('看不清对局状态时不能填写对局模式')
        if result_panel_label == 'result_panel' and 'result_panel' not in get_boxes(
            conn, int(frame_id)
        ):
            raise ValueError('结算正样本必须画一个完整结算面板框')
        if hero_layout_label in _HERO_SCREEN_TYPES:
            if match_flow_label != 'match_flow':
                raise ValueError('HUD、积分板或结算图必须属于对局流程画面')
            if hero_select_label != 'not_select':
                raise ValueError('英雄头像画面不能同时是英雄选择界面')
            if (
                hero_layout_label == 'result_page'
                and result_panel_label != 'result_panel'
            ):
                raise ValueError('结算图必须同时标记真正结算面板')
            if (
                hero_layout_label != 'result_page'
                and result_panel_label != 'no_result_panel'
            ):
                raise ValueError('HUD 或积分板不能同时标记真正结算面板')
            lineup = get_training_review_hero_lineup(conn, int(frame_id))
            if (
                lineup is None
                or lineup['screen_type'] != hero_layout_label
                or lineup['review_status'] != 'confirmed'
                or len(lineup['slots']) != int(lineup['team_size']) * 2
            ):
                raise ValueError('请先画满并确认全部英雄头像')
    if status != 'skipped' and result_panel_label != 'result_panel':
        delete_box(conn, int(frame_id), 'result_panel')
    timestamp = now()
    conn.execute(
        """
        INSERT INTO training_review_items
            (frame_id, match_flow_label, match_mode_label, hero_select_label,
             hero_select_variant, hero_select_visibility, result_panel_label,
             hero_layout_label,
             panel_render_state, ocr_usable, result_occlusion, occluder_types,
             review_status, notes, created_at, updated_at, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(frame_id) DO UPDATE SET
            match_flow_label=excluded.match_flow_label,
            match_mode_label=excluded.match_mode_label,
            hero_select_label=excluded.hero_select_label,
            hero_select_variant=excluded.hero_select_variant,
            hero_select_visibility=excluded.hero_select_visibility,
            result_panel_label=excluded.result_panel_label,
            hero_layout_label=excluded.hero_layout_label,
            panel_render_state=excluded.panel_render_state,
            ocr_usable=excluded.ocr_usable,
            result_occlusion=excluded.result_occlusion,
            occluder_types=excluded.occluder_types,
            review_status=excluded.review_status,
            notes=excluded.notes,
            updated_at=excluded.updated_at,
            reviewed_at=excluded.reviewed_at
        """,
        (
            int(frame_id),
            match_flow_label,
            match_mode_label,
            hero_select_label,
            hero_select_variant,
            normalized_select_visibility,
            result_panel_label,
            hero_layout_label,
            normalized_render_state,
            normalized_ocr,
            normalized_occlusion,
            json.dumps(normalized_occluders, ensure_ascii=False),
            status,
            notes[:1000],
            timestamp,
            timestamp,
            timestamp if status in ('confirmed', 'skipped') else None,
        ),
    )
    if status in ('confirmed', 'skipped'):
        conn.execute(
            "UPDATE training_review_sources SET sync_state = 'dirty', "
            'updated_at = ? WHERE frame_id = ? AND source_type = ?',
            (timestamp, int(frame_id), 'worker'),
        )
    if status == 'confirmed':
        conn.execute('UPDATE frames SET labeled = 1 WHERE id = ?', (int(frame_id),))
    conn.commit()
    audit(
        conn,
        'training_review',
        frame_id=int(frame_id),
        detail=json.dumps(
            {
                **labels,
                'hero_select_variant': hero_select_variant,
                'hero_select_visibility': normalized_select_visibility,
                'hero_layout_label': hero_layout_label,
                'panel_render_state': normalized_render_state,
                'ocr_usable': normalized_ocr,
                'result_occlusion': normalized_occlusion,
                'occluder_types': normalized_occluders,
                'review_status': status,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ),
    )
    item = get_training_review_item(conn, int(frame_id))
    if item is None:
        raise KeyError(frame_id)
    return item


def hero_layout_key(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ValueError('图片尺寸必须为正数')
    return f'{width / height:.3f}'


def get_training_review_hero_lineup(
    conn: sqlite3.Connection, frame_id: int
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM training_review_hero_lineups WHERE frame_id = ?',
        (int(frame_id),),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    slots = conn.execute(
        'SELECT side, slot, crop_x, crop_y, crop_w, crop_h, '
        'suggested_label, suggestion_confidence, confirmed_label, updated_at '
        'FROM training_review_hero_slots WHERE frame_id = ? '
        "ORDER BY CASE side WHEN 'left' THEN 0 ELSE 1 END, slot",
        (int(frame_id),),
    ).fetchall()
    result['slots'] = [
        {
            'side': str(slot['side']),
            'slot': int(slot['slot']),
            'crop': {
                'x': float(slot['crop_x']),
                'y': float(slot['crop_y']),
                'w': float(slot['crop_w']),
                'h': float(slot['crop_h']),
            },
            'suggested_label': str(slot['suggested_label'] or ''),
            'suggestion_confidence': float(slot['suggestion_confidence']),
            'confirmed_label': slot['confirmed_label'],
            'updated_at': slot['updated_at'],
        }
        for slot in slots
    ]
    return result


def _validated_hero_slots(
    slots: List[Dict[str, Any]], team_size: int, *, require_complete: bool = True
) -> List[Dict[str, Any]]:
    if team_size not in (3, 5):
        raise ValueError('英雄阵容人数必须是 3 或 5')
    expected = {
        (side, slot) for side in ('left', 'right') for slot in range(1, team_size + 1)
    }
    normalized = []
    positions = set()
    for value in slots:
        side = str(value.get('side') or '')
        try:
            slot = int(value.get('slot'))
            crop = value.get('crop') or {}
            x, y, w, h = (float(crop[name]) for name in ('x', 'y', 'w', 'h'))
            confidence = float(value.get('suggestion_confidence', 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('英雄位置或裁剪框无效') from exc
        position = (side, slot)
        if position not in expected or position in positions:
            raise ValueError('英雄位置与队伍人数不匹配')
        if not (
            0 <= x <= 1
            and 0 <= y <= 1
            and 0 < w <= 1
            and 0 < h <= 1
            and x + w <= 1.001
            and y + h <= 1.001
        ):
            raise ValueError('英雄头像裁剪框必须归一化到 [0,1]')
        if not 0 <= confidence <= 1:
            raise ValueError('英雄建议置信度必须在 [0,1]')
        suggested_label = str(value.get('suggested_label') or '').strip()
        if len(suggested_label) > 80:
            raise ValueError('英雄名称过长')
        positions.add(position)
        normalized.append(
            {
                'side': side,
                'slot': slot,
                'crop': {'x': x, 'y': y, 'w': w, 'h': h},
                'suggested_label': suggested_label,
                'suggestion_confidence': confidence,
            }
        )
    if require_complete and positions != expected:
        raise ValueError(f'必须提供完整的 {team_size * 2} 个英雄位置')
    return normalized


def _clear_invalid_training_review_player_slot(
    conn: sqlite3.Connection, frame_id: int
) -> None:
    conn.execute(
        """
        UPDATE training_review_hero_lineups
        SET player_status = 'pending', player_side = NULL, player_slot = NULL
        WHERE frame_id = ?
          AND player_status = 'identified'
          AND NOT EXISTS (
              SELECT 1 FROM training_review_hero_slots slot
              WHERE slot.frame_id = training_review_hero_lineups.frame_id
                AND slot.side = training_review_hero_lineups.player_side
                AND slot.slot = training_review_hero_lineups.player_slot
          )
        """,
        (int(frame_id),),
    )


def replace_training_review_hero_suggestions(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    screen_type: str,
    team_size: int,
    method: str,
    slots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if screen_type not in _HERO_SCREEN_TYPES:
        raise ValueError('英雄阵容画面类型无效')
    normalized_method = method.strip()[:80]
    if not normalized_method:
        raise ValueError('英雄预填算法不能为空')
    normalized = _validated_hero_slots(slots, team_size)
    if not conn.execute(
        'SELECT 1 FROM training_review_items WHERE frame_id = ?', (int(frame_id),)
    ).fetchone():
        raise KeyError(frame_id)
    timestamp = now()
    with conn:
        existing = conn.execute(
            'SELECT team_size, review_status '
            'FROM training_review_hero_lineups WHERE frame_id = ?',
            (int(frame_id),),
        ).fetchone()
        if (
            existing is not None
            and existing['review_status'] == 'confirmed'
            and int(existing['team_size']) != team_size
        ):
            raise ValueError('已确认阵容不能自动更换队伍人数')
        conn.execute(
            """
            INSERT INTO training_review_hero_lineups
                (frame_id, screen_type, team_size, suggestion_method,
                 review_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(frame_id) DO UPDATE SET
                player_side=CASE
                    WHEN training_review_hero_lineups.screen_type =
                         excluded.screen_type
                     AND training_review_hero_lineups.team_size =
                         excluded.team_size
                    THEN training_review_hero_lineups.player_side
                    ELSE NULL
                END,
                player_slot=CASE
                    WHEN training_review_hero_lineups.screen_type =
                         excluded.screen_type
                     AND training_review_hero_lineups.team_size =
                         excluded.team_size
                    THEN training_review_hero_lineups.player_slot
                    ELSE NULL
                END,
                player_status=CASE
                    WHEN training_review_hero_lineups.screen_type =
                         excluded.screen_type
                     AND training_review_hero_lineups.team_size =
                         excluded.team_size
                    THEN training_review_hero_lineups.player_status
                    ELSE 'pending'
                END,
                screen_type=excluded.screen_type,
                team_size=excluded.team_size,
                suggestion_method=excluded.suggestion_method,
                updated_at=excluded.updated_at
            """,
            (
                int(frame_id),
                screen_type,
                team_size,
                normalized_method,
                timestamp,
                timestamp,
            ),
        )
        conn.executemany(
            """
            INSERT INTO training_review_hero_slots
                (frame_id, side, slot, crop_x, crop_y, crop_w, crop_h,
                 suggested_label, suggestion_confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(frame_id, side, slot) DO UPDATE SET
                crop_x=excluded.crop_x,
                crop_y=excluded.crop_y,
                crop_w=excluded.crop_w,
                crop_h=excluded.crop_h,
                suggested_label=excluded.suggested_label,
                suggestion_confidence=excluded.suggestion_confidence,
                updated_at=excluded.updated_at
            """,
            [
                (
                    int(frame_id),
                    value['side'],
                    value['slot'],
                    value['crop']['x'],
                    value['crop']['y'],
                    value['crop']['w'],
                    value['crop']['h'],
                    value['suggested_label'],
                    value['suggestion_confidence'],
                    timestamp,
                )
                for value in normalized
            ],
        )
        conn.execute(
            'DELETE FROM training_review_hero_slots ' 'WHERE frame_id = ? AND slot > ?',
            (int(frame_id), team_size),
        )
        _clear_invalid_training_review_player_slot(conn, int(frame_id))
    result = get_training_review_hero_lineup(conn, int(frame_id))
    if result is None:
        raise KeyError(frame_id)
    return result


def replace_training_review_hero_layout(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    screen_type: str,
    team_size: int,
    method: str,
    slots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """保存本帧人工圆框草稿；允许尚未画满，且不会冒充人工英雄真值。"""
    if screen_type not in _HERO_SCREEN_TYPES:
        raise ValueError('英雄阵容画面类型无效')
    normalized_method = method.strip()[:80]
    if not normalized_method:
        raise ValueError('英雄预填算法不能为空')
    normalized = _validated_hero_slots(slots, team_size, require_complete=False)
    if not conn.execute(
        'SELECT 1 FROM training_review_items WHERE frame_id = ?', (int(frame_id),)
    ).fetchone():
        raise KeyError(frame_id)
    timestamp = now()
    with conn:
        conn.execute(
            """
            INSERT INTO training_review_hero_lineups
                (frame_id, screen_type, team_size, suggestion_method,
                 review_status, created_at, updated_at, reviewed_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL)
            ON CONFLICT(frame_id) DO UPDATE SET
                player_side=CASE
                    WHEN training_review_hero_lineups.screen_type =
                         excluded.screen_type
                     AND training_review_hero_lineups.team_size =
                         excluded.team_size
                    THEN training_review_hero_lineups.player_side
                    ELSE NULL
                END,
                player_slot=CASE
                    WHEN training_review_hero_lineups.screen_type =
                         excluded.screen_type
                     AND training_review_hero_lineups.team_size =
                         excluded.team_size
                    THEN training_review_hero_lineups.player_slot
                    ELSE NULL
                END,
                player_status=CASE
                    WHEN training_review_hero_lineups.screen_type =
                         excluded.screen_type
                     AND training_review_hero_lineups.team_size =
                         excluded.team_size
                    THEN training_review_hero_lineups.player_status
                    ELSE 'pending'
                END,
                screen_type=excluded.screen_type,
                team_size=excluded.team_size,
                suggestion_method=excluded.suggestion_method,
                review_status='pending',
                updated_at=excluded.updated_at,
                reviewed_at=NULL
            """,
            (
                int(frame_id),
                screen_type,
                team_size,
                normalized_method,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            'DELETE FROM training_review_hero_slots WHERE frame_id = ?',
            (int(frame_id),),
        )
        conn.executemany(
            """
            INSERT INTO training_review_hero_slots
                (frame_id, side, slot, crop_x, crop_y, crop_w, crop_h,
                 suggested_label, suggestion_confidence, confirmed_label,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            [
                (
                    int(frame_id),
                    value['side'],
                    value['slot'],
                    value['crop']['x'],
                    value['crop']['y'],
                    value['crop']['w'],
                    value['crop']['h'],
                    value['suggested_label'],
                    value['suggestion_confidence'],
                    timestamp,
                )
                for value in normalized
            ],
        )
        _clear_invalid_training_review_player_slot(conn, int(frame_id))
    result = get_training_review_hero_lineup(conn, int(frame_id))
    if result is None:
        raise KeyError(frame_id)
    return result


def _hero_template_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    slots = conn.execute(
        'SELECT side, slot, crop_x, crop_y, crop_w, crop_h '
        'FROM training_review_hero_template_slots WHERE template_id = ? '
        "ORDER BY CASE side WHEN 'left' THEN 0 ELSE 1 END, slot",
        (int(row['id']),),
    ).fetchall()
    result['slots'] = [
        {
            'side': str(slot['side']),
            'slot': int(slot['slot']),
            'crop': {
                'x': float(slot['crop_x']),
                'y': float(slot['crop_y']),
                'w': float(slot['crop_w']),
                'h': float(slot['crop_h']),
            },
        }
        for slot in slots
    ]
    return result


def get_training_review_hero_template(
    conn: sqlite3.Connection,
    *,
    streamer: str,
    screen_type: str,
    team_size: int,
    layout_key: str,
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT * FROM training_review_hero_templates '
        'WHERE streamer = ? AND screen_type = ? AND team_size = ? '
        'AND layout_key = ?',
        (streamer, screen_type, int(team_size), layout_key),
    ).fetchone()
    return None if row is None else _hero_template_dict(conn, row)


def save_training_review_hero_template(
    conn: sqlite3.Connection,
    *,
    streamer: str,
    screen_type: str,
    team_size: int,
    layout_key: str,
    slots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_streamer = streamer.strip()[:200]
    normalized_key = layout_key.strip()[:40]
    if not normalized_streamer:
        raise ValueError('主播名称为空，不能缓存英雄布局')
    if screen_type not in _HERO_SCREEN_TYPES:
        raise ValueError('英雄阵容画面类型无效')
    if not normalized_key:
        raise ValueError('英雄布局比例无效')
    normalized = _validated_hero_slots(slots, team_size)
    timestamp = now()
    with conn:
        conn.execute(
            """
            INSERT INTO training_review_hero_templates
                (streamer, screen_type, team_size, layout_key,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(streamer, screen_type, team_size, layout_key)
            DO UPDATE SET updated_at=excluded.updated_at
            """,
            (
                normalized_streamer,
                screen_type,
                team_size,
                normalized_key,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            'SELECT id FROM training_review_hero_templates '
            'WHERE streamer = ? AND screen_type = ? AND team_size = ? '
            'AND layout_key = ?',
            (normalized_streamer, screen_type, team_size, normalized_key),
        ).fetchone()
        if row is None:
            raise RuntimeError('英雄布局模板保存失败')
        template_id = int(row['id'])
        conn.execute(
            'DELETE FROM training_review_hero_template_slots ' 'WHERE template_id = ?',
            (template_id,),
        )
        conn.executemany(
            """
            INSERT INTO training_review_hero_template_slots
                (template_id, side, slot, crop_x, crop_y, crop_w, crop_h)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    template_id,
                    value['side'],
                    value['slot'],
                    value['crop']['x'],
                    value['crop']['y'],
                    value['crop']['w'],
                    value['crop']['h'],
                )
                for value in normalized
            ],
        )
    result = get_training_review_hero_template(
        conn,
        streamer=normalized_streamer,
        screen_type=screen_type,
        team_size=team_size,
        layout_key=normalized_key,
    )
    if result is None:
        raise RuntimeError('英雄布局模板保存失败')
    return result


def save_training_review_hero_lineup(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    labels: List[Dict[str, Any]],
    allowed_labels: set[str],
    player_status: Optional[str] = None,
    player_side: Optional[str] = None,
    player_slot: Optional[int] = None,
) -> Dict[str, Any]:
    lineup = get_training_review_hero_lineup(conn, int(frame_id))
    if lineup is None:
        raise KeyError(frame_id)
    team_size = int(lineup['team_size'])
    expected = {
        (side, slot) for side in ('left', 'right') for slot in range(1, team_size + 1)
    }
    normalized = []
    positions = set()
    accepted = set(allowed_labels) | {'unreadable'}
    for value in labels:
        side = str(value.get('side') or '')
        try:
            slot = int(value.get('slot'))
        except (TypeError, ValueError) as exc:
            raise ValueError('英雄位置无效') from exc
        hero_label = str(value.get('hero_label') or '').strip()
        position = (side, slot)
        if position not in expected or position in positions:
            raise ValueError('英雄位置与队伍人数不匹配')
        if hero_label not in accepted:
            raise ValueError('英雄名称无效')
        positions.add(position)
        normalized.append((hero_label, side, slot))
    if positions != expected:
        raise ValueError(f'必须确认完整的 {team_size * 2} 个英雄位置')
    raw_player_status = str(player_status or '').strip()
    normalized_player_status = raw_player_status or (
        'identified'
        if player_side is not None or player_slot is not None
        else 'pending'
    )
    if normalized_player_status not in {'pending', 'identified', 'unreadable'}:
        raise ValueError('主播英雄位置状态无效')
    normalized_player_side: Optional[str] = None
    normalized_player_slot: Optional[int] = None
    if normalized_player_status == 'identified':
        try:
            normalized_player_side = str(player_side or '')
            normalized_player_slot = int(player_slot)
        except (TypeError, ValueError) as exc:
            raise ValueError('主播英雄位置无效') from exc
        if (normalized_player_side, normalized_player_slot) not in expected:
            raise ValueError('主播英雄位置无效')
    elif player_side is not None or player_slot is not None:
        raise ValueError('主播英雄位置状态冲突')
    timestamp = now()
    with conn:
        conn.executemany(
            'UPDATE training_review_hero_slots SET confirmed_label = ?, '
            'updated_at = ? WHERE frame_id = ? AND side = ? AND slot = ?',
            [
                (label, timestamp, int(frame_id), side, slot)
                for label, side, slot in normalized
            ],
        )
        conn.execute(
            "UPDATE training_review_hero_lineups SET review_status='confirmed', "
            'player_status=?, player_side=?, player_slot=?, '
            'updated_at=?, reviewed_at=? '
            'WHERE frame_id=?',
            (
                normalized_player_status,
                normalized_player_side,
                normalized_player_slot,
                timestamp,
                timestamp,
                int(frame_id),
            ),
        )
    audit(
        conn,
        'training_review_hero_lineup',
        frame_id=int(frame_id),
        detail=json.dumps(
            {
                'team_size': team_size,
                'heroes': [
                    {'side': side, 'slot': slot, 'hero_label': label}
                    for label, side, slot in normalized
                ],
                'player_status': normalized_player_status,
                'player_side': normalized_player_side,
                'player_slot': normalized_player_slot,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ),
    )
    result = get_training_review_hero_lineup(conn, int(frame_id))
    if result is None:
        raise KeyError(frame_id)
    return result


def training_review_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    duplicates = training_review_duplicate_result_frame_ids(conn)
    item_rows = conn.execute('SELECT * FROM training_review_items').fetchall()
    visible_rows = [row for row in item_rows if int(row['frame_id']) not in duplicates]
    visible_ids = {int(row['frame_id']) for row in visible_rows}
    statuses: Dict[str, int] = {}
    for row in visible_rows:
        status = str(row['review_status'])
        statuses[status] = statuses.get(status, 0) + 1
    labels: Dict[str, Dict[str, int]] = {}
    for task, column in (
        ('match_flow', 'match_flow_label'),
        ('match_mode', 'match_mode_label'),
        ('hero_select', 'hero_select_label'),
        ('hero_select_variant', 'hero_select_variant'),
        ('result_panel', 'result_panel_label'),
    ):
        counts: Dict[str, int] = {}
        for row in visible_rows:
            if row[column] is None:
                continue
            label = str(row[column])
            counts[label] = counts.get(label, 0) + 1
        labels[task] = counts
    dirty = int(
        conn.execute(
            "SELECT COUNT(*) FROM training_review_sources WHERE source_type = 'worker' "
            "AND sync_state = 'dirty'"
        ).fetchone()[0]
    )
    conflicts = int(
        conn.execute(
            "SELECT COUNT(*) FROM training_review_sources WHERE source_type = 'worker' "
            "AND sync_state = 'conflict'"
        ).fetchone()[0]
    )
    missing_player_ids = {
        int(row['frame_id'])
        for row in conn.execute(
            'SELECT item.frame_id FROM training_review_items item '
            f'WHERE {_MISSING_PLAYER_HERO_REVIEW}'
        ).fetchall()
        if int(row['frame_id']) not in duplicates
    }
    missing_player_hero = len(missing_player_ids)
    categories_by_frame: Dict[int, set[str]] = {}
    for row in conn.execute(
        'SELECT frame_id, source_type FROM training_review_sources'
    ).fetchall():
        frame_id = int(row['frame_id'])
        if frame_id not in visible_ids:
            continue
        categories_by_frame.setdefault(frame_id, set()).add(
            _training_review_source_category(row['source_type'])
        )
    source_frames = {'legacy': 0, 'worker': 0, 'result_archive': 0, 'other': 0}
    origin_categories = {'legacy', 'worker', 'result_archive'}
    for frame_id in visible_ids:
        categories = categories_by_frame.get(frame_id, set())
        matched = categories & origin_categories
        if not matched:
            source_frames['other'] += 1
            continue
        for category in matched:
            source_frames[category] += 1

    legacy_ids = {
        frame_id
        for frame_id, categories in categories_by_frame.items()
        if 'legacy' in categories
    }
    legacy_hero_targets = {
        int(row['frame_id']): _LEGACY_HERO_SCREEN_TYPES.get(
            str(row['screen_type'] or '')
        )
        for row in conn.execute(
            'SELECT frame_id, screen_type FROM annotations '
            "WHERE annotation_status = 'complete'"
        ).fetchall()
        if int(row['frame_id']) in legacy_ids
        and _LEGACY_HERO_SCREEN_TYPES.get(str(row['screen_type'] or '')) is not None
    }
    confirmed_lineups = {
        int(row['frame_id']): str(row['screen_type'])
        for row in conn.execute(
            'SELECT frame_id, screen_type FROM training_review_hero_lineups '
            "WHERE review_status = 'confirmed'"
        ).fetchall()
    }
    legacy_hero_complete = sum(
        confirmed_lineups.get(frame_id) == target
        for frame_id, target in legacy_hero_targets.items()
    )
    legacy_core_confirmed = sum(
        str(row['review_status']) == 'confirmed'
        for row in visible_rows
        if int(row['frame_id']) in legacy_ids
    )
    manually_reviewed_ids = {
        int(row['frame_id'])
        for row in conn.execute(
            "SELECT DISTINCT frame_id FROM audit_log WHERE action = ? "
            'AND frame_id IS NOT NULL',
            ('training_review',),
        ).fetchall()
        if int(row['frame_id']) in visible_ids
    }
    confirmed_ids = {
        int(row['frame_id'])
        for row in visible_rows
        if str(row['review_status']) == 'confirmed'
    }
    legacy_manual_confirmed = len(legacy_ids & manually_reviewed_ids & confirmed_ids)
    legacy_migration_pending = len((legacy_ids & confirmed_ids) - manually_reviewed_ids)
    legacy_data = {
        'frames': len(legacy_ids),
        'core_label_confirmed': legacy_core_confirmed,
        'core_label_needs_review': len(legacy_ids) - legacy_core_confirmed,
        'unified_manual_confirmed': legacy_manual_confirmed,
        'migration_pending_review': legacy_migration_pending,
        'hero_eligible': len(legacy_hero_targets),
        'hero_complete': legacy_hero_complete,
        'hero_missing': len(legacy_hero_targets) - legacy_hero_complete,
    }
    source_scopes: Dict[str, Dict[str, Any]] = {}
    scope_ids = {
        'new': {
            frame_id
            for frame_id, categories in categories_by_frame.items()
            if categories & {'worker', 'result_archive'}
        },
        'legacy': legacy_ids,
    }
    for scope, frame_ids in scope_ids.items():
        scope_rows = [row for row in visible_rows if int(row['frame_id']) in frame_ids]
        scope_statuses: Dict[str, int] = {}
        for row in scope_rows:
            review_status = str(row['review_status'])
            scope_statuses[review_status] = scope_statuses.get(review_status, 0) + 1
        source_scopes[scope] = {
            'total': len(scope_rows),
            'statuses': scope_statuses,
            'needs_review': int(scope_statuses.get('pending', 0))
            + int(scope_statuses.get('partial', 0)),
            'missing_player_hero': len(frame_ids & missing_player_ids),
            'human_confirmed': len(frame_ids & manually_reviewed_ids & confirmed_ids),
            'migration_pending_review': (
                len((frame_ids & confirmed_ids) - manually_reviewed_ids)
                if scope == 'legacy'
                else 0
            ),
            'core_model_prefilled': sum(
                'model_prefill' in categories_by_frame.get(frame_id, set())
                for frame_id in frame_ids
            ),
            'hero_model_prefilled': sum(
                'hero_model_prefill' in categories_by_frame.get(frame_id, set())
                for frame_id in frame_ids
            ),
        }
    return {
        'total': sum(statuses.values()),
        'statuses': statuses,
        'labels': labels,
        'dirty': dirty,
        'conflicts': conflicts,
        'missing_player_hero': missing_player_hero,
        'source_frames': source_frames,
        'source_scopes': source_scopes,
        'legacy_data': legacy_data,
    }
