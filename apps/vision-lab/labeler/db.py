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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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
            '3v3', 'aram', '5v5', 'blitz', 'unreadable')),
    match_kind_label TEXT CHECK (
        match_kind_label IS NULL OR match_kind_label IN (
            'pvp', 'bot', 'practice', 'unreadable')),
    view_context_label TEXT CHECK (
        view_context_label IS NULL OR view_context_label IN (
            'played', 'spectated', 'replay', 'unreadable')),
    hero_select_label TEXT CHECK (
        hero_select_label IS NULL OR hero_select_label IN (
            'not_select', 'select_3v3', 'select_aram', 'select_5v5',
            'select_blitz', 'unreadable')),
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
CREATE INDEX IF NOT EXISTS idx_training_review_context
    ON training_review_items (
        match_kind_label, view_context_label, review_status, frame_id);

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
CREATE INDEX IF NOT EXISTS idx_training_review_sources_type_frame
    ON training_review_sources (source_type, frame_id);

-- 候选收件箱只负责预打标的领取、重试和租约。只有核心模型
-- 已产出结果的图片才会晋级到 training_review_items。
CREATE TABLE IF NOT EXISTS training_review_candidate_inbox (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    prefill_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        prefill_status IN ('pending', 'queued', 'running', 'failed', 'promoted')),
    prefill_stage TEXT NOT NULL DEFAULT 'core' CHECK (
        prefill_stage IN ('core', 'hero', 'complete')),
    prefill_attempts INTEGER NOT NULL DEFAULT 0 CHECK (prefill_attempts >= 0),
    prefill_error TEXT NOT NULL DEFAULT '',
    prefill_screen_type TEXT NOT NULL DEFAULT '' CHECK (
        prefill_screen_type IN (
            '', 'gameplay_hud', 'scoreboard', 'result_page')),
    prefill_team_size INTEGER CHECK (prefill_team_size IN (3, 5)),
    source_created_at INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    promoted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_review_candidate_inbox_queue
    ON training_review_candidate_inbox (
        prefill_status, prefill_stage, prefill_attempts,
        source_created_at DESC, frame_id DESC);

-- 打标工作台的可检索事实。来源 JSON 仍完整保留，但列表筛选、排序和素材
-- 建议只读取这一行，不在请求时反复解析所有 JSON。
CREATE TABLE IF NOT EXISTS training_review_material_index (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL DEFAULT 0 CHECK (session_id >= 0),
    part_id INTEGER NOT NULL DEFAULT 0 CHECK (part_id >= 0),
    at_ms INTEGER NOT NULL DEFAULT 0 CHECK (at_ms >= 0),
    linked_match_id INTEGER REFERENCES training_review_match_contexts(match_id)
        ON DELETE SET NULL,
    match_link_source TEXT NOT NULL DEFAULT '' CHECK (
        match_link_source IN ('', 'result_archive', 'time_window')),
    review_status TEXT NOT NULL CHECK (
        review_status IN ('pending', 'partial', 'confirmed', 'skipped')),
    scene TEXT NOT NULL DEFAULT 'other' CHECK (
        scene IN ('gameplay_hud', 'scoreboard', 'result_page',
                  'hero_select', 'other')),
    match_mode TEXT NOT NULL DEFAULT '' CHECK (
        match_mode IN ('', '3v3', 'aram', '5v5', 'blitz')),
    is_new INTEGER NOT NULL DEFAULT 0 CHECK (is_new IN (0, 1)),
    is_legacy INTEGER NOT NULL DEFAULT 0 CHECK (is_legacy IN (0, 1)),
    has_worker INTEGER NOT NULL DEFAULT 0 CHECK (has_worker IN (0, 1)),
    has_result_archive INTEGER NOT NULL DEFAULT 0 CHECK (
        has_result_archive IN (0, 1)),
    has_manual_correction INTEGER NOT NULL DEFAULT 0 CHECK (
        has_manual_correction IN (0, 1)),
    has_model_prefill INTEGER NOT NULL DEFAULT 0 CHECK (
        has_model_prefill IN (0, 1)),
    has_hero_model_prefill INTEGER NOT NULL DEFAULT 0 CHECK (
        has_hero_model_prefill IN (0, 1)),
    prefill_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        prefill_status IN ('pending', 'queued', 'running', 'ready', 'failed')),
    prefill_stage TEXT NOT NULL DEFAULT 'core' CHECK (
        prefill_stage IN ('core', 'hero', 'complete')),
    prefill_attempts INTEGER NOT NULL DEFAULT 0 CHECK (prefill_attempts >= 0),
    prefill_error TEXT NOT NULL DEFAULT '',
    prefill_screen_type TEXT NOT NULL DEFAULT '' CHECK (
        prefill_screen_type IN (
            '', 'gameplay_hud', 'scoreboard', 'result_page')),
    prefill_team_size INTEGER CHECK (prefill_team_size IN (3, 5)),
    prefill_updated_at TEXT NOT NULL DEFAULT '',
    prefilled_at TEXT,
    has_low_confidence INTEGER NOT NULL DEFAULT 0 CHECK (
        has_low_confidence IN (0, 1)),
    has_boundary_confidence INTEGER NOT NULL DEFAULT 0 CHECK (
        has_boundary_confidence IN (0, 1)),
    has_high_confidence INTEGER NOT NULL DEFAULT 0 CHECK (
        has_high_confidence IN (0, 1)),
    selects_aram INTEGER NOT NULL DEFAULT 0 CHECK (selects_aram IN (0, 1)),
    suggests_aram INTEGER NOT NULL DEFAULT 0 CHECK (suggests_aram IN (0, 1)),
    source_created_at INTEGER NOT NULL DEFAULT 0,
    source_offset INTEGER NOT NULL DEFAULT 0,
    result_group_representative_frame_id INTEGER NOT NULL,
    result_group_size INTEGER NOT NULL DEFAULT 1 CHECK (result_group_size >= 1),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_review_material_queue
    ON training_review_material_index (
        review_status, is_new, scene, match_mode,
        source_created_at DESC, source_offset DESC, frame_id DESC);
CREATE INDEX IF NOT EXISTS idx_training_review_material_legacy_queue
    ON training_review_material_index (
        review_status, is_legacy, scene, match_mode,
        source_created_at DESC, frame_id DESC);
CREATE INDEX IF NOT EXISTS idx_training_review_material_video_mode
    ON training_review_material_index (video_id, review_status, match_mode);
CREATE INDEX IF NOT EXISTS idx_training_review_material_source_time
    ON training_review_material_index (session_id, part_id, at_ms, frame_id);
CREATE INDEX IF NOT EXISTS idx_training_review_material_match_scene
    ON training_review_material_index (
        linked_match_id, review_status, scene, frame_id);
CREATE INDEX IF NOT EXISTS idx_training_review_material_video_link_scene
    ON training_review_material_index (
        video_id, linked_match_id, review_status, scene, frame_id);
CREATE INDEX IF NOT EXISTS idx_training_review_material_representative
    ON training_review_material_index (
        result_group_representative_frame_id, frame_id);
CREATE INDEX IF NOT EXISTS idx_training_review_material_prefill_queue
    ON training_review_material_index (
        prefill_status, prefill_stage, prefill_attempts, review_status,
        is_new, source_created_at DESC, frame_id DESC);

-- 只镜像素材检索需要的最小对局时间窗。英雄证据仍来自各帧的人工确认或
-- 模型预填槽位，不复制主业务库的玩家真值。
CREATE TABLE IF NOT EXISTS training_review_match_contexts (
    match_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL CHECK (session_id > 0),
    part_id INTEGER NOT NULL CHECK (part_id > 0),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
    result_at_ms INTEGER NOT NULL CHECK (result_at_ms >= started_at_ms),
    game_mode TEXT NOT NULL DEFAULT '' CHECK (
        game_mode IN ('', '3v3', 'aram', '5v5', 'blitz')),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('result_archive', 'manual_correction')),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_review_match_context_window
    ON training_review_match_contexts (
        session_id, part_id, started_at_ms, result_at_ms, match_id);

-- 每帧对素材建议的贡献与汇总分开保存。更新一帧时先扣除旧贡献，再增加
-- 新贡献；同一帧反复导入或改标不会重复累计。
CREATE TABLE IF NOT EXISTS training_review_material_contributions (
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('scene_mode', 'hero_scene')),
    scene TEXT NOT NULL,
    match_mode TEXT NOT NULL DEFAULT '',
    hero_label TEXT NOT NULL DEFAULT '',
    source_scope TEXT NOT NULL CHECK (source_scope IN ('all', 'new', 'legacy')),
    metric TEXT NOT NULL CHECK (metric IN ('confirmed', 'candidate')),
    frame_count INTEGER NOT NULL DEFAULT 0 CHECK (frame_count >= 0),
    crop_count INTEGER NOT NULL DEFAULT 0 CHECK (crop_count >= 0),
    PRIMARY KEY (
        frame_id, kind, scene, match_mode, hero_label, source_scope, metric)
);

CREATE TABLE IF NOT EXISTS training_review_material_totals (
    kind TEXT NOT NULL CHECK (kind IN ('scene_mode', 'hero_scene')),
    scene TEXT NOT NULL,
    match_mode TEXT NOT NULL DEFAULT '',
    hero_label TEXT NOT NULL DEFAULT '',
    source_scope TEXT NOT NULL CHECK (source_scope IN ('all', 'new', 'legacy')),
    metric TEXT NOT NULL CHECK (metric IN ('confirmed', 'candidate')),
    frame_count INTEGER NOT NULL DEFAULT 0 CHECK (frame_count >= 0),
    crop_count INTEGER NOT NULL DEFAULT 0 CHECK (crop_count >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, scene, match_mode, hero_label, source_scope, metric)
);

-- 模型预填与人工真值的版本化对照。每个模型版本只保留该版本在该对象上的
-- 最终人工结论；新版本不会覆盖旧版本，报告也不需要重新解析全部来源 JSON。
CREATE TABLE IF NOT EXISTS training_review_model_outcomes (
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL CHECK (task_id IN (
        'match_flow', 'match_mode', 'hero_select', 'result_detector',
        'hero_avatar_detector', 'hero_identity', 'player_position',
        'afk_status')),
    model_run_id TEXT NOT NULL,
    subject_key TEXT NOT NULL DEFAULT 'frame',
    metric TEXT NOT NULL DEFAULT 'accuracy' CHECK (
        metric IN ('accuracy', 'complete_rate')),
    predicted_label TEXT NOT NULL,
    confirmed_label TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0 CHECK (
        confidence BETWEEN 0 AND 1),
    screen_type TEXT NOT NULL DEFAULT '',
    match_mode TEXT NOT NULL DEFAULT '',
    is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (frame_id, task_id, model_run_id, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_training_review_model_quality
    ON training_review_model_outcomes (
        task_id, model_run_id, is_correct, screen_type, match_mode);
CREATE INDEX IF NOT EXISTS idx_training_review_model_frame
    ON training_review_model_outcomes (frame_id, task_id, subject_key);

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
    is_afk INTEGER CHECK (is_afk IS NULL OR is_afk IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (frame_id, side, slot)
);
CREATE INDEX IF NOT EXISTS idx_training_review_hero_status
    ON training_review_hero_lineups (review_status, updated_at DESC, frame_id);
CREATE INDEX IF NOT EXISTS idx_training_review_hero_confirmed_label
    ON training_review_hero_slots (confirmed_label, frame_id);
CREATE INDEX IF NOT EXISTS idx_training_review_hero_suggested_label
    ON training_review_hero_slots (suggested_label, frame_id);

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

-- 拆分部署角色之间共享的轻量运行状态。每个服务只保留最新快照，
-- 不记录历史事件，避免高频进度更新无限增长。
CREATE TABLE IF NOT EXISTS service_runtime_states (
    service_key TEXT PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

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


def connect_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _prepare_training_review_match_columns(conn)
    _prepare_training_review_prefill_columns(conn)
    _migrate_training_review_context_labels(conn)
    conn.executescript(_SCHEMA)
    conn.executemany(
        'INSERT OR IGNORE INTO annotation_tasks (id, name, description) '
        'VALUES (?, ?, ?)',
        DEFAULT_TASKS,
    )
    _migrate(conn)
    conn.commit()
    return conn


def connect(db_path: Path) -> Any:
    if config.DATABASE_URL:
        from . import postgres

        return postgres.connect(
            config.DATABASE_URL,
            schema=config.DATABASE_SCHEMA,
            schema_sql=_SCHEMA,
            default_tasks=DEFAULT_TASKS,
            pool_size=config.DATABASE_POOL_SIZE,
        )
    return connect_sqlite(db_path)


def close_connections() -> None:
    if not config.DATABASE_URL:
        return
    from . import postgres

    postgres.close_pool()


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
    _migrate_training_review_context_labels(conn)
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
    _migrate_training_review_afk_slots(conn)
    _migrate_training_review_player_slot(conn)
    _prepare_training_review_match_columns(conn)
    _prepare_training_review_prefill_columns(conn)
    repair_managed_paths(conn)


def _migrate_training_review_context_labels(conn: sqlite3.Connection) -> None:
    """扩展统一复核真值枚举，并原样保留旧人工标签。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ('training_review_items',),
    ).fetchone()
    if row is None:
        return
    table_sql = str(row['sql'] or '')
    columns = {
        str(value['name'])
        for value in conn.execute('PRAGMA table_info(training_review_items)')
    }
    if (
        'match_kind_label' in columns
        and 'view_context_label' in columns
        and "'blitz'" in table_sql
        and "'select_blitz'" in table_sql
    ):
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_training_review_context '
            'ON training_review_items ('
            'match_kind_label, view_context_label, review_status, frame_id)'
        )
        return

    target_columns = (
        'frame_id',
        'match_flow_label',
        'match_mode_label',
        'match_kind_label',
        'view_context_label',
        'hero_select_label',
        'hero_select_variant',
        'hero_select_visibility',
        'result_panel_label',
        'hero_layout_label',
        'panel_render_state',
        'ocr_usable',
        'result_occlusion',
        'occluder_types',
        'review_status',
        'notes',
        'created_at',
        'updated_at',
        'reviewed_at',
    )
    conn.execute('DROP TABLE IF EXISTS training_review_items_new')
    conn.execute(
        """
        CREATE TABLE training_review_items_new (
            frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
            match_flow_label TEXT CHECK (
                match_flow_label IS NULL OR match_flow_label IN (
                    'match_flow', 'not_match_flow', 'unreadable')),
            match_mode_label TEXT CHECK (
                match_mode_label IS NULL OR match_mode_label IN (
                    '3v3', 'aram', '5v5', 'blitz', 'unreadable')),
            match_kind_label TEXT CHECK (
                match_kind_label IS NULL OR match_kind_label IN (
                    'pvp', 'bot', 'practice', 'unreadable')),
            view_context_label TEXT CHECK (
                view_context_label IS NULL OR view_context_label IN (
                    'played', 'spectated', 'replay', 'unreadable')),
            hero_select_label TEXT CHECK (
                hero_select_label IS NULL OR hero_select_label IN (
                    'not_select', 'select_3v3', 'select_aram', 'select_5v5',
                    'select_blitz', 'unreadable')),
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
        )
        """
    )
    missing_defaults = {
        'panel_render_state': "'clear'",
        'ocr_usable': "'yes'",
        'result_occlusion': "'none'",
        'occluder_types': "'[]'",
        'review_status': "'pending'",
        'notes': "''",
        'created_at': "datetime('now')",
        'updated_at': "datetime('now')",
    }
    select_columns = [
        column if column in columns else missing_defaults.get(column, 'NULL')
        for column in target_columns
    ]
    conn.execute(
        'INSERT INTO training_review_items_new ('
        + ','.join(target_columns)
        + ') SELECT '
        + ','.join(select_columns)
        + ' FROM training_review_items'
    )
    conn.execute('DROP TABLE training_review_items')
    conn.execute(
        'ALTER TABLE training_review_items_new RENAME TO training_review_items'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_status '
        'ON training_review_items (review_status, updated_at DESC, frame_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_context '
        'ON training_review_items ('
        'match_kind_label, view_context_label, review_status, frame_id)'
    )


def _prepare_training_review_match_columns(conn: sqlite3.Connection) -> None:
    """在创建新索引前给旧素材索引补齐同局关联列。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ('training_review_material_index',),
    ).fetchone()
    if exists is None:
        return
    columns = {
        str(row['name'])
        for row in conn.execute('PRAGMA table_info(training_review_material_index)')
    }
    additions = (
        ('session_id', 'INTEGER NOT NULL DEFAULT 0 CHECK (session_id >= 0)'),
        ('part_id', 'INTEGER NOT NULL DEFAULT 0 CHECK (part_id >= 0)'),
        ('at_ms', 'INTEGER NOT NULL DEFAULT 0 CHECK (at_ms >= 0)'),
        (
            'linked_match_id',
            'INTEGER REFERENCES training_review_match_contexts(match_id) '
            'ON DELETE SET NULL',
        ),
        (
            'match_link_source',
            "TEXT NOT NULL DEFAULT '' CHECK (match_link_source IN ("
            "'', 'result_archive', 'time_window'))",
        ),
    )
    for column, definition in additions:
        if column not in columns:
            conn.execute(
                'ALTER TABLE training_review_material_index '
                f'ADD COLUMN {column} {definition}'
            )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_material_source_time '
        'ON training_review_material_index (session_id,part_id,at_ms,frame_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_material_match_scene '
        'ON training_review_material_index '
        '(linked_match_id,review_status,scene,frame_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_material_video_link_scene '
        'ON training_review_material_index '
        '(video_id,linked_match_id,review_status,scene,frame_id)'
    )


def _prepare_training_review_prefill_columns(conn: sqlite3.Connection) -> None:
    """给旧素材索引补齐后台预打标生命周期。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ('training_review_material_index',),
    ).fetchone()
    if exists is None:
        return
    columns = {
        str(row['name'])
        for row in conn.execute('PRAGMA table_info(training_review_material_index)')
    }
    initializes_lifecycle = 'prefill_status' not in columns
    additions = (
        (
            'prefill_status',
            "TEXT NOT NULL DEFAULT 'pending' CHECK (prefill_status IN ("
            "'pending','queued','running','ready','failed'))",
        ),
        (
            'prefill_stage',
            "TEXT NOT NULL DEFAULT 'core' CHECK (prefill_stage IN ("
            "'core','hero','complete'))",
        ),
        ('prefill_attempts', 'INTEGER NOT NULL DEFAULT 0 CHECK (prefill_attempts>=0)'),
        ('prefill_error', "TEXT NOT NULL DEFAULT ''"),
        (
            'prefill_screen_type',
            "TEXT NOT NULL DEFAULT '' CHECK (prefill_screen_type IN ("
            "'','gameplay_hud','scoreboard','result_page'))",
        ),
        ('prefill_team_size', 'INTEGER CHECK (prefill_team_size IN (3,5))'),
        ('prefill_updated_at', "TEXT NOT NULL DEFAULT ''"),
        ('prefilled_at', 'TEXT'),
    )
    for column, definition in additions:
        if column not in columns:
            conn.execute(
                'ALTER TABLE training_review_material_index '
                f'ADD COLUMN {column} {definition}'
            )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_material_prefill_queue '
        'ON training_review_material_index('
        'prefill_status,prefill_stage,prefill_attempts,review_status,is_new,'
        'source_created_at DESC,frame_id DESC)'
    )
    if initializes_lifecycle:
        for table in (
            'training_review_material_contributions',
            'training_review_material_totals',
        ):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is not None:
                conn.execute(f"DELETE FROM {table} WHERE metric='candidate'")


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


def _migrate_training_review_afk_slots(conn: sqlite3.Connection) -> None:
    """旧阵容没有挂机结论；保留为 NULL，等待人工补标。"""
    columns = {
        row['name']
        for row in conn.execute('PRAGMA table_info(training_review_hero_slots)')
    }
    if 'is_afk' not in columns:
        conn.execute(
            'ALTER TABLE training_review_hero_slots ADD COLUMN '
            'is_afk INTEGER CHECK (is_afk IS NULL OR is_afk IN (0, 1))'
        )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_training_review_hero_afk '
        'ON training_review_hero_slots (is_afk, frame_id)'
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
    commit: bool = True,
) -> None:
    conn.execute(
        'INSERT INTO audit_log (frame_id, event_id, action, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (frame_id, event_id, action, detail, now()),
    )
    if commit:
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
            inserted_id = getattr(cur, 'lastrowid', None)
            if inserted_id is None:
                inserted_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            ids.append(int(inserted_id))
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
    *,
    commit: bool = True,
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
    if commit:
        conn.commit()


def delete_box(
    conn: sqlite3.Connection, frame_id: int, box_type: str, *, commit: bool = True
) -> None:
    conn.execute(
        'DELETE FROM boxes WHERE frame_id = ? AND box_type = ?', (frame_id, box_type)
    )
    if commit:
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


def save_service_runtime_state(
    conn: sqlite3.Connection, service_key: str, state: Dict[str, Any]
) -> None:
    key = str(service_key).strip()
    if not key:
        raise ValueError('服务运行状态 key 不能为空')
    conn.execute(
        """
        INSERT INTO service_runtime_states (service_key, state_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(service_key) DO UPDATE SET
            state_json=excluded.state_json, updated_at=excluded.updated_at
        """,
        (key, json.dumps(state, ensure_ascii=False, sort_keys=True), now()),
    )
    conn.commit()


def load_service_runtime_state(
    conn: sqlite3.Connection, service_key: str
) -> Dict[str, Any]:
    row = conn.execute(
        'SELECT state_json FROM service_runtime_states WHERE service_key = ?',
        (str(service_key).strip(),),
    ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row['state_json'] or '{}')
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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
          AND a.game_mode IN ('3v3', 'aram', '5v5', 'blitz')
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
    'match_mode': {'3v3', 'aram', '5v5', 'blitz', 'unreadable'},
    'match_kind': {'pvp', 'bot', 'practice', 'unreadable'},
    'view_context': {'played', 'spectated', 'replay', 'unreadable'},
    'hero_select': {
        'not_select',
        'select_3v3',
        'select_aram',
        'select_5v5',
        'select_blitz',
        'unreadable',
    },
    'result_panel': {'result_panel', 'no_result_panel', 'unreadable'},
}
_TRAINING_REVIEW_STATUSES = {'pending', 'partial', 'confirmed', 'skipped'}
_TRAINING_REVIEW_SOURCE_SCOPES = {'all', 'new', 'legacy'}
_TRAINING_REVIEW_MODE_FILTERS = {'3v3', 'aram', '5v5', 'blitz', 'unreadable'}
_TRAINING_REVIEW_KIND_FILTERS = {'pvp', 'bot', 'practice', 'unreadable'}
_TRAINING_REVIEW_VIEW_FILTERS = {'played', 'spectated', 'replay', 'unreadable'}
_TRAINING_REVIEW_REVIEW_REASONS = {
    '',
    'mode_unreadable',
    'mode_conflict',
    'hero_unreadable',
    'hero_conflict',
}
_HERO_SCREEN_TYPES = {'gameplay_hud', 'scoreboard', 'result_page'}
_HERO_LAYOUT_LABELS = _HERO_SCREEN_TYPES | {'none', 'unreadable'}
_HERO_SELECT_VARIANTS = {'bp', 'blind', 'random', 'unreadable'}
_MATERIAL_SUGGESTION_SCENES = (
    ('gameplay_hud', 'HUD', 200),
    ('scoreboard', '积分板', 100),
    ('result_page', '结算界面', 100),
    ('hero_select', '英雄选择', 100),
)
_MATERIAL_SUGGESTION_MODES = (('3v3', '3V3'), ('aram', '大乱斗'), ('5v5', '5V5'))
_MATERIAL_HERO_SCENE_TARGET = 20
_MATERIAL_AFK_TARGETS = {'active': 500, 'afk': 200}
_MISSING_PLAYER_HERO_REVIEW = """
item.review_status = 'confirmed'
AND COALESCE(item.view_context_label, 'played') = 'played'
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
_MISSING_AFK_REVIEW = """
item.review_status = 'confirmed'
AND EXISTS (
    SELECT 1
    FROM training_review_hero_lineups lineup
    JOIN training_review_hero_slots slot ON slot.frame_id = lineup.frame_id
    WHERE lineup.frame_id = item.frame_id
      AND lineup.review_status = 'confirmed'
      AND lineup.screen_type = 'result_page'
      AND slot.is_afk IS NULL
)
"""


def _training_review_reason_condition(review_reason: str) -> Tuple[str, List[Any]]:
    if review_reason not in _TRAINING_REVIEW_REVIEW_REASONS:
        raise ValueError('复查原因筛选无效')
    if not review_reason:
        return '', []
    if review_reason == 'mode_unreadable':
        return "item.match_mode_label = 'unreadable'", []
    if review_reason == 'hero_unreadable':
        return (
            'EXISTS (SELECT 1 FROM training_review_hero_slots slot '
            'WHERE slot.frame_id=item.frame_id '
            "AND slot.confirmed_label='unreadable')",
            [],
        )
    if review_reason == 'hero_conflict':
        return (
            'EXISTS (SELECT 1 FROM training_review_hero_slots slot '
            'WHERE slot.frame_id=item.frame_id '
            "AND COALESCE(slot.confirmed_label,'') NOT IN ('','unreadable') "
            "AND COALESCE(slot.suggested_label,'') NOT IN ('','unreadable') "
            'AND slot.confirmed_label != slot.suggested_label '
            'AND COALESCE(slot.suggestion_confidence,0) >= 0.85)',
            [],
        )
    return (
        'EXISTS (SELECT 1 FROM training_review_sources source '
        'WHERE source.frame_id=item.frame_id '
        "AND source.source_type='new_model_prefill' "
        'AND source.id=(SELECT latest.id FROM training_review_sources latest '
        'WHERE latest.frame_id=item.frame_id '
        "AND latest.source_type='new_model_prefill' "
        'ORDER BY latest.source_created_at DESC,latest.id DESC LIMIT 1) '
        "AND item.match_mode_label IN ('3v3','aram','5v5') "
        "AND json_extract(source.suggestions_json,'$.match_mode.label') "
        "IN ('3v3','aram','5v5') "
        "AND json_extract(source.suggestions_json,'$.match_mode.label') "
        '!= item.match_mode_label '
        "AND CAST(json_extract(source.suggestions_json,'$.match_mode.confidence') "
        'AS REAL) >= 0.85)',
        [],
    )


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
_TRAINING_REVIEW_INDEXED_ARAM_PRIORITY = """
CASE
    WHEN material.selects_aram=1 THEN 0
    WHEN material.suggests_aram=1 OR material.match_mode='aram' THEN 1
    WHEN EXISTS (
        SELECT 1 FROM training_review_material_index known_material
        WHERE known_material.video_id=material.video_id
          AND known_material.review_status='confirmed'
          AND known_material.match_mode='aram'
    ) THEN 2
    ELSE 3
END
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
    stage_for_prefill: bool = False,
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
    suggestions_json = json.dumps(
        normalized_suggestions,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    )
    metadata_json = json.dumps(
        metadata or {}, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )
    normalized_image_path = image_path[:500]
    normalized_source_created_at = max(0, int(source_created_at))
    timestamp = now()
    existing = conn.execute(
        'SELECT source.*,EXISTS('
        'SELECT 1 FROM training_review_items item '
        'WHERE item.frame_id=source.frame_id) AS has_item,EXISTS('
        'SELECT 1 FROM training_review_candidate_inbox inbox '
        'WHERE inbox.frame_id=source.frame_id) AS has_inbox '
        'FROM training_review_sources source '
        'WHERE source.source_type = ? AND source.source_id = ?',
        (normalized_type, normalized_id),
    ).fetchone()
    if existing is not None and all(
        (
            int(existing['frame_id']) == int(frame_id),
            str(existing['image_path']) == normalized_image_path,
            str(existing['suggestions_json']) == suggestions_json,
            str(existing['metadata_json']) == metadata_json,
            int(existing['source_created_at']) == normalized_source_created_at,
            bool(existing['has_item'])
            or (bool(stage_for_prefill) and bool(existing['has_inbox'])),
        )
    ):
        conn.commit()
        return False
    if not stage_for_prefill:
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
            normalized_image_path,
            suggestions_json,
            metadata_json,
            normalized_source_created_at,
            timestamp,
            timestamp,
        ),
    )
    has_item = conn.execute(
        'SELECT 1 FROM training_review_items WHERE frame_id=?', (int(frame_id),)
    ).fetchone()
    if stage_for_prefill and has_item is None:
        conn.execute(
            """
            INSERT INTO training_review_candidate_inbox
                (frame_id, prefill_status, prefill_stage, prefill_attempts,
                 source_created_at, created_at, updated_at)
            VALUES (?, 'pending', 'core', 0, ?, ?, ?)
            ON CONFLICT(frame_id) DO UPDATE SET
                source_created_at=CASE
                    WHEN excluded.source_created_at >
                         training_review_candidate_inbox.source_created_at
                    THEN excluded.source_created_at
                    ELSE training_review_candidate_inbox.source_created_at
                END,
                updated_at=excluded.updated_at
            """,
            (int(frame_id), normalized_source_created_at, timestamp, timestamp),
        )
    else:
        if has_item is not None:
            conn.execute(
                "UPDATE training_review_candidate_inbox SET "
                "prefill_status='promoted',prefill_stage='complete',"
                'updated_at=?,promoted_at=COALESCE(promoted_at,?) '
                'WHERE frame_id=?',
                (timestamp, timestamp, int(frame_id)),
            )
            refresh_training_review_material_index(conn, int(frame_id), commit=False)
    if normalized_type in {'new_model_prefill', 'new_model_hero_prefill'}:
        from . import model_quality

        model_quality.refresh_frame(conn, int(frame_id), commit=False)
    conn.commit()
    return existing is None


def promote_training_review_candidate(
    conn: sqlite3.Connection,
    frame_id: int,
    *,
    refresh_material_index: bool = True,
    commit: bool = True,
) -> bool:
    """核心模型已产生结果时，才把候选图晋级为可人工复核素材。"""
    inbox = conn.execute(
        'SELECT 1 FROM training_review_candidate_inbox WHERE frame_id=?',
        (int(frame_id),),
    ).fetchone()
    existing = conn.execute(
        'SELECT 1 FROM training_review_items WHERE frame_id=?', (int(frame_id),)
    ).fetchone()
    if inbox is None:
        if existing is not None:
            return False
        raise KeyError(frame_id)
    timestamp = now()
    inserted = conn.execute(
        'INSERT INTO training_review_items('
        'frame_id,review_status,created_at,updated_at) '
        "VALUES(?,'pending',?,?) ON CONFLICT(frame_id) DO NOTHING",
        (int(frame_id), timestamp, timestamp),
    ).rowcount
    conn.execute(
        "UPDATE training_review_candidate_inbox SET prefill_status='promoted',"
        "prefill_stage='complete',prefill_error='',updated_at=?,promoted_at=? "
        'WHERE frame_id=?',
        (timestamp, timestamp, int(frame_id)),
    )
    if refresh_material_index:
        refresh_training_review_material_index(conn, int(frame_id), commit=False)
    if commit:
        conn.commit()
    return bool(inserted)


def training_review_candidate_inbox_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        'SELECT prefill_status,COUNT(*) AS count '
        'FROM training_review_candidate_inbox GROUP BY prefill_status'
    ).fetchall()
    statuses = {str(row['prefill_status']): int(row['count']) for row in rows}
    return {'total': sum(statuses.values()), 'statuses': statuses}


def migrate_unprefilled_training_review_candidates(
    conn: sqlite3.Connection, *, dry_run: bool = True
) -> Dict[str, int]:
    """把从未人工复核、也没有新模型预填的 pending 空壳移回候选收件箱。"""
    rows = conn.execute(
        """
        SELECT item.frame_id,COALESCE(MAX(source.source_created_at),0)
               AS source_created_at
        FROM training_review_items item
        JOIN training_review_sources source ON source.frame_id=item.frame_id
        WHERE item.review_status='pending'
          AND item.match_flow_label IS NULL
          AND item.match_mode_label IS NULL
          AND item.hero_select_label IS NULL
          AND item.result_panel_label IS NULL
          AND item.hero_layout_label IS NULL
          AND TRIM(item.notes)=''
          AND NOT EXISTS (
              SELECT 1 FROM audit_log audit
              WHERE audit.frame_id=item.frame_id
                AND audit.action='training_review'
          )
          AND NOT EXISTS (
              SELECT 1 FROM training_review_sources prefill
              WHERE prefill.frame_id=item.frame_id
                AND prefill.source_type IN (
                    'new_model_prefill','new_model_hero_prefill')
          )
          AND NOT EXISTS (
              SELECT 1 FROM training_review_hero_lineups lineup
              WHERE lineup.frame_id=item.frame_id
          )
        GROUP BY item.frame_id
        ORDER BY item.frame_id
        """
    ).fetchall()
    result = {'eligible': len(rows), 'migrated': 0}
    if dry_run or not rows:
        return result
    timestamp = now()
    for offset in range(0, len(rows), 500):
        batch = rows[offset : offset + 500]
        frame_ids = [int(row['frame_id']) for row in batch]
        placeholders = ','.join('?' for _frame_id in frame_ids)
        conn.executemany(
            """
            INSERT INTO training_review_candidate_inbox
                (frame_id,prefill_status,prefill_stage,prefill_attempts,
                 source_created_at,created_at,updated_at)
            VALUES(?,'pending','core',0,?,?,?)
            ON CONFLICT(frame_id) DO UPDATE SET
                prefill_status='pending',prefill_stage='core',
                prefill_attempts=0,prefill_error='',prefill_screen_type='',
                prefill_team_size=NULL,source_created_at=excluded.source_created_at,
                updated_at=excluded.updated_at,promoted_at=NULL
            """,
            [
                (
                    int(row['frame_id']),
                    int(row['source_created_at']),
                    timestamp,
                    timestamp,
                )
                for row in batch
            ],
        )
        conn.execute(
            'DELETE FROM training_review_material_contributions '
            f'WHERE frame_id IN ({placeholders})',
            frame_ids,
        )
        conn.execute(
            'DELETE FROM training_review_material_index '
            f'WHERE frame_id IN ({placeholders})',
            frame_ids,
        )
        deleted = conn.execute(
            'DELETE FROM training_review_items ' f'WHERE frame_id IN ({placeholders})',
            frame_ids,
        ).rowcount
        conn.commit()
        result['migrated'] += int(deleted)
    conn.execute('DELETE FROM training_review_material_totals')
    conn.execute(
        'INSERT INTO training_review_material_totals('
        'kind,scene,match_mode,hero_label,source_scope,metric,'
        'frame_count,crop_count,updated_at) '
        'SELECT kind,scene,match_mode,hero_label,source_scope,metric,'
        'SUM(frame_count),SUM(crop_count),? '
        'FROM training_review_material_contributions '
        'GROUP BY kind,scene,match_mode,hero_label,source_scope,metric',
        (timestamp,),
    )
    conn.commit()
    return result


def _training_review_item_dict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    source_rows: Optional[Sequence[sqlite3.Row]] = None,
    boxes: Optional[Dict[str, Dict[str, float]]] = None,
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
    if source_rows is None:
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
    selection = suggestions.get('hero_select') or {}
    flow = suggestions.get('match_flow') or {}
    selection_applies = (
        str(selection.get('label') or '').startswith('select_')
        and _training_review_float(selection.get('confidence')) >= 0.55
    )
    outside_match = (
        str(flow.get('label') or '') == 'not_match_flow'
        and _training_review_float(flow.get('confidence')) >= 0.55
    )
    if selection_applies or outside_match:
        # 原始来源仍完整保留；这里只隐藏不适用于当前画面的模式建议。
        suggestions.pop('match_mode', None)
    if selection_applies:
        for source in sources:
            metadata = source.get('metadata') or {}
            context = metadata.pop('hero_context_suggestion', None)
            if context is not None:
                metadata['suppressed_hero_context_suggestion'] = context
    item['suggestions'] = suggestions
    item['sources'] = sources
    item['source_count'] = len(sources)
    item['source_categories'] = sorted(
        {_training_review_source_category(source['source_type']) for source in sources}
    )
    item['boxes'] = get_boxes(conn, int(row['frame_id'])) if boxes is None else boxes
    item['needs_player_hero_review'] = bool(item['needs_player_hero_review'])
    item['needs_afk_review'] = bool(item['needs_afk_review'])
    item['unified_manual_reviewed'] = bool(item['unified_manual_reviewed'])
    item['legacy_migration_needs_review'] = bool(
        item['review_status'] == 'confirmed'
        and 'legacy' in item['source_categories']
        and not item['unified_manual_reviewed']
    )
    return item


def _calculate_training_review_result_groups(
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
        WHERE item.result_panel_label = 'result_panel'
           OR (
                item.result_panel_label IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM training_review_sources source
                    WHERE source.frame_id = item.frame_id
                      AND json_extract(
                          source.suggestions_json, '$.result_panel.label'
                      ) = 'result_panel'
                )
           )
        """
    ).fetchall()
    if not rows:
        return {}
    by_frame = {int(row['frame_id']): row for row in rows}
    positive = set(by_frame)
    sources_by_frame: Dict[int, List[Tuple[str, Dict[str, Any]]]] = {}
    positive_ids = sorted(positive)
    for offset in range(0, len(positive_ids), 500):
        batch = positive_ids[offset : offset + 500]
        placeholders = ', '.join('?' for _frame_id in batch)
        sources = conn.execute(
            'SELECT frame_id, source_type, metadata_json '
            'FROM training_review_sources '
            f'WHERE frame_id IN ({placeholders})',
            batch,
        ).fetchall()
        for source in sources:
            frame_id = int(source['frame_id'])
            try:
                metadata = json.loads(source['metadata_json'] or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict):
                sources_by_frame.setdefault(frame_id, []).append(
                    (str(source['source_type']), metadata)
                )

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


def training_review_result_groups(
    conn: sqlite3.Connection, *, allow_partial_index: bool = False
) -> Dict[int, Dict[str, Any]]:
    """优先读取已回填的代表图索引；旧库回填前保留兼容计算。"""
    if allow_partial_index or training_review_material_index_complete(conn):
        rows = conn.execute(
            'SELECT frame_id,result_group_representative_frame_id,'
            'result_group_size FROM training_review_material_index '
            'WHERE result_group_size>1'
        ).fetchall()
        return {
            int(row['frame_id']): {
                'result_group_size': int(row['result_group_size']),
                'result_group_representative_frame_id': int(
                    row['result_group_representative_frame_id']
                ),
            }
            for row in rows
        }
    return _calculate_training_review_result_groups(conn)


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


def _training_review_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or '{}'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _training_review_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _training_review_material_signals(
    source_rows: Sequence[sqlite3.Row],
) -> Dict[int, Dict[str, Any]]:
    signals: Dict[int, Dict[str, Any]] = {}
    for row in source_rows:
        frame_id = int(row['frame_id'])
        signal = signals.setdefault(
            frame_id, {'suggestions': {}, 'suggestion_ranks': {}, 'metadata': []}
        )
        for task in ('hero_select', 'match_mode', 'result_panel'):
            label = row['{}_suggestion_label'.format(task)]
            if label is None:
                continue
            confidence = _training_review_float(
                row['{}_suggestion_confidence'.format(task)]
            )
            if confidence <= signal['suggestion_ranks'].get(task, -1.0):
                continue
            signal['suggestions'][task] = {
                'label': str(label),
                'confidence': confidence,
            }
            signal['suggestion_ranks'][task] = confidence
        metadata: Dict[str, Any] = {}
        screen_type = row['hero_context_screen_type']
        if screen_type is not None:
            metadata['hero_context_suggestion'] = {
                'screen_type': str(screen_type),
                'confidence': _training_review_float(row['hero_context_confidence']),
            }
        for key in ('game_mode', 'mode_class'):
            value = row[key]
            if value is not None:
                metadata[key] = str(value)
        for key in (
            'source_screen_type',
            'source_stage_class',
            'model_stage_class',
            'model_mode_class',
        ):
            value = row[key]
            if value is not None:
                metadata[key] = str(value)
        corrected_mode = row['manual_game_mode']
        if corrected_mode is not None:
            metadata['manual_correction'] = {
                'after': {'game_mode': str(corrected_mode)}
            }
        if metadata:
            signal['metadata'].append(metadata)
    return signals


def _training_review_material_scene(
    row: sqlite3.Row, signal: Dict[str, Any]
) -> Optional[str]:
    hero_select = str(row['hero_select_label'] or '')
    if hero_select.startswith('select_'):
        return 'hero_select'
    hero_layout = str(row['hero_layout_label'] or '')
    if hero_layout in _HERO_SCREEN_TYPES:
        return hero_layout
    if str(row['result_panel_label'] or '') == 'result_panel':
        return 'result_page'
    if str(row['match_flow_label'] or '') in {
        'not_match_flow',
        'unreadable',
    } or hero_layout in {'none', 'unreadable'}:
        return None

    suggestions = signal.get('suggestions') or {}
    select_suggestion = suggestions.get('hero_select') or {}
    if str(select_suggestion.get('label') or '').startswith('select_'):
        return 'hero_select'
    flow_suggestion = str((suggestions.get('match_flow') or {}).get('label') or '')
    if flow_suggestion in {'not_match_flow', 'unreadable'}:
        return None
    result_suggestion = suggestions.get('result_panel') or {}
    if str(result_suggestion.get('label') or '') == 'result_panel':
        return 'result_page'
    contexts = [
        metadata.get('hero_context_suggestion')
        for metadata in signal.get('metadata') or []
    ]
    contexts = [context for context in contexts if isinstance(context, dict)]
    if contexts:
        context = max(
            contexts, key=lambda value: _training_review_float(value.get('confidence'))
        )
        screen_type = str(context.get('screen_type') or '')
        if screen_type in _HERO_SCREEN_TYPES:
            return screen_type
    for metadata in signal.get('metadata') or []:
        values = {
            str(metadata.get(key) or '')
            for key in ('source_screen_type', 'source_stage_class', 'model_stage_class')
        }
        if values & {'scoreboard', 'death_scoreboard'}:
            return 'scoreboard'
        if values & {'gameplay', 'gameplay_hud', 'in_match'}:
            return 'gameplay_hud'
        if 'result_page' in values:
            return 'result_page'
    return None


def _training_review_mode_from_select(value: Any) -> Optional[str]:
    labels = {
        'select_3v3': '3v3',
        'select_aram': 'aram',
        'select_5v5': '5v5',
        'select_blitz': 'blitz',
    }
    return labels.get(str(value or ''))


def _training_review_material_mode(
    row: sqlite3.Row, signal: Dict[str, Any]
) -> Optional[str]:
    match_mode = str(row['match_mode_label'] or '')
    if match_mode in {'3v3', 'aram', '5v5', 'blitz'}:
        return match_mode
    select_mode = _training_review_mode_from_select(row['hero_select_label'])
    if select_mode is not None:
        return select_mode
    if (
        str(row['match_flow_label'] or '') in {'not_match_flow', 'unreadable'}
        or str(row['match_mode_label'] or '') == 'unreadable'
    ):
        return None

    suggestions = signal.get('suggestions') or {}
    select_mode = _training_review_mode_from_select(
        (suggestions.get('hero_select') or {}).get('label')
    )
    if select_mode is not None:
        return select_mode
    flow_suggestion = str((suggestions.get('match_flow') or {}).get('label') or '')
    if flow_suggestion in {'not_match_flow', 'unreadable'}:
        return None
    mode_suggestion = str((suggestions.get('match_mode') or {}).get('label') or '')
    if mode_suggestion in {'3v3', 'aram', '5v5', 'blitz'}:
        return mode_suggestion
    for metadata in signal.get('metadata') or []:
        for value in (
            metadata.get('game_mode'),
            metadata.get('mode_class'),
            metadata.get('model_mode_class'),
        ):
            if str(value or '') in {'3v3', 'aram', '5v5', 'blitz'}:
                return str(value)
        correction = metadata.get('manual_correction')
        if isinstance(correction, dict):
            after = correction.get('after')
            if isinstance(after, dict):
                value = str(after.get('game_mode') or '')
                if value in {'3v3', 'aram', '5v5', 'blitz'}:
                    return value
    return None


def _training_review_material_source_facts(
    conn: sqlite3.Connection, frame_id: int
) -> Dict[str, Any]:
    rows = conn.execute(
        'SELECT source_type,suggestions_json,metadata_json,source_created_at '
        'FROM training_review_sources WHERE frame_id=?',
        (int(frame_id),),
    ).fetchall()
    signal: Dict[str, Any] = {'suggestions': {}, 'suggestion_ranks': {}, 'metadata': []}
    categories: set[str] = set()
    confidences: List[float] = []
    source_created_at = 0
    source_offset = 0
    selects_aram = False
    suggests_aram = False
    session_id = 0
    part_id = 0
    at_ms = 0
    match_contexts: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        source_type = str(row['source_type'] or '')
        categories.add(_training_review_source_category(source_type))
        source_created_at = max(source_created_at, int(row['source_created_at'] or 0))
        suggestions = _training_review_json_object(row['suggestions_json'])
        metadata = _training_review_json_object(row['metadata_json'])
        try:
            session_id = max(session_id, int(metadata.get('session_id') or 0))
            part_id = max(part_id, int(metadata.get('part_id') or 0))
        except (TypeError, ValueError):
            pass
        for key in ('at_ms', 'result_at_ms'):
            try:
                at_ms = max(at_ms, int(metadata.get(key) or 0))
            except (TypeError, ValueError):
                pass
        correction = metadata.get('manual_correction')
        correction = correction if isinstance(correction, dict) else {}
        try:
            match_id = int(metadata.get('match_id') or correction.get('match_id') or 0)
            context_session_id = int(metadata.get('session_id') or 0)
            context_part_id = int(metadata.get('part_id') or 0)
            result_at_ms = int(
                metadata.get('result_at_ms') or metadata.get('at_ms') or -1
            )
            duration_ms = max(0, int(metadata.get('duration_seconds') or 0)) * 1_000
            raw_started_at = metadata.get('started_at_ms')
            if raw_started_at is None:
                raw_started_at = metadata.get('segment_start_ms')
            started_at_ms = (
                max(0, result_at_ms - duration_ms)
                if raw_started_at is None
                else max(0, int(raw_started_at))
            )
        except (TypeError, ValueError):
            match_id = 0
            context_session_id = 0
            context_part_id = 0
            result_at_ms = -1
            started_at_ms = 0
        context_source = (
            source_type
            if source_type in {'result_archive', 'manual_correction'}
            else ''
        )
        if (
            context_source
            and match_id > 0
            and context_session_id > 0
            and context_part_id > 0
            and result_at_ms >= started_at_ms
        ):
            mode = str(metadata.get('game_mode') or '')
            after = correction.get('after')
            if mode not in {'3v3', 'aram', '5v5', 'blitz'} and isinstance(after, dict):
                mode = str(after.get('game_mode') or '')
            match_contexts[match_id] = {
                'match_id': match_id,
                'session_id': context_session_id,
                'part_id': context_part_id,
                'started_at_ms': started_at_ms,
                'result_at_ms': result_at_ms,
                'game_mode': (mode if mode in {'3v3', 'aram', '5v5', 'blitz'} else ''),
                'source_type': context_source,
            }
        signal_metadata = dict(metadata)
        if metadata.get('screen_type') is not None:
            signal_metadata['source_screen_type'] = str(metadata['screen_type'])
        if metadata.get('stage_class') is not None:
            signal_metadata['source_stage_class'] = str(metadata['stage_class'])
        outputs = metadata.get('model_outputs')
        if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict):
            if outputs[0].get('stage_class') is not None:
                signal_metadata['model_stage_class'] = str(outputs[0]['stage_class'])
            if outputs[0].get('mode_class') is not None:
                signal_metadata['model_mode_class'] = str(outputs[0]['mode_class'])
        signal['metadata'].append(signal_metadata)
        for task, raw in suggestions.items():
            if not isinstance(raw, dict):
                continue
            label = str(raw.get('label') or '')
            confidence = _training_review_float(raw.get('confidence'))
            confidences.append(confidence)
            if confidence > signal['suggestion_ranks'].get(task, -1.0):
                signal['suggestions'][task] = {'label': label, 'confidence': confidence}
                signal['suggestion_ranks'][task] = confidence
            if task == 'hero_select' and label == 'select_aram':
                selects_aram = True
            if task == 'match_mode' and label == 'aram':
                suggests_aram = True
        for key in ('at_ms', 'result_at_ms'):
            try:
                source_offset = max(source_offset, int(metadata.get(key) or 0))
            except (TypeError, ValueError):
                pass
        for key in ('game_mode', 'mode_class'):
            if str(metadata.get(key) or '') == 'aram':
                suggests_aram = True
        if isinstance(outputs, list):
            for output in outputs:
                if not isinstance(output, dict):
                    continue
                if str(output.get('mode_class') or '') == 'aram':
                    suggests_aram = True
    return {
        'signal': signal,
        'categories': categories,
        'source_created_at': source_created_at,
        'source_offset': source_offset,
        'has_low_confidence': any(value < 0.6 for value in confidences),
        'has_boundary_confidence': any(0.6 <= value <= 0.85 for value in confidences),
        'has_high_confidence': any(value >= 0.85 for value in confidences),
        'selects_aram': selects_aram,
        'suggests_aram': suggests_aram,
        'session_id': session_id,
        'part_id': part_id,
        'at_ms': at_ms,
        'match_contexts': tuple(match_contexts.values()),
    }


def _training_review_match_for_time(
    conn: sqlite3.Connection, *, session_id: int, part_id: int, at_ms: int
) -> Optional[int]:
    if session_id <= 0 or part_id <= 0 or at_ms < 0:
        return None
    row = conn.execute(
        'SELECT match_id FROM training_review_match_contexts '
        'WHERE session_id=? AND part_id=? AND started_at_ms<=? '
        'AND result_at_ms+30000>=? '
        'ORDER BY (result_at_ms-started_at_ms),result_at_ms,match_id LIMIT 1',
        (int(session_id), int(part_id), int(at_ms), int(at_ms)),
    ).fetchone()
    return None if row is None else int(row['match_id'])


def _relink_training_review_material_matches(
    conn: sqlite3.Connection, *, session_id: int, part_id: int
) -> None:
    rows = conn.execute(
        'SELECT material.frame_id,('
        'SELECT context.match_id FROM training_review_match_contexts context '
        'WHERE context.session_id=material.session_id '
        'AND context.part_id=material.part_id '
        'AND context.started_at_ms<=material.at_ms '
        'AND context.result_at_ms+30000>=material.at_ms '
        'ORDER BY (context.result_at_ms-context.started_at_ms),'
        'context.result_at_ms,context.match_id LIMIT 1) AS match_id '
        'FROM training_review_material_index material '
        'WHERE material.session_id=? AND material.part_id=?',
        (int(session_id), int(part_id)),
    ).fetchall()
    updates = [
        (
            row['match_id'],
            'time_window' if row['match_id'] is not None else '',
            int(row['frame_id']),
        )
        for row in rows
    ]
    if updates:
        conn.executemany(
            'UPDATE training_review_material_index SET linked_match_id=?, '
            'match_link_source=? WHERE frame_id=?',
            updates,
        )


def _upsert_training_review_match_context(
    conn: sqlite3.Connection, context: Mapping[str, Any]
) -> bool:
    match_id = int(context['match_id'])
    game_mode = str(context['game_mode'])
    if game_mode == 'blitz' and getattr(conn, 'dialect', 'sqlite') == 'sqlite':
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ('training_review_match_contexts',),
        ).fetchone()
        if schema is not None and "'blitz'" not in str(schema['sql'] or ''):
            game_mode = ''
    values = (
        int(context['session_id']),
        int(context['part_id']),
        int(context['started_at_ms']),
        int(context['result_at_ms']),
        game_mode,
        str(context['source_type']),
    )
    current = conn.execute(
        'SELECT session_id,part_id,started_at_ms,result_at_ms,game_mode,source_type '
        'FROM training_review_match_contexts WHERE match_id=?',
        (match_id,),
    ).fetchone()
    current_values = (
        None
        if current is None
        else tuple(
            current[name]
            for name in (
                'session_id',
                'part_id',
                'started_at_ms',
                'result_at_ms',
                'game_mode',
                'source_type',
            )
        )
    )
    if current_values == values:
        return False
    conn.execute(
        'INSERT INTO training_review_match_contexts('
        'match_id,session_id,part_id,started_at_ms,result_at_ms,game_mode,'
        'source_type,updated_at) VALUES(?,?,?,?,?,?,?,?) '
        'ON CONFLICT(match_id) DO UPDATE SET '
        'session_id=excluded.session_id,part_id=excluded.part_id,'
        'started_at_ms=excluded.started_at_ms,result_at_ms=excluded.result_at_ms,'
        'game_mode=excluded.game_mode,source_type=excluded.source_type,'
        'updated_at=excluded.updated_at',
        (match_id, *values, now()),
    )
    _relink_training_review_material_matches(
        conn, session_id=int(context['session_id']), part_id=int(context['part_id'])
    )
    return True


def _training_review_material_contributions(
    conn: sqlite3.Connection, index_row: Mapping[str, Any]
) -> Dict[Tuple[str, str, str, str, str, str], Tuple[int, int]]:
    if int(index_row['result_group_representative_frame_id']) != int(
        index_row['frame_id']
    ):
        return {}
    frame_id = int(index_row['frame_id'])
    status = str(index_row['review_status'])
    scene = str(index_row['scene'])
    match_mode = str(index_row['match_mode'])
    contributions: Dict[Tuple[str, str, str, str, str, str], Tuple[int, int]] = {}

    def add(
        kind: str,
        target_scene: str,
        target_mode: str,
        hero_label: str,
        source_scope: str,
        metric: str,
        *,
        frame_count: int,
        crop_count: int,
    ) -> None:
        key = (kind, target_scene, target_mode, hero_label, source_scope, metric)
        old_frame_count, old_crop_count = contributions.get(key, (0, 0))
        contributions[key] = (
            old_frame_count + frame_count,
            old_crop_count + crop_count,
        )

    valid_scene_modes = {
        (target_scene, target_mode)
        for target_scene, _label, _minimum in _MATERIAL_SUGGESTION_SCENES
        for target_mode, _mode_label in _MATERIAL_SUGGESTION_MODES
    }
    if (scene, match_mode) in valid_scene_modes:
        if status == 'confirmed':
            add(
                'scene_mode',
                scene,
                match_mode,
                '',
                'all',
                'confirmed',
                frame_count=1,
                crop_count=0,
            )
        elif (
            status in {'pending', 'partial'}
            and str(index_row['prefill_status']) == 'ready'
        ):
            for source_scope, enabled in (
                ('new', int(index_row['is_new'])),
                ('legacy', int(index_row['is_legacy'])),
            ):
                if enabled:
                    add(
                        'scene_mode',
                        scene,
                        match_mode,
                        '',
                        source_scope,
                        'candidate',
                        frame_count=1,
                        crop_count=0,
                    )

    lineup = conn.execute(
        'SELECT screen_type,review_status FROM training_review_hero_lineups '
        'WHERE frame_id=?',
        (frame_id,),
    ).fetchone()
    if lineup is None or str(lineup['screen_type']) not in _HERO_SCREEN_TYPES:
        return contributions
    hero_scene = str(lineup['screen_type'])
    slots = conn.execute(
        'SELECT suggested_label,confirmed_label FROM training_review_hero_slots '
        'WHERE frame_id=?',
        (frame_id,),
    ).fetchall()
    if str(lineup['review_status']) == 'confirmed':
        labels: Dict[str, int] = {}
        for slot in slots:
            label = str(slot['confirmed_label'] or '')
            if label in {'', 'unreadable'}:
                continue
            labels[label] = labels.get(label, 0) + 1
        for label, crop_count in labels.items():
            add(
                'hero_scene',
                hero_scene,
                '',
                label,
                'all',
                'confirmed',
                frame_count=1,
                crop_count=crop_count,
            )
    elif (
        status in {'pending', 'partial'}
        and str(index_row['prefill_status']) == 'ready'
        and hero_scene == scene
    ):
        labels = {}
        for slot in slots:
            label = str(slot['suggested_label'] or '')
            if label in {'', 'unreadable'}:
                continue
            labels[label] = labels.get(label, 0) + 1
        for label, crop_count in labels.items():
            for source_scope, enabled in (
                ('new', int(index_row['is_new'])),
                ('legacy', int(index_row['is_legacy'])),
            ):
                if enabled:
                    add(
                        'hero_scene',
                        hero_scene,
                        '',
                        label,
                        source_scope,
                        'candidate',
                        frame_count=1,
                        crop_count=crop_count,
                    )
    return contributions


def _replace_training_review_material_contributions(
    conn: sqlite3.Connection,
    frame_id: int,
    values: Mapping[Tuple[str, str, str, str, str, str], Tuple[int, int]],
) -> None:
    old_rows = conn.execute(
        'SELECT kind,scene,match_mode,hero_label,source_scope,metric,'
        'frame_count,crop_count FROM training_review_material_contributions '
        'WHERE frame_id=?',
        (int(frame_id),),
    ).fetchall()
    old = {
        (
            str(row['kind']),
            str(row['scene']),
            str(row['match_mode']),
            str(row['hero_label']),
            str(row['source_scope']),
            str(row['metric']),
        ): (int(row['frame_count']), int(row['crop_count']))
        for row in old_rows
    }
    timestamp = now()
    deltas = []
    for key in set(old) | set(values):
        old_frame_count, old_crop_count = old.get(key, (0, 0))
        new_frame_count, new_crop_count = values.get(key, (0, 0))
        frame_delta = new_frame_count - old_frame_count
        crop_delta = new_crop_count - old_crop_count
        if frame_delta or crop_delta:
            deltas.append((*key, frame_delta, crop_delta, timestamp))
    if deltas:
        insertable = [
            (*row[:6], 0, 0, row[8])
            for row in deltas
            if int(row[6]) >= 0 and int(row[7]) >= 0
        ]
        if insertable:
            conn.executemany(
                'INSERT INTO training_review_material_totals('
                'kind,scene,match_mode,hero_label,source_scope,metric,'
                'frame_count,crop_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(kind,scene,match_mode,hero_label,source_scope,metric) '
                'DO NOTHING',
                insertable,
            )
        updated = conn.executemany(
            'UPDATE training_review_material_totals SET '
            'frame_count=frame_count+?,crop_count=crop_count+?,updated_at=? '
            'WHERE kind=? AND scene=? AND match_mode=? AND hero_label=? '
            'AND source_scope=? AND metric=?',
            [(row[6], row[7], row[8], *row[:6]) for row in deltas],
        )
        if updated.rowcount != len(deltas):
            raise RuntimeError('训练素材增量统计缺少原始记录')
        if conn.execute(
            'SELECT 1 FROM training_review_material_totals '
            'WHERE frame_count<0 OR crop_count<0 LIMIT 1'
        ).fetchone():
            raise RuntimeError('训练素材增量统计出现负数')
        conn.execute(
            'DELETE FROM training_review_material_totals '
            'WHERE frame_count=0 AND crop_count=0'
        )
    conn.execute(
        'DELETE FROM training_review_material_contributions WHERE frame_id=?',
        (int(frame_id),),
    )
    if values:
        conn.executemany(
            'INSERT INTO training_review_material_contributions('
            'frame_id,kind,scene,match_mode,hero_label,source_scope,metric,'
            'frame_count,crop_count) VALUES(?,?,?,?,?,?,?,?,?)',
            [
                (int(frame_id), *key, frame_count, crop_count)
                for key, (frame_count, crop_count) in values.items()
            ],
        )


def _refresh_training_review_event_group(
    conn: sqlite3.Connection, frame_id: int
) -> set[int]:
    event = conn.execute(
        'SELECT event_id FROM frames WHERE id=?', (int(frame_id),)
    ).fetchone()
    if event is None or event['event_id'] is None:
        return {int(frame_id)}
    rows = conn.execute(
        """
        SELECT material.frame_id,material.scene,item.review_status,
               item.hero_layout_label,item.panel_render_state,item.ocr_usable,
               item.result_occlusion,frame.timestamp_ms,frame.is_representative,
               frame.model_confidence,
               EXISTS (
                   SELECT 1 FROM boxes box
                   WHERE box.frame_id=material.frame_id
                     AND box.box_type='result_panel'
               ) AS has_result_box,
               EXISTS (
                   SELECT 1 FROM training_review_hero_lineups lineup
                   WHERE lineup.frame_id=material.frame_id
                     AND lineup.review_status='confirmed'
                     AND lineup.player_status='identified'
               ) AS has_complete_lineup
        FROM training_review_material_index material
        JOIN training_review_items item ON item.frame_id=material.frame_id
        JOIN frames frame ON frame.id=material.frame_id
        WHERE frame.event_id=?
        """,
        (int(event['event_id']),),
    ).fetchall()
    affected = {int(row['frame_id']) for row in rows}
    if not affected:
        return {int(frame_id)}
    conn.executemany(
        'UPDATE training_review_material_index SET '
        'result_group_representative_frame_id=frame_id,result_group_size=1 '
        'WHERE frame_id=?',
        [(value,) for value in affected],
    )
    positives = [row for row in rows if str(row['scene']) == 'result_page']
    if len(positives) < 2:
        return affected
    timestamps = sorted(int(row['timestamp_ms']) for row in positives)
    median = timestamps[len(timestamps) // 2]

    def rank(row: Mapping[str, Any]) -> Tuple[Any, ...]:
        return (
            int(row['has_complete_lineup']),
            int(str(row['review_status']) == 'confirmed'),
            int(str(row['hero_layout_label'] or '') == 'result_page'),
            int(row['has_result_box']),
            int(str(row['panel_render_state']) == 'clear'),
            int(str(row['ocr_usable']) == 'yes'),
            int(str(row['result_occlusion']) == 'none'),
            int(row['is_representative']),
            float(row['model_confidence'] or 0),
            -abs(int(row['timestamp_ms']) - median),
            -int(row['frame_id']),
        )

    representative = int(max(positives, key=rank)['frame_id'])
    conn.executemany(
        'UPDATE training_review_material_index SET '
        'result_group_representative_frame_id=?,result_group_size=? '
        'WHERE frame_id=?',
        [(representative, len(positives), int(row['frame_id'])) for row in positives],
    )
    return affected


def refresh_training_review_material_index(
    conn: sqlite3.Connection, frame_id: int, *, commit: bool = True
) -> bool:
    """按当前人工真值与模型来源重算单帧索引；可安全重复调用。"""
    item = conn.execute(
        'SELECT item.*,frame.video_id,frame.timestamp_ms AS frame_timestamp_ms '
        'FROM training_review_items item '
        'JOIN frames frame ON frame.id=item.frame_id WHERE item.frame_id=?',
        (int(frame_id),),
    ).fetchone()
    if item is None:
        _replace_training_review_material_contributions(conn, int(frame_id), {})
        conn.execute(
            'DELETE FROM training_review_material_index WHERE frame_id=?',
            (int(frame_id),),
        )
        if commit:
            conn.commit()
        return False
    source_facts = _training_review_material_source_facts(conn, int(frame_id))
    contexts = tuple(source_facts['match_contexts'])
    for context in contexts:
        _upsert_training_review_match_context(conn, context)
    signal = source_facts['signal']
    categories = source_facts['categories']
    scene = _training_review_material_scene(item, signal) or 'other'
    match_mode = _training_review_material_mode(item, signal) or ''
    if match_mode == 'blitz' and getattr(conn, 'dialect', 'sqlite') == 'sqlite':
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ('training_review_material_index',),
        ).fetchone()
        if schema is not None and "'blitz'" not in str(schema['sql'] or ''):
            match_mode = ''
    session_id = int(source_facts['session_id'])
    part_id = int(source_facts['part_id'])
    at_ms = int(source_facts['at_ms'] or item['frame_timestamp_ms'] or 0)
    exact_match_id = int(contexts[0]['match_id']) if len(contexts) == 1 else None
    linked_match_id = exact_match_id or _training_review_match_for_time(
        conn, session_id=session_id, part_id=part_id, at_ms=at_ms
    )
    match_link_source = (
        'result_archive'
        if exact_match_id is not None
        else 'time_window' if linked_match_id is not None else ''
    )
    existing = conn.execute(
        'SELECT result_group_representative_frame_id,result_group_size '
        'FROM training_review_material_index WHERE frame_id=?',
        (int(frame_id),),
    ).fetchone()
    representative = (
        int(frame_id)
        if existing is None
        else int(existing['result_group_representative_frame_id'])
    )
    group_size = 1 if existing is None else int(existing['result_group_size'])
    values = {
        'frame_id': int(frame_id),
        'video_id': int(item['video_id']),
        'session_id': session_id,
        'part_id': part_id,
        'at_ms': at_ms,
        'linked_match_id': linked_match_id,
        'match_link_source': match_link_source,
        'review_status': str(item['review_status']),
        'scene': scene,
        'match_mode': match_mode,
        'is_new': int(
            bool(categories & {'worker', 'result_archive', 'manual_correction'})
        ),
        'is_legacy': int('legacy' in categories),
        'has_worker': int('worker' in categories),
        'has_result_archive': int('result_archive' in categories),
        'has_manual_correction': int('manual_correction' in categories),
        'has_model_prefill': int('model_prefill' in categories),
        'has_hero_model_prefill': int('hero_model_prefill' in categories),
        'has_low_confidence': int(source_facts['has_low_confidence']),
        'has_boundary_confidence': int(source_facts['has_boundary_confidence']),
        'has_high_confidence': int(source_facts['has_high_confidence']),
        'selects_aram': int(source_facts['selects_aram']),
        'suggests_aram': int(source_facts['suggests_aram']),
        'source_created_at': int(source_facts['source_created_at']),
        'source_offset': int(source_facts['source_offset']),
        'result_group_representative_frame_id': representative,
        'result_group_size': group_size,
        'updated_at': now(),
    }
    columns = tuple(values)
    conn.execute(
        'INSERT INTO training_review_material_index('
        + ','.join(columns)
        + ') VALUES('
        + ','.join('?' for _column in columns)
        + ') ON CONFLICT(frame_id) DO UPDATE SET '
        + ','.join(
            '{}=excluded.{}'.format(column, column)
            for column in columns
            if column != 'frame_id'
        ),
        tuple(values[column] for column in columns),
    )
    affected = _refresh_training_review_event_group(conn, int(frame_id))
    for affected_frame_id in affected:
        indexed = conn.execute(
            'SELECT * FROM training_review_material_index WHERE frame_id=?',
            (affected_frame_id,),
        ).fetchone()
        if indexed is None:
            continue
        _replace_training_review_material_contributions(
            conn,
            affected_frame_id,
            _training_review_material_contributions(conn, indexed),
        )
    if commit:
        conn.commit()
    return True


def rebuild_training_review_material_index(
    conn: sqlite3.Connection,
    *,
    batch_size: int = 500,
    progress: Optional[Callable[[Dict[str, int]], None]] = None,
) -> Dict[str, int]:
    """从现有真值和来源续建索引；中断后可重跑且不会重复累计。"""
    if batch_size < 1:
        raise ValueError('素材索引回填批次必须大于零')
    conn.execute('DELETE FROM training_review_match_contexts')
    conn.execute(
        "UPDATE training_review_material_index SET linked_match_id=NULL,"
        "match_link_source=''"
    )
    conn.commit()
    frame_ids = [
        int(row['frame_id'])
        for row in conn.execute(
            'SELECT frame_id FROM training_review_items ORDER BY frame_id'
        ).fetchall()
    ]
    result_groups = _calculate_training_review_result_groups(conn)
    stale_frame_ids = {
        int(row['frame_id'])
        for row in conn.execute(
            'SELECT material.frame_id FROM training_review_material_index material '
            'LEFT JOIN training_review_items item ON item.frame_id=material.frame_id '
            'WHERE item.frame_id IS NULL'
        ).fetchall()
    }
    stale_frame_ids.update(
        int(row['frame_id'])
        for row in conn.execute(
            'SELECT DISTINCT contribution.frame_id '
            'FROM training_review_material_contributions contribution '
            'LEFT JOIN training_review_items item '
            'ON item.frame_id=contribution.frame_id WHERE item.frame_id IS NULL'
        ).fetchall()
    )
    for frame_id in stale_frame_ids:
        refresh_training_review_material_index(conn, frame_id, commit=False)
    conn.commit()
    indexed = 0
    for offset in range(0, len(frame_ids), batch_size):
        for frame_id in frame_ids[offset : offset + batch_size]:
            indexed += int(
                refresh_training_review_material_index(conn, frame_id, commit=False)
            )
            conn.commit()
        if progress is not None:
            progress(
                {
                    'total': len(frame_ids),
                    'processed': min(len(frame_ids), offset + batch_size),
                    'indexed': indexed,
                }
            )
    previously_grouped = {
        int(row['frame_id'])
        for row in conn.execute(
            'SELECT frame_id FROM training_review_material_index '
            'WHERE result_group_size>1'
        ).fetchall()
    }
    grouped_frame_ids = set()
    for frame_id, group in result_groups.items():
        if int(group['result_group_size']) <= 1:
            continue
        grouped_frame_ids.add(int(frame_id))
    affected_groups = sorted(previously_grouped | grouped_frame_ids)
    if affected_groups:
        for offset in range(0, len(affected_groups), batch_size):
            batch = affected_groups[offset : offset + batch_size]
            conn.executemany(
                'UPDATE training_review_material_index SET '
                'result_group_representative_frame_id=frame_id,result_group_size=1 '
                'WHERE frame_id=?',
                [(frame_id,) for frame_id in batch],
            )
            conn.commit()
        ordered_grouped = sorted(grouped_frame_ids)
        for offset in range(0, len(ordered_grouped), batch_size):
            batch = ordered_grouped[offset : offset + batch_size]
            conn.executemany(
                'UPDATE training_review_material_index SET '
                'result_group_representative_frame_id=?,result_group_size=? '
                'WHERE frame_id=?',
                [
                    (
                        int(
                            result_groups[frame_id][
                                'result_group_representative_frame_id'
                            ]
                        ),
                        int(result_groups[frame_id]['result_group_size']),
                        frame_id,
                    )
                    for frame_id in batch
                ],
            )
            conn.commit()
        for frame_id in affected_groups:
            indexed_row = conn.execute(
                'SELECT * FROM training_review_material_index WHERE frame_id=?',
                (frame_id,),
            ).fetchone()
            if indexed_row is None:
                continue
            _replace_training_review_material_contributions(
                conn,
                frame_id,
                _training_review_material_contributions(conn, indexed_row),
            )
            conn.commit()
    conn.execute('DELETE FROM training_review_material_totals')
    conn.execute(
        'INSERT INTO training_review_material_totals('
        'kind,scene,match_mode,hero_label,source_scope,metric,'
        'frame_count,crop_count,updated_at) '
        'SELECT kind,scene,match_mode,hero_label,source_scope,metric,'
        'SUM(frame_count),SUM(crop_count),? '
        'FROM training_review_material_contributions '
        'GROUP BY kind,scene,match_mode,hero_label,source_scope,metric',
        (now(),),
    )
    conn.commit()
    return {
        'total': len(frame_ids),
        'indexed': indexed,
        'contributions': int(
            conn.execute(
                'SELECT COUNT(*) FROM training_review_material_contributions'
            ).fetchone()[0]
        ),
        'totals': int(
            conn.execute(
                'SELECT COUNT(*) FROM training_review_material_totals'
            ).fetchone()[0]
        ),
        'grouped': len(grouped_frame_ids),
    }


def training_review_material_index_complete(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        'SELECT (SELECT COUNT(*) FROM training_review_items) AS items,'
        '(SELECT COUNT(*) FROM training_review_material_index) AS indexed'
    ).fetchone()
    if row is None:
        return False
    try:
        return int(row['items']) == int(row['indexed'])
    except (KeyError, TypeError, ValueError):
        return False


_TRAINING_REVIEW_PREFILL_STATUSES = {'pending', 'queued', 'running', 'ready', 'failed'}
_TRAINING_REVIEW_PREFILL_STAGES = {'core', 'hero', 'complete'}


def next_training_review_prefill_candidate(
    conn: sqlite3.Connection, *, maximum_attempts: int = 3
) -> Optional[Dict[str, Any]]:
    """返回下一张需要后台预打标的图，并按需补一条历史派生索引。"""
    if maximum_attempts < 1:
        raise ValueError('预打标最大尝试次数必须大于零')
    staged = conn.execute(
        'SELECT inbox.*,frame.video_id FROM training_review_candidate_inbox inbox '
        'JOIN frames frame ON frame.id=inbox.frame_id '
        "WHERE inbox.prefill_status IN ('pending','failed') "
        'AND inbox.prefill_attempts<? '
        "ORDER BY CASE inbox.prefill_status WHEN 'pending' THEN 0 ELSE 1 END,"
        'inbox.source_created_at DESC,inbox.frame_id DESC LIMIT 1',
        (int(maximum_attempts),),
    ).fetchone()
    if staged is not None:
        return {**dict(staged), 'prefill_origin': 'candidate_inbox'}
    candidate_sql = (
        'SELECT material.* FROM training_review_material_index material '
        'JOIN training_review_items item ON item.frame_id=material.frame_id '
        "WHERE material.prefill_status IN ('pending','failed') "
        'AND material.prefill_attempts<? AND ('
        "item.review_status IN ('pending','partial') OR ("
        "item.review_status='confirmed' AND material.is_legacy=1 "
        f'AND NOT ({_UNIFIED_MANUAL_REVIEWED}))) '
        "ORDER BY CASE material.prefill_stage WHEN 'hero' THEN 0 ELSE 1 END,"
        "CASE material.prefill_status WHEN 'pending' THEN 0 ELSE 1 END,"
        'CASE WHEN material.result_group_representative_frame_id='
        'material.frame_id THEN 0 ELSE 1 END,'
        'material.is_new DESC,material.source_created_at DESC,'
        'material.source_offset DESC,material.frame_id DESC LIMIT 1'
    )
    parameters = (int(maximum_attempts),)
    row = conn.execute(candidate_sql, parameters).fetchone()
    if row is None:
        missing = conn.execute(
            'SELECT item.frame_id FROM training_review_items item '
            'LEFT JOIN training_review_material_index material '
            'ON material.frame_id=item.frame_id '
            'WHERE material.frame_id IS NULL '
            "AND item.review_status IN ('pending','partial') "
            'ORDER BY item.frame_id DESC LIMIT 1'
        ).fetchone()
        if missing is not None:
            refresh_training_review_material_index(conn, int(missing['frame_id']))
            row = conn.execute(candidate_sql, parameters).fetchone()
    return None if row is None else {**dict(row), 'prefill_origin': 'review_item'}


def update_training_review_prefill_state(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    status: str,
    stage: str,
    screen_type: str = '',
    team_size: Optional[int] = None,
    error: str = '',
    increment_attempt: bool = False,
    reset_attempts: bool = False,
) -> Dict[str, Any]:
    """更新单帧预打标生命周期，并同步该帧可筛选统计。"""
    if status not in _TRAINING_REVIEW_PREFILL_STATUSES:
        raise ValueError('预打标状态无效')
    if stage not in _TRAINING_REVIEW_PREFILL_STAGES:
        raise ValueError('预打标阶段无效')
    normalized_screen = screen_type.strip()
    if normalized_screen not in {'', *_HERO_SCREEN_TYPES}:
        raise ValueError('预打标英雄画面类型无效')
    if team_size is not None and int(team_size) not in {3, 5}:
        raise ValueError('预打标英雄人数必须是 3 或 5')
    if stage == 'hero' and (
        normalized_screen not in _HERO_SCREEN_TYPES or team_size is None
    ):
        raise ValueError('英雄预打标阶段缺少画面类型或人数')
    timestamp = now()
    cursor = conn.execute(
        'UPDATE training_review_material_index SET prefill_status=?,'
        'prefill_stage=?,prefill_attempts=CASE WHEN ?=1 THEN 0 '
        'ELSE prefill_attempts+? END,'
        'prefill_error=?,prefill_screen_type=?,prefill_team_size=?,'
        'prefill_updated_at=?,prefilled_at=? WHERE frame_id=?',
        (
            status,
            stage,
            int(bool(reset_attempts)),
            int(bool(increment_attempt)),
            error.strip()[:2_000],
            normalized_screen,
            None if team_size is None else int(team_size),
            timestamp,
            timestamp if status == 'ready' else None,
            int(frame_id),
        ),
    )
    if cursor.rowcount == 1:
        refresh_training_review_material_index(conn, int(frame_id), commit=False)
        conn.commit()
        row = conn.execute(
            'SELECT * FROM training_review_material_index WHERE frame_id=?',
            (int(frame_id),),
        ).fetchone()
    else:
        inbox_status = 'promoted' if status == 'ready' else status
        cursor = conn.execute(
            'UPDATE training_review_candidate_inbox SET prefill_status=?,'
            'prefill_stage=?,prefill_attempts=CASE WHEN ?=1 THEN 0 '
            'ELSE prefill_attempts+? END,prefill_error=?,prefill_screen_type=?,'
            'prefill_team_size=?,updated_at=?,promoted_at=CASE WHEN ?='
            "'promoted' THEN ? ELSE promoted_at END WHERE frame_id=?",
            (
                inbox_status,
                stage,
                int(bool(reset_attempts)),
                int(bool(increment_attempt)),
                error.strip()[:2_000],
                normalized_screen,
                None if team_size is None else int(team_size),
                timestamp,
                inbox_status,
                timestamp,
                int(frame_id),
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise KeyError(frame_id)
        conn.commit()
        row = conn.execute(
            'SELECT * FROM training_review_candidate_inbox WHERE frame_id=?',
            (int(frame_id),),
        ).fetchone()
    if row is None:
        raise KeyError(frame_id)
    return dict(row)


def training_review_prefill_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        'SELECT prefill_status,prefill_stage,COUNT(*) AS count '
        'FROM training_review_material_index GROUP BY prefill_status,prefill_stage'
    ).fetchall()
    statuses: Dict[str, int] = {}
    stages: Dict[str, int] = {}
    for row in rows:
        count = int(row['count'])
        status = str(row['prefill_status'])
        stage = str(row['prefill_stage'])
        statuses[status] = statuses.get(status, 0) + count
        stages[stage] = stages.get(stage, 0) + count
    return {
        'total': sum(statuses.values()),
        'statuses': statuses,
        'stages': stages,
        'ready': int(statuses.get('ready', 0)),
        'waiting': sum(
            int(statuses.get(value, 0)) for value in ('pending', 'queued', 'running')
        ),
        'failed': int(statuses.get('failed', 0)),
        'candidate_inbox': training_review_candidate_inbox_stats(conn),
    }


def training_review_queue_summary(
    conn: sqlite3.Connection, *, source_scope: str = 'new'
) -> Dict[str, int]:
    """轻量汇总统一复核队列；不读取来源 JSON 或训练素材分布。"""
    if source_scope != 'new':
        raise ValueError('轻量复核统计目前只支持 Worker 新素材')
    source = conn.execute(
        'SELECT COUNT(DISTINCT source.frame_id) AS total '
        'FROM training_review_sources source '
        "WHERE source.source_type IN ('worker','result_archive',"
        "'manual_correction')"
    ).fetchone()
    material = conn.execute(
        'SELECT '
        "COALESCE(SUM(CASE WHEN material.prefill_status='ready' "
        'THEN 1 ELSE 0 END),0) '
        'AS prefill_ready,'
        "COALESCE(SUM(CASE WHEN material.prefill_status='ready' AND "
        "item.review_status IN ('pending','partial') THEN 1 ELSE 0 END),0) "
        'AS ready_for_review,'
        "COALESCE(SUM(CASE WHEN material.prefill_status='failed' "
        'THEN 1 ELSE 0 END),0) '
        'AS prefill_failed,'
        "COALESCE(SUM(CASE WHEN material.prefill_status IN "
        "('pending','queued','running') AND "
        "item.review_status IN ('pending','partial') THEN 1 ELSE 0 END),0) "
        'AS review_waiting '
        'FROM training_review_material_index material '
        'JOIN training_review_items item ON item.frame_id=material.frame_id '
        'WHERE material.is_new=1'
    ).fetchone()
    inbox = conn.execute(
        'SELECT '
        "COALESCE(SUM(CASE WHEN prefill_status IN "
        "('pending','queued','running') THEN 1 ELSE 0 END),0) AS waiting,"
        "COALESCE(SUM(CASE WHEN prefill_status='failed' "
        'THEN 1 ELSE 0 END),0) AS failed '
        'FROM training_review_candidate_inbox'
    ).fetchone()
    total = int(source['total'] or 0)
    ready_for_review = int(material['ready_for_review'] or 0)
    return {
        'total': total,
        'prefill_ready': int(material['prefill_ready'] or 0),
        'ready_for_review': ready_for_review,
        'prefill_waiting': int(material['review_waiting'] or 0)
        + int(inbox['waiting'] or 0),
        'prefill_failed': int(material['prefill_failed'] or 0)
        + int(inbox['failed'] or 0),
    }


def _training_review_related_hero_counts(
    conn: sqlite3.Connection,
) -> Tuple[Dict[Tuple[str, str, str], Dict[str, int]], Dict[Tuple[str, str], int]]:
    """一次聚合出模型漏认但同局／同视频仍可复核的候选数量。"""
    scenes = tuple(value[0] for value in _MATERIAL_SUGGESTION_SCENES[:3])
    scene_values = ','.join("('{}')".format(value) for value in scenes)
    effective = "COALESCE(NULLIF({slot}.confirmed_label,''),{slot}.suggested_label)"
    direct_missing = (
        'NOT EXISTS (SELECT 1 FROM training_review_hero_slots target_slot '
        'JOIN training_review_hero_lineups target_lineup '
        'ON target_lineup.frame_id=target_slot.frame_id '
        'WHERE target_slot.frame_id=target.frame_id '
        'AND target_lineup.screen_type=target.scene AND '
        + effective.format(slot='target_slot')
        + '=evidence.hero_label)'
    )
    related: Dict[Tuple[str, str, str], Dict[str, int]] = {}

    match_rows = conn.execute(
        'WITH evidence AS ('
        'SELECT DISTINCT material.linked_match_id AS match_id,'
        + effective.format(slot='slot')
        + ' AS hero_label FROM training_review_material_index material '
        'JOIN training_review_hero_slots slot ON slot.frame_id=material.frame_id '
        'JOIN training_review_hero_lineups lineup ON lineup.frame_id=material.frame_id '
        'WHERE material.linked_match_id IS NOT NULL AND '
        'lineup.screen_type=material.scene AND '
        + effective.format(slot='slot')
        + " NOT IN ('','unreadable')) "
        'SELECT evidence.hero_label,target.scene,'
        'COUNT(DISTINCT CASE WHEN target.is_new=1 THEN target.frame_id END) '
        'AS new_count,'
        'COUNT(DISTINCT CASE WHEN target.is_legacy=1 THEN target.frame_id END) '
        'AS legacy_count FROM evidence '
        'JOIN training_review_material_index target '
        'ON target.linked_match_id=evidence.match_id '
        "WHERE target.review_status IN ('pending','partial') "
        "AND target.prefill_status='ready' "
        "AND target.scene IN ('gameplay_hud','scoreboard','result_page') "
        'AND target.result_group_representative_frame_id=target.frame_id AND '
        + direct_missing
        + ' GROUP BY evidence.hero_label,target.scene'
    ).fetchall()
    for row in match_rows:
        for scope in ('new', 'legacy'):
            related.setdefault(
                (str(row['hero_label']), str(row['scene']), scope),
                {'same_match': 0, 'same_video': 0},
            )['same_match'] = int(row[f'{scope}_count'])

    video_rows = conn.execute(
        'WITH linked_videos AS ('
        'SELECT DISTINCT video_id FROM training_review_material_index '
        'WHERE linked_match_id IS NOT NULL),evidence AS ('
        'SELECT DISTINCT material.video_id,'
        + effective.format(slot='slot')
        + ' AS hero_label FROM training_review_material_index material '
        'JOIN training_review_hero_slots slot ON slot.frame_id=material.frame_id '
        'JOIN training_review_hero_lineups lineup ON lineup.frame_id=material.frame_id '
        'LEFT JOIN linked_videos linked ON linked.video_id=material.video_id '
        'WHERE material.linked_match_id IS NULL AND material.session_id>0 '
        'AND material.part_id>0 AND linked.video_id IS NULL AND '
        'lineup.screen_type=material.scene AND '
        + effective.format(slot='slot')
        + " NOT IN ('','unreadable')) "
        'SELECT evidence.hero_label,target.scene,'
        'COUNT(DISTINCT CASE WHEN target.is_new=1 THEN target.frame_id END) '
        'AS new_count,'
        'COUNT(DISTINCT CASE WHEN target.is_legacy=1 THEN target.frame_id END) '
        'AS legacy_count FROM evidence '
        'JOIN training_review_material_index target '
        'ON target.video_id=evidence.video_id '
        "WHERE target.review_status IN ('pending','partial') "
        "AND target.prefill_status='ready' "
        'AND target.linked_match_id IS NULL '
        "AND target.scene IN ('gameplay_hud','scoreboard','result_page') "
        'AND target.result_group_representative_frame_id=target.frame_id AND '
        + direct_missing
        + ' GROUP BY evidence.hero_label,target.scene'
    ).fetchall()
    for row in video_rows:
        for scope in ('new', 'legacy'):
            related.setdefault(
                (str(row['hero_label']), str(row['scene']), scope),
                {'same_match': 0, 'same_video': 0},
            )['same_video'] = int(row[f'{scope}_count'])

    missing_rows = conn.execute(
        'WITH evidence AS ('
        'SELECT DISTINCT material.linked_match_id AS match_id,'
        + effective.format(slot='slot')
        + ' AS hero_label FROM training_review_material_index material '
        'JOIN training_review_hero_slots slot ON slot.frame_id=material.frame_id '
        'JOIN training_review_hero_lineups lineup ON lineup.frame_id=material.frame_id '
        'WHERE material.linked_match_id IS NOT NULL AND '
        'lineup.screen_type=material.scene AND '
        + effective.format(slot='slot')
        + " NOT IN ('','unreadable')),desired(scene) AS (VALUES "
        + scene_values
        + ') SELECT evidence.hero_label,desired.scene,COUNT(*) AS match_count '
        'FROM evidence CROSS JOIN desired WHERE NOT EXISTS ('
        'SELECT 1 FROM training_review_material_index target '
        'WHERE target.linked_match_id=evidence.match_id '
        'AND target.scene=desired.scene) '
        'GROUP BY evidence.hero_label,desired.scene'
    ).fetchall()
    missing = {
        (str(row['hero_label']), str(row['scene'])): int(row['match_count'])
        for row in missing_rows
    }
    return related, missing


def _training_review_scene_prefill_counts(
    conn: sqlite3.Connection,
) -> Dict[Tuple[str, str], Dict[str, int]]:
    """按已建立的轻量索引统计尚不可复核的场景素材。"""
    rows = conn.execute(
        'SELECT scene,match_mode,'
        "SUM(CASE WHEN prefill_status IN ('pending','queued','running') "
        'THEN 1 ELSE 0 END) AS waiting_count,'
        "SUM(CASE WHEN prefill_status='failed' THEN 1 ELSE 0 END) "
        'AS failed_count '
        'FROM training_review_material_index '
        "WHERE review_status IN ('pending','partial') "
        'AND result_group_representative_frame_id=frame_id '
        "AND scene IN ('gameplay_hud','scoreboard','result_page','hero_select') "
        "AND match_mode IN ('3v3','aram','5v5','blitz') "
        'GROUP BY scene,match_mode'
    ).fetchall()
    return {
        (str(row['scene']), str(row['match_mode'])): {
            'waiting': int(row['waiting_count'] or 0),
            'failed': int(row['failed_count'] or 0),
        }
        for row in rows
    }


def training_review_material_suggestions(
    conn: sqlite3.Connection, *, hero_catalog: Sequence[Dict[str, str]] = ()
) -> List[Dict[str, Any]]:
    """只读取增量汇总，生成素材缺口建议。"""
    from . import model_quality

    latest_model_issues = model_quality.latest_issue_rates(conn)
    totals = {
        (
            str(row['kind']),
            str(row['scene']),
            str(row['match_mode']),
            str(row['hero_label']),
            str(row['source_scope']),
            str(row['metric']),
        ): (int(row['frame_count']), int(row['crop_count']))
        for row in conn.execute(
            'SELECT kind,scene,match_mode,hero_label,source_scope,metric,'
            'frame_count,crop_count FROM training_review_material_totals'
        ).fetchall()
    }

    confirmed_scene_rows = conn.execute(
        """
        SELECT scene,match_mode,source_scope,COUNT(*) AS frame_count FROM (
            SELECT
                CASE
                    WHEN item.hero_select_label LIKE 'select_%' THEN 'hero_select'
                    WHEN item.hero_layout_label IN
                         ('gameplay_hud','scoreboard','result_page')
                    THEN item.hero_layout_label
                    WHEN item.result_panel_label='result_panel' THEN 'result_page'
                    ELSE ''
                END AS scene,
                CASE
                    WHEN item.match_mode_label IN ('3v3','aram','5v5','blitz')
                    THEN item.match_mode_label
                    WHEN item.hero_select_label='select_3v3' THEN '3v3'
                    WHEN item.hero_select_label='select_aram' THEN 'aram'
                    WHEN item.hero_select_label='select_5v5' THEN '5v5'
                    WHEN item.hero_select_label='select_blitz' THEN 'blitz'
                    ELSE ''
                END AS match_mode,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM training_review_sources source
                        WHERE source.frame_id=item.frame_id
                          AND (source.source_type='legacy_annotation'
                               OR source.source_type LIKE 'legacy_%')
                    ) THEN 'legacy'
                    WHEN EXISTS (
                        SELECT 1 FROM training_review_sources source
                        WHERE source.frame_id=item.frame_id
                          AND source.source_type IN
                              ('worker','result_archive','manual_correction')
                    ) THEN 'new'
                    ELSE 'other'
                END AS source_scope
            FROM training_review_items item
            WHERE item.review_status='confirmed'
        ) truth
        WHERE scene IN ('gameplay_hud','scoreboard','result_page','hero_select')
          AND match_mode IN ('3v3','aram','5v5','blitz')
        GROUP BY scene,match_mode,source_scope
        """
    ).fetchall()
    confirmed_scenes: Dict[Tuple[str, str], int] = {}
    confirmed_scene_scopes: Dict[Tuple[str, str, str], int] = {}
    for row in confirmed_scene_rows:
        key = (str(row['scene']), str(row['match_mode']))
        scope = str(row['source_scope'])
        count = int(row['frame_count'])
        confirmed_scenes[key] = confirmed_scenes.get(key, 0) + count
        confirmed_scene_scopes[(*key, scope)] = count
    confirmed_hero_rows = conn.execute(
        """
        SELECT lineup.screen_type AS scene,slot.confirmed_label AS hero_label,
               CASE
                   WHEN EXISTS (
                       SELECT 1 FROM training_review_sources source
                       WHERE source.frame_id=lineup.frame_id
                         AND (source.source_type='legacy_annotation'
                              OR source.source_type LIKE 'legacy_%')
                   ) THEN 'legacy'
                   WHEN EXISTS (
                       SELECT 1 FROM training_review_sources source
                       WHERE source.frame_id=lineup.frame_id
                         AND source.source_type IN
                             ('worker','result_archive','manual_correction')
                   ) THEN 'new'
                   ELSE 'other'
               END AS source_scope,
               COUNT(DISTINCT lineup.frame_id) AS frame_count,
               COUNT(*) AS crop_count
        FROM training_review_hero_lineups lineup
        JOIN training_review_hero_slots slot ON slot.frame_id=lineup.frame_id
        WHERE lineup.review_status='confirmed'
          AND lineup.screen_type IN ('gameplay_hud','scoreboard','result_page')
          AND COALESCE(slot.confirmed_label,'') NOT IN ('','unreadable')
        GROUP BY lineup.screen_type,slot.confirmed_label,source_scope
        """
    ).fetchall()
    confirmed_heroes: Dict[Tuple[str, str], Tuple[int, int]] = {}
    confirmed_hero_scopes: Dict[Tuple[str, str, str], Tuple[int, int]] = {}
    for row in confirmed_hero_rows:
        key = (str(row['scene']), str(row['hero_label']))
        scope = str(row['source_scope'])
        frame_count = int(row['frame_count'])
        crop_count = int(row['crop_count'])
        previous_frames, previous_crops = confirmed_heroes.get(key, (0, 0))
        confirmed_heroes[key] = (
            previous_frames + frame_count,
            previous_crops + crop_count,
        )
        confirmed_hero_scopes[(*key, scope)] = (frame_count, crop_count)

    def total(
        kind: str,
        scene: str,
        match_mode: str,
        hero_label: str,
        source_scope: str,
        metric: str,
    ) -> Tuple[int, int]:
        if source_scope == 'all' and metric == 'confirmed':
            if kind == 'scene_mode':
                return confirmed_scenes.get((scene, match_mode), 0), 0
            if kind == 'hero_scene':
                return confirmed_heroes.get((scene, hero_label), (0, 0))
        return totals.get(
            (kind, scene, match_mode, hero_label, source_scope, metric), (0, 0)
        )

    result: List[Dict[str, Any]] = []
    related_heroes, missing_hero_scenes = _training_review_related_hero_counts(conn)
    scene_prefill = _training_review_scene_prefill_counts(conn)
    for scene, scene_label, minimum in _MATERIAL_SUGGESTION_SCENES:
        target = minimum
        for mode, mode_label in _MATERIAL_SUGGESTION_MODES:
            count = total('scene_mode', scene, mode, '', 'all', 'confirmed')[0]
            confirmed_breakdown = {
                scope: confirmed_scene_scopes.get((scene, mode, scope), 0)
                for scope in ('legacy', 'new', 'other')
            }
            new_count = total('scene_mode', scene, mode, '', 'new', 'candidate')[0]
            legacy_count = total('scene_mode', scene, mode, '', 'legacy', 'candidate')[
                0
            ]
            source_scope = 'new' if new_count >= legacy_count else 'legacy'
            available = new_count if source_scope == 'new' else legacy_count
            ratio = count / target if target else 1.0
            sufficient = count >= target
            if scene == 'hero_select':
                quality = latest_model_issues.get(
                    ('hero_select', 'hero_select', f'select_{mode}')
                )
            else:
                quality = latest_model_issues.get(('match_mode', scene, mode))
            model_needs_attention = bool(
                quality
                and int(quality['compared']) >= 20
                and float(quality['correction_rate']) >= 0.1
            )
            prefill = scene_prefill.get((scene, mode), {})
            severity = (
                ('scarce' if float(quality['correction_rate']) >= 0.2 else 'low')
                if sufficient and model_needs_attention and quality
                else (
                    'sufficient'
                    if sufficient
                    else 'urgent' if count == 0 else 'scarce' if ratio < 0.35 else 'low'
                )
            )
            result.append(
                {
                    'kind': 'scene_mode',
                    'scene': scene,
                    'scene_label': scene_label,
                    'match_mode': mode,
                    'mode_label': mode_label,
                    'confirmed_count': count,
                    'legacy_confirmed_count': confirmed_breakdown['legacy'],
                    'new_confirmed_count': confirmed_breakdown['new'],
                    'other_confirmed_count': confirmed_breakdown['other'],
                    'target_count': target,
                    'shortage_count': max(0, target - count),
                    'candidate_count': available,
                    'prefill_waiting_count': int(prefill.get('waiting', 0)),
                    'prefill_failed_count': int(prefill.get('failed', 0)),
                    'source_scope': source_scope,
                    'model_quality': quality,
                    'severity': severity,
                    'status': (
                        'model_errors'
                        if sufficient and model_needs_attention
                        else 'sufficient' if sufficient else 'shortage'
                    ),
                    'filters': {
                        'status': 'needs_review',
                        'scene': scene,
                        'match_mode': mode,
                    },
                }
            )

    afk_row = conn.execute(
        'SELECT '
        'COALESCE(SUM(CASE WHEN slot.is_afk=0 THEN 1 ELSE 0 END),0) '
        'AS active_count,'
        'COALESCE(SUM(CASE WHEN slot.is_afk=1 THEN 1 ELSE 0 END),0) '
        'AS afk_count,'
        'COUNT(DISTINCT CASE WHEN slot.is_afk IS NULL AND material.is_new=1 '
        'THEN lineup.frame_id END) AS new_candidate_count,'
        'COUNT(DISTINCT CASE WHEN slot.is_afk IS NULL AND material.is_legacy=1 '
        'THEN lineup.frame_id END) AS legacy_candidate_count '
        'FROM training_review_hero_lineups lineup '
        'JOIN training_review_hero_slots slot ON slot.frame_id=lineup.frame_id '
        'JOIN training_review_material_index material '
        'ON material.frame_id=lineup.frame_id '
        "WHERE lineup.review_status='confirmed' "
        "AND lineup.screen_type='result_page' "
        'AND material.result_group_representative_frame_id=lineup.frame_id'
    ).fetchone()
    active_count = int(afk_row['active_count'] or 0)
    afk_count = int(afk_row['afk_count'] or 0)
    new_candidates = int(afk_row['new_candidate_count'] or 0)
    legacy_candidates = int(afk_row['legacy_candidate_count'] or 0)
    source_scope = 'new' if new_candidates >= legacy_candidates else 'legacy'
    candidate_count = new_candidates if source_scope == 'new' else legacy_candidates
    active_shortage = max(0, _MATERIAL_AFK_TARGETS['active'] - active_count)
    afk_shortage = max(0, _MATERIAL_AFK_TARGETS['afk'] - afk_count)
    sufficient = active_shortage == 0 and afk_shortage == 0
    ratio = min(
        active_count / _MATERIAL_AFK_TARGETS['active'],
        afk_count / _MATERIAL_AFK_TARGETS['afk'],
    )
    result.append(
        {
            'kind': 'afk_status',
            'scene': 'result_page',
            'scene_label': '真正结算图',
            'confirmed_count': afk_count,
            'active_count': active_count,
            'afk_count': afk_count,
            'active_target_count': _MATERIAL_AFK_TARGETS['active'],
            'afk_target_count': _MATERIAL_AFK_TARGETS['afk'],
            'active_shortage_count': active_shortage,
            'afk_shortage_count': afk_shortage,
            'target_count': _MATERIAL_AFK_TARGETS['afk'],
            'shortage_count': active_shortage + afk_shortage,
            'candidate_count': candidate_count,
            'source_scope': source_scope,
            'severity': (
                'sufficient'
                if sufficient
                else 'urgent' if afk_count == 0 else 'scarce' if ratio < 0.35 else 'low'
            ),
            'status': 'sufficient' if sufficient else 'shortage',
            'filters': {'status': 'missing_afk', 'scene': 'result_page'},
        }
    )

    catalog_by_label = {
        str(hero.get('label') or ''): str(hero.get('name') or hero.get('label') or '')
        for hero in hero_catalog
        if str(hero.get('label') or '')
    }
    for key in totals:
        if key[0] == 'hero_scene' and key[3]:
            catalog_by_label.setdefault(key[3], key[3])
    for _scene, hero_label in confirmed_heroes:
        catalog_by_label.setdefault(hero_label, hero_label)
    for hero_label, hero_name in sorted(
        catalog_by_label.items(), key=lambda item: (item[1], item[0])
    ):
        for scene, scene_label, _minimum in _MATERIAL_SUGGESTION_SCENES[:3]:
            count = total('hero_scene', scene, '', hero_label, 'all', 'confirmed')[1]
            confirmed_breakdown = {
                scope: confirmed_hero_scopes.get((scene, hero_label, scope), (0, 0))[1]
                for scope in ('legacy', 'new', 'other')
            }
            new_frames, new_crops = total(
                'hero_scene', scene, '', hero_label, 'new', 'candidate'
            )
            legacy_frames, legacy_crops = total(
                'hero_scene', scene, '', hero_label, 'legacy', 'candidate'
            )
            new_related = related_heroes.get(
                (hero_label, scene, 'new'), {'same_match': 0, 'same_video': 0}
            )
            legacy_related = related_heroes.get(
                (hero_label, scene, 'legacy'), {'same_match': 0, 'same_video': 0}
            )
            new_related_count = int(new_related['same_match']) + int(
                new_related['same_video']
            )
            legacy_related_count = int(legacy_related['same_match']) + int(
                legacy_related['same_video']
            )
            source_scope = (
                'new'
                if (new_frames, new_related_count)
                >= (legacy_frames, legacy_related_count)
                else 'legacy'
            )
            if source_scope == 'new':
                candidate_count = new_frames
                candidate_crop_count = new_crops
                related_counts = new_related
                model_prefill_count = new_frames
                related_candidate_count = new_related_count
            else:
                candidate_count = legacy_frames
                candidate_crop_count = legacy_crops
                related_counts = legacy_related
                model_prefill_count = legacy_frames
                related_candidate_count = legacy_related_count
            target = _MATERIAL_HERO_SCENE_TARGET
            sufficient = count >= target
            ratio = count / target
            quality = latest_model_issues.get(('hero_identity', scene, hero_label))
            model_needs_attention = bool(
                quality
                and int(quality['compared']) >= 10
                and float(quality['correction_rate']) >= 0.15
            )
            result.append(
                {
                    'kind': 'hero_scene',
                    'scene': scene,
                    'scene_label': scene_label,
                    'hero_label': hero_label,
                    'hero_name': hero_name,
                    'confirmed_count': count,
                    'legacy_confirmed_count': confirmed_breakdown['legacy'],
                    'new_confirmed_count': confirmed_breakdown['new'],
                    'other_confirmed_count': confirmed_breakdown['other'],
                    'target_count': target,
                    'shortage_count': max(0, target - count),
                    'candidate_count': candidate_count,
                    'related_candidate_count': related_candidate_count,
                    'candidate_crop_count': candidate_crop_count,
                    'model_prefill_count': model_prefill_count,
                    'model_prefill_crop_count': candidate_crop_count,
                    'same_match_candidate_count': int(related_counts['same_match']),
                    'same_video_candidate_count': int(related_counts['same_video']),
                    'matches_without_scene_candidate': missing_hero_scenes.get(
                        (hero_label, scene), 0
                    ),
                    'source_scope': source_scope,
                    'model_quality': quality,
                    'severity': (
                        (
                            'scarce'
                            if float(quality['correction_rate']) >= 0.25
                            else 'low'
                        )
                        if sufficient and model_needs_attention and quality
                        else (
                            'sufficient'
                            if sufficient
                            else (
                                'urgent'
                                if count == 0
                                else 'scarce' if ratio < 0.35 else 'low'
                            )
                        )
                    ),
                    'status': (
                        'model_errors'
                        if sufficient and model_needs_attention
                        else 'sufficient' if sufficient else 'shortage'
                    ),
                    'filters': {
                        'status': 'needs_review',
                        'scene': scene,
                        'hero': hero_label,
                    },
                }
            )
    severity_rank = {'urgent': 0, 'scarce': 1, 'low': 2, 'sufficient': 3}
    return sorted(
        result,
        key=lambda value: (
            severity_rank[value['severity']],
            -int(value['candidate_count'] > 0),
            -int(value['shortage_count']),
            0 if value['kind'] == 'scene_mode' else 1,
            str(value['scene']),
            str(value.get('match_mode') or value.get('hero_name') or ''),
        ),
    )


def _training_review_material_suggestions(
    visible_rows: Sequence[sqlite3.Row],
    source_rows: Sequence[sqlite3.Row],
    categories_by_frame: Dict[int, set[str]],
    hero_rows: Sequence[sqlite3.Row] = (),
    hero_catalog: Sequence[Dict[str, str]] = (),
) -> List[Dict[str, Any]]:
    signals = _training_review_material_signals(source_rows)
    confirmed = {
        (scene, mode): 0
        for scene, _scene_label, _minimum in _MATERIAL_SUGGESTION_SCENES
        for mode, _mode_label in _MATERIAL_SUGGESTION_MODES
    }
    confirmed_scopes = {
        scope: {key: 0 for key in confirmed} for scope in ('legacy', 'new', 'other')
    }
    candidates = {scope: {key: 0 for key in confirmed} for scope in ('new', 'legacy')}
    for row in visible_rows:
        frame_id = int(row['frame_id'])
        signal = signals.get(frame_id, {})
        scene = _training_review_material_scene(row, signal)
        mode = _training_review_material_mode(row, signal)
        key = (scene, mode)
        if key not in confirmed:
            continue
        status = str(row['review_status'])
        if status == 'confirmed':
            confirmed[key] += 1
            categories = categories_by_frame.get(frame_id, set())
            scope = (
                'legacy'
                if 'legacy' in categories
                else (
                    'new'
                    if categories & {'worker', 'result_archive', 'manual_correction'}
                    else 'other'
                )
            )
            confirmed_scopes[scope][key] += 1
            continue
        if status not in {'pending', 'partial'}:
            continue
        categories = categories_by_frame.get(frame_id, set())
        if categories & {'worker', 'result_archive', 'manual_correction'}:
            candidates['new'][key] += 1
        if 'legacy' in categories:
            candidates['legacy'][key] += 1

    result = []
    for scene, scene_label, minimum in _MATERIAL_SUGGESTION_SCENES:
        target = minimum
        for mode, mode_label in _MATERIAL_SUGGESTION_MODES:
            count = confirmed[(scene, mode)]
            key = (scene, mode)
            new_count = candidates['new'][key]
            legacy_count = candidates['legacy'][key]
            source_scope = 'new' if new_count >= legacy_count else 'legacy'
            available = new_count if source_scope == 'new' else legacy_count
            ratio = count / target if target else 1.0
            sufficient = count >= target
            severity = (
                'sufficient'
                if sufficient
                else 'urgent' if count == 0 else 'scarce' if ratio < 0.35 else 'low'
            )
            result.append(
                {
                    'kind': 'scene_mode',
                    'scene': scene,
                    'scene_label': scene_label,
                    'match_mode': mode,
                    'mode_label': mode_label,
                    'confirmed_count': count,
                    'legacy_confirmed_count': confirmed_scopes['legacy'][key],
                    'new_confirmed_count': confirmed_scopes['new'][key],
                    'other_confirmed_count': confirmed_scopes['other'][key],
                    'target_count': target,
                    'shortage_count': max(0, target - count),
                    'candidate_count': available,
                    'source_scope': source_scope,
                    'severity': severity,
                    'status': 'sufficient' if sufficient else 'shortage',
                    'filters': {
                        'status': 'needs_review',
                        'scene': scene,
                        'match_mode': mode,
                    },
                }
            )

    catalog_by_label = {
        str(hero.get('label') or ''): str(hero.get('name') or hero.get('label') or '')
        for hero in hero_catalog
        if str(hero.get('label') or '')
    }
    confirmed_heroes: Dict[Tuple[str, str], int] = {}
    confirmed_hero_scopes: Dict[str, Dict[Tuple[str, str], int]] = {
        scope: {} for scope in ('legacy', 'new', 'other')
    }
    candidate_heroes: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {
        'new': {},
        'legacy': {},
    }
    visible_ids = {int(row['frame_id']) for row in visible_rows}
    for row in hero_rows:
        frame_id = int(row['frame_id'])
        if frame_id not in visible_ids:
            continue
        screen_type = str(row['screen_type'] or '')
        if screen_type not in _HERO_SCREEN_TYPES:
            continue
        lineup_status = str(row['lineup_review_status'] or '')
        confirmed_label = str(row['confirmed_label'] or '')
        if lineup_status == 'confirmed' and confirmed_label not in {'', 'unreadable'}:
            key = (confirmed_label, screen_type)
            confirmed_heroes[key] = confirmed_heroes.get(key, 0) + 1
            categories = categories_by_frame.get(frame_id, set())
            scope = (
                'legacy'
                if 'legacy' in categories
                else (
                    'new'
                    if categories & {'worker', 'result_archive', 'manual_correction'}
                    else 'other'
                )
            )
            scoped = confirmed_hero_scopes[scope]
            scoped[key] = scoped.get(key, 0) + 1
            catalog_by_label.setdefault(confirmed_label, confirmed_label)
            continue
        if str(row['item_review_status'] or '') not in {'pending', 'partial'}:
            continue
        suggested_label = str(row['suggested_label'] or '')
        if suggested_label in {'', 'unreadable'}:
            continue
        catalog_by_label.setdefault(suggested_label, suggested_label)
        categories = categories_by_frame.get(frame_id, set())
        scopes = []
        if categories & {'worker', 'result_archive', 'manual_correction'}:
            scopes.append('new')
        if 'legacy' in categories:
            scopes.append('legacy')
        for scope in scopes:
            key = (suggested_label, screen_type)
            value = candidate_heroes[scope].setdefault(
                key, {'frame_ids': set(), 'crop_count': 0}
            )
            value['frame_ids'].add(frame_id)
            value['crop_count'] += 1

    for hero_label, hero_name in sorted(
        catalog_by_label.items(), key=lambda item: (item[1], item[0])
    ):
        for scene, scene_label, _minimum in _MATERIAL_SUGGESTION_SCENES[:3]:
            key = (hero_label, scene)
            count = confirmed_heroes.get(key, 0)
            new_value = candidate_heroes['new'].get(
                key, {'frame_ids': set(), 'crop_count': 0}
            )
            legacy_value = candidate_heroes['legacy'].get(
                key, {'frame_ids': set(), 'crop_count': 0}
            )
            source_scope = (
                'new'
                if len(new_value['frame_ids']) >= len(legacy_value['frame_ids'])
                else 'legacy'
            )
            selected = new_value if source_scope == 'new' else legacy_value
            target = _MATERIAL_HERO_SCENE_TARGET
            sufficient = count >= target
            ratio = count / target
            result.append(
                {
                    'kind': 'hero_scene',
                    'scene': scene,
                    'scene_label': scene_label,
                    'hero_label': hero_label,
                    'hero_name': hero_name,
                    'confirmed_count': count,
                    'legacy_confirmed_count': confirmed_hero_scopes['legacy'].get(
                        key, 0
                    ),
                    'new_confirmed_count': confirmed_hero_scopes['new'].get(key, 0),
                    'other_confirmed_count': confirmed_hero_scopes['other'].get(key, 0),
                    'target_count': target,
                    'shortage_count': max(0, target - count),
                    'candidate_count': len(selected['frame_ids']),
                    'candidate_crop_count': int(selected['crop_count']),
                    'model_prefill_count': len(selected['frame_ids']),
                    'model_prefill_crop_count': int(selected['crop_count']),
                    'related_candidate_count': 0,
                    'same_match_candidate_count': 0,
                    'same_video_candidate_count': 0,
                    'matches_without_scene_candidate': 0,
                    'source_scope': source_scope,
                    'severity': (
                        'sufficient'
                        if sufficient
                        else (
                            'urgent'
                            if count == 0
                            else 'scarce' if ratio < 0.35 else 'low'
                        )
                    ),
                    'status': 'sufficient' if sufficient else 'shortage',
                    'filters': {
                        'status': 'needs_review',
                        'scene': scene,
                        'hero': hero_label,
                    },
                }
            )
    severity_rank = {'urgent': 0, 'scarce': 1, 'low': 2, 'sufficient': 3}
    return sorted(
        result,
        key=lambda value: (
            severity_rank[value['severity']],
            -int(value['candidate_count'] > 0),
            -int(value['shortage_count']),
            0 if value['kind'] == 'scene_mode' else 1,
            str(value['scene']),
            str(value.get('match_mode') or value.get('hero_name') or ''),
        ),
    )


def _training_review_origin_frame_ids(
    conn: sqlite3.Connection, source_scope: str
) -> Optional[set[int]]:
    if source_scope not in _TRAINING_REVIEW_SOURCE_SCOPES:
        raise ValueError('训练复核数据来源无效')
    if source_scope == 'all':
        return None
    if training_review_material_index_complete(conn):
        column = 'is_legacy' if source_scope == 'legacy' else 'is_new'
        rows = conn.execute(
            'SELECT frame_id FROM training_review_material_index ' f'WHERE {column}=1'
        ).fetchall()
    elif source_scope == 'legacy':
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


def training_review_filter_options(
    conn: sqlite3.Connection, *, source_scope: str = 'all'
) -> Dict[str, Any]:
    if source_scope not in _TRAINING_REVIEW_SOURCE_SCOPES:
        raise ValueError('训练复核数据来源无效')
    source_condition = ''
    if source_scope == 'legacy':
        source_condition = (
            'AND EXISTS (SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id = item.frame_id '
            "AND source.source_type LIKE 'legacy_%')"
        )
    elif source_scope == 'new':
        source_condition = (
            'AND EXISTS (SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id = item.frame_id '
            "AND source.source_type IN ('worker', 'result_archive', "
            "'manual_correction'))"
        )
    rows = conn.execute(
        'SELECT video.streamer, COUNT(DISTINCT item.frame_id) AS frame_count '
        'FROM training_review_items item '
        'JOIN frames frame ON frame.id = item.frame_id '
        'JOIN videos video ON video.id = frame.video_id '
        "WHERE TRIM(video.streamer) != '' "
        + source_condition
        + ' GROUP BY video.streamer '
        'ORDER BY frame_count DESC, video.streamer'
    ).fetchall()
    return {
        'streamers': [
            {'name': str(row['streamer']), 'frame_count': int(row['frame_count'])}
            for row in rows
        ]
    }


def training_review_duplicate_result_frame_ids(
    conn: sqlite3.Connection,
    *,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
) -> set[int]:
    groups = (
        training_review_result_groups(conn) if result_groups is None else result_groups
    )
    return {
        frame_id
        for frame_id, group in groups.items()
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
               COALESCE(material.prefill_status, 'pending') AS prefill_status,
               COALESCE(material.prefill_stage, 'core') AS prefill_stage,
               COALESCE(material.prefill_attempts, 0) AS prefill_attempts,
               COALESCE(material.prefill_error, '') AS prefill_error,
               CASE WHEN ({_MISSING_PLAYER_HERO_REVIEW})
                    THEN 1 ELSE 0 END AS needs_player_hero_review,
               CASE WHEN ({_MISSING_AFK_REVIEW})
                    THEN 1 ELSE 0 END AS needs_afk_review,
               CASE WHEN ({_UNIFIED_MANUAL_REVIEWED})
                    THEN 1 ELSE 0 END AS unified_manual_reviewed
        FROM training_review_items item
        JOIN frames frame ON frame.id = item.frame_id
        JOIN videos video ON video.id = frame.video_id
        LEFT JOIN training_review_material_index material
          ON material.frame_id=item.frame_id
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


def get_training_review_items(
    conn: sqlite3.Connection,
    frame_ids: Sequence[int],
    *,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
    pending_review_queue: bool = False,
) -> List[Dict[str, Any]]:
    ordered_ids = list(dict.fromkeys(int(frame_id) for frame_id in frame_ids))
    if not ordered_ids:
        return []
    rows_by_id: Dict[int, sqlite3.Row] = {}
    sources_by_id: Dict[int, List[sqlite3.Row]] = {}
    boxes_by_id: Dict[int, Dict[str, Dict[str, float]]] = {}
    for start in range(0, len(ordered_ids), 400):
        batch = ordered_ids[start : start + 400]
        placeholders = ', '.join('?' for _frame_id in batch)
        review_columns = (
            '0 AS needs_player_hero_review, 0 AS needs_afk_review, '
            '0 AS unified_manual_reviewed'
            if pending_review_queue
            else (
                f'CASE WHEN ({_MISSING_PLAYER_HERO_REVIEW}) '
                'THEN 1 ELSE 0 END AS needs_player_hero_review, '
                f'CASE WHEN ({_MISSING_AFK_REVIEW}) '
                'THEN 1 ELSE 0 END AS needs_afk_review, '
                f'CASE WHEN ({_UNIFIED_MANUAL_REVIEWED}) '
                'THEN 1 ELSE 0 END AS unified_manual_reviewed'
            )
        )
        rows = conn.execute(
            f"""
            SELECT item.*, frame.video_id, frame.timestamp_ms, frame.width,
                   frame.height, frame.frame_path, frame.thumb_path, frame.sha256,
                   video.streamer, video.filename, video.remote_path,
                   COALESCE(material.prefill_status, 'pending') AS prefill_status,
                   COALESCE(material.prefill_stage, 'core') AS prefill_stage,
                   COALESCE(material.prefill_attempts, 0) AS prefill_attempts,
                   COALESCE(material.prefill_error, '') AS prefill_error,
                   {review_columns}
            FROM training_review_items item
            JOIN frames frame ON frame.id = item.frame_id
            JOIN videos video ON video.id = frame.video_id
            LEFT JOIN training_review_material_index material
              ON material.frame_id=item.frame_id
            WHERE item.frame_id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        rows_by_id.update((int(row['frame_id']), row) for row in rows)
        source_rows = conn.execute(
            'SELECT frame_id, id, source_type, source_id, image_path, '
            'suggestions_json, metadata_json, source_created_at, sync_state, '
            'remote_reviewed_at FROM training_review_sources '
            f'WHERE frame_id IN ({placeholders}) '
            'ORDER BY frame_id, source_created_at DESC, id DESC',
            batch,
        ).fetchall()
        for source in source_rows:
            sources_by_id.setdefault(int(source['frame_id']), []).append(source)
        box_rows = conn.execute(
            'SELECT frame_id, box_type, x, y, w, h FROM boxes '
            f'WHERE frame_id IN ({placeholders})',
            batch,
        ).fetchall()
        for box in box_rows:
            boxes_by_id.setdefault(int(box['frame_id']), {})[str(box['box_type'])] = {
                'box_type': str(box['box_type']),
                'x': float(box['x']),
                'y': float(box['y']),
                'w': float(box['w']),
                'h': float(box['h']),
            }
    groups = (
        training_review_result_groups(conn) if result_groups is None else result_groups
    )
    result: List[Dict[str, Any]] = []
    for frame_id in ordered_ids:
        row = rows_by_id.get(frame_id)
        if row is None:
            continue
        item = _training_review_item_dict(
            conn,
            row,
            source_rows=sources_by_id.get(frame_id, ()),
            boxes=boxes_by_id.get(frame_id, {}),
        )
        item.update(
            groups.get(
                frame_id,
                {
                    'result_group_size': 1,
                    'result_group_representative_frame_id': frame_id,
                },
            )
        )
        result.append(item)
    return result


_LEGACY_HERO_SCREEN_TYPES = {
    'gameplay': 'gameplay_hud',
    'scoreboard': 'scoreboard',
    'result_page': 'result_page',
}


def _legacy_hero_review_groups(
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    screen_type: str = '',
    prefill_ready_only: bool = False,
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
               COALESCE(material.prefill_status, 'pending') AS prefill_status,
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
        LEFT JOIN training_review_material_index material
          ON material.frame_id = annotation.frame_id
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
        eligible_members = (
            [row for row in members if str(row['prefill_status']) == 'ready']
            if prefill_ready_only
            else members
        )
        if not eligible_members:
            continue

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

        representative = max(eligible_members, key=rank)
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
    prefill_ready_only: bool = False,
) -> List[Dict[str, Any]]:
    if limit < 1 or limit > 10_000 or offset < 0:
        raise ValueError('训练复核分页参数无效')
    groups = [
        group
        for group in _legacy_hero_review_groups(
            conn,
            streamer=streamer,
            screen_type=screen_type,
            prefill_ready_only=prefill_ready_only,
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
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    screen_type: str = '',
    prefill_ready_only: bool = False,
) -> Dict[str, Any]:
    groups = _legacy_hero_review_groups(
        conn,
        streamer=streamer,
        screen_type=screen_type,
        prefill_ready_only=prefill_ready_only,
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


def _normalized_training_review_heroes(hero: Sequence[str] | str) -> List[str]:
    heroes = (
        [hero.strip()]
        if isinstance(hero, str) and hero.strip()
        else [str(value).strip() for value in hero if str(value).strip()]
    )
    heroes = list(dict.fromkeys(heroes))
    if len(heroes) > 100:
        raise ValueError('英雄筛选数量过多')
    return heroes


def _training_review_related_hero_condition(
    heroes: Sequence[str],
    *,
    material_alias: str = 'material',
    scope: str = 'all',
    require_matching_scene: bool = False,
) -> Tuple[str, List[Any]]:
    if scope not in {'all', 'direct'}:
        raise ValueError('英雄证据范围无效')
    placeholders = ','.join('?' for _hero in heroes)
    effective_label = (
        "COALESCE(NULLIF({slot}.confirmed_label,''),{slot}.suggested_label)"
    )
    direct_scene = (
        f'direct_lineup.screen_type={material_alias}.scene AND '
        if require_matching_scene
        else ''
    )
    match_scene = (
        'match_lineup.screen_type=match_peer.scene AND '
        if require_matching_scene
        else ''
    )
    video_scene = (
        'video_lineup.screen_type=video_peer.scene AND '
        if require_matching_scene
        else ''
    )
    direct = (
        'EXISTS (SELECT 1 FROM training_review_hero_slots direct_slot '
        'JOIN training_review_hero_lineups direct_lineup '
        'ON direct_lineup.frame_id=direct_slot.frame_id '
        'WHERE direct_slot.frame_id=item.frame_id AND '
        + direct_scene
        + effective_label.format(slot='direct_slot')
        + f' IN ({placeholders}))'
    )
    if scope == 'direct':
        return direct, list(heroes)
    condition = (
        f'({direct} OR (' + f'{material_alias}.linked_match_id IS NOT NULL AND EXISTS ('
        'SELECT 1 FROM training_review_material_index match_peer '
        'JOIN training_review_hero_slots match_slot '
        'ON match_slot.frame_id=match_peer.frame_id '
        'JOIN training_review_hero_lineups match_lineup '
        'ON match_lineup.frame_id=match_peer.frame_id '
        + f'WHERE match_peer.linked_match_id={material_alias}.linked_match_id AND '
        + match_scene
        + effective_label.format(slot='match_slot')
        + f' IN ({placeholders}))) OR ('
        + f'{material_alias}.linked_match_id IS NULL '
        + f'AND {material_alias}.session_id>0 AND {material_alias}.part_id>0 '
        'AND NOT EXISTS ('
        'SELECT 1 FROM training_review_material_index linked_video '
        + f'WHERE linked_video.video_id={material_alias}.video_id '
        'AND linked_video.linked_match_id IS NOT NULL) AND EXISTS ('
        'SELECT 1 FROM training_review_material_index video_peer '
        'JOIN training_review_hero_slots video_slot '
        'ON video_slot.frame_id=video_peer.frame_id '
        'JOIN training_review_hero_lineups video_lineup '
        'ON video_lineup.frame_id=video_peer.frame_id '
        + f'WHERE video_peer.video_id={material_alias}.video_id AND '
        + video_scene
        + effective_label.format(slot='video_slot')
        + f' IN ({placeholders}))))'
    )
    return condition, [*heroes, *heroes, *heroes]


def _training_review_related_hero_order(
    heroes: Sequence[str], *, material_alias: str = 'material'
) -> Tuple[str, List[Any]]:
    placeholders = ','.join('?' for _hero in heroes)
    effective = "COALESCE(NULLIF(direct_slot.confirmed_label,''),"
    effective += 'direct_slot.suggested_label)'
    has_direct = (
        'EXISTS (SELECT 1 FROM training_review_hero_slots direct_slot '
        'WHERE direct_slot.frame_id=item.frame_id AND '
        + effective
        + f' IN ({placeholders}))'
    )
    has_confirmed = (
        'EXISTS (SELECT 1 FROM training_review_hero_slots confirmed_slot '
        'WHERE confirmed_slot.frame_id=item.frame_id '
        "AND NULLIF(confirmed_slot.confirmed_label,'') "
        f'IN ({placeholders}))'
    )
    return (
        'CASE '
        f'WHEN NOT ({has_direct}) AND {material_alias}.linked_match_id '
        'IS NOT NULL THEN 0 '
        f'WHEN NOT ({has_direct}) THEN 1 '
        f'WHEN {has_confirmed} THEN 3 ELSE 2 END,',
        [*heroes, *heroes, *heroes],
    )


def training_review_hero_filter_matches(
    conn: sqlite3.Connection, frame_ids: Sequence[int], hero: Sequence[str] | str
) -> Dict[int, List[Dict[str, Any]]]:
    heroes = _normalized_training_review_heroes(hero)
    ordered_ids = list(dict.fromkeys(int(value) for value in frame_ids))
    if not heroes or not ordered_ids:
        return {}
    hero_set = set(heroes)
    direct: Dict[int, Dict[str, str]] = {}
    materials: Dict[int, Dict[str, Optional[int]]] = {}
    for offset in range(0, len(ordered_ids), 350):
        batch = ordered_ids[offset : offset + 350]
        frame_placeholders = ','.join('?' for _frame_id in batch)
        hero_placeholders = ','.join('?' for _hero in heroes)
        rows = conn.execute(
            'SELECT frame_id,confirmed_label,suggested_label '
            'FROM training_review_hero_slots '
            f'WHERE frame_id IN ({frame_placeholders}) AND '
            "COALESCE(NULLIF(confirmed_label,''),suggested_label) "
            f'IN ({hero_placeholders})',
            (*batch, *heroes),
        ).fetchall()
        for row in rows:
            frame_id = int(row['frame_id'])
            confirmed = str(row['confirmed_label'] or '')
            label = confirmed or str(row['suggested_label'] or '')
            if label in hero_set:
                direct.setdefault(frame_id, {})[label] = (
                    'direct_confirmed' if confirmed else 'direct_suggested'
                )
        rows = conn.execute(
            'SELECT frame_id,video_id,session_id,part_id,linked_match_id '
            'FROM training_review_material_index '
            f'WHERE frame_id IN ({frame_placeholders})',
            batch,
        ).fetchall()
        for row in rows:
            materials[int(row['frame_id'])] = {
                'video_id': int(row['video_id']),
                'session_id': int(row['session_id']),
                'part_id': int(row['part_id']),
                'match_id': (
                    None
                    if row['linked_match_id'] is None
                    else int(row['linked_match_id'])
                ),
            }

    match_ids = sorted(
        {
            int(value['match_id'])
            for value in materials.values()
            if value['match_id'] is not None
        }
    )
    match_evidence: Dict[int, Dict[str, str]] = {}
    for offset in range(0, len(match_ids), 350):
        batch = match_ids[offset : offset + 350]
        match_placeholders = ','.join('?' for _match_id in batch)
        hero_placeholders = ','.join('?' for _hero in heroes)
        rows = conn.execute(
            'SELECT material.linked_match_id,slot.confirmed_label,'
            'slot.suggested_label FROM training_review_material_index material '
            'JOIN training_review_hero_slots slot ON slot.frame_id=material.frame_id '
            f'WHERE material.linked_match_id IN ({match_placeholders}) AND '
            "COALESCE(NULLIF(slot.confirmed_label,''),slot.suggested_label) "
            f'IN ({hero_placeholders})',
            (*batch, *heroes),
        ).fetchall()
        for row in rows:
            confirmed = str(row['confirmed_label'] or '')
            label = confirmed or str(row['suggested_label'] or '')
            evidence = match_evidence.setdefault(int(row['linked_match_id']), {})
            if label in hero_set and (confirmed or label not in evidence):
                evidence[label] = 'human' if confirmed else 'model'

    video_ids = sorted({int(value['video_id']) for value in materials.values()})
    videos_with_matches: set[int] = set()
    video_evidence: Dict[int, Dict[str, str]] = {}
    for offset in range(0, len(video_ids), 350):
        batch = video_ids[offset : offset + 350]
        video_placeholders = ','.join('?' for _video_id in batch)
        videos_with_matches.update(
            int(row['video_id'])
            for row in conn.execute(
                'SELECT DISTINCT video_id FROM training_review_material_index '
                f'WHERE video_id IN ({video_placeholders}) '
                'AND linked_match_id IS NOT NULL',
                batch,
            ).fetchall()
        )
        eligible = [value for value in batch if value not in videos_with_matches]
        if not eligible:
            continue
        eligible_placeholders = ','.join('?' for _video_id in eligible)
        hero_placeholders = ','.join('?' for _hero in heroes)
        rows = conn.execute(
            'SELECT material.video_id,slot.confirmed_label,slot.suggested_label '
            'FROM training_review_material_index material '
            'JOIN training_review_hero_slots slot ON slot.frame_id=material.frame_id '
            f'WHERE material.video_id IN ({eligible_placeholders}) AND '
            "COALESCE(NULLIF(slot.confirmed_label,''),slot.suggested_label) "
            f'IN ({hero_placeholders})',
            (*eligible, *heroes),
        ).fetchall()
        for row in rows:
            confirmed = str(row['confirmed_label'] or '')
            label = confirmed or str(row['suggested_label'] or '')
            evidence = video_evidence.setdefault(int(row['video_id']), {})
            if label in hero_set and (confirmed or label not in evidence):
                evidence[label] = 'human' if confirmed else 'model'

    result: Dict[int, List[Dict[str, Any]]] = {}
    for frame_id in ordered_ids:
        material = materials.get(frame_id)
        if material is None:
            continue
        matches = []
        for hero_label in heroes:
            reason = direct.get(frame_id, {}).get(hero_label)
            evidence_source = (
                'human'
                if reason == 'direct_confirmed'
                else 'model' if reason == 'direct_suggested' else None
            )
            match_id = material['match_id']
            if reason is None and match_id is not None:
                evidence_source = match_evidence.get(int(match_id), {}).get(hero_label)
                if evidence_source is not None:
                    reason = 'same_match'
            if (
                reason is None
                and match_id is None
                and int(material['session_id']) > 0
                and int(material['part_id']) > 0
                and int(material['video_id']) not in videos_with_matches
                and hero_label in video_evidence.get(int(material['video_id']), {})
            ):
                reason = 'same_video'
                evidence_source = video_evidence[int(material['video_id'])][hero_label]
            if reason is not None:
                matches.append(
                    {
                        'hero_label': hero_label,
                        'reason': reason,
                        'evidence_source': evidence_source,
                        'match_id': match_id,
                    }
                )
        if matches:
            result[frame_id] = matches
    return result


def _training_review_indexed_attribute_frame_ids(
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
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
        if match_mode not in _TRAINING_REVIEW_MODE_FILTERS:
            raise ValueError('对局模式筛选无效')
        if match_mode == 'unreadable':
            conditions.append('item.match_mode_label = ?')
            parameters.append(match_mode)
        elif match_mode == 'blitz':
            conditions.append('(item.match_mode_label = ? OR material.match_mode = ?)')
            parameters.extend((match_mode, match_mode))
        else:
            conditions.append('material.match_mode = ?')
            parameters.append(match_mode)
    if match_kind:
        if match_kind not in _TRAINING_REVIEW_KIND_FILTERS:
            raise ValueError('对局性质筛选无效')
        conditions.append('item.match_kind_label = ?')
        parameters.append(match_kind)
    if view_context:
        if view_context not in _TRAINING_REVIEW_VIEW_FILTERS:
            raise ValueError('观看方式筛选无效')
        conditions.append('item.view_context_label = ?')
        parameters.append(view_context)
    if scene:
        if scene not in _HERO_SCREEN_TYPES | {'hero_select', 'other'}:
            raise ValueError('画面类型筛选无效')
        conditions.append('material.scene = ?')
        parameters.append(scene)
    heroes = _normalized_training_review_heroes(hero)
    if heroes:
        condition, hero_parameters = _training_review_related_hero_condition(
            heroes, scope=hero_scope, require_matching_scene=bool(scene)
        )
        conditions.append(condition)
        parameters.extend(hero_parameters)
    if confidence:
        confidence_column = {
            'low': 'has_low_confidence',
            'boundary': 'has_boundary_confidence',
            'high': 'has_high_confidence',
        }[confidence]
        conditions.append(f'material.{confidence_column} = 1')
    if not conditions:
        return None
    rows = conn.execute(
        'SELECT DISTINCT item.frame_id FROM training_review_items item '
        'JOIN training_review_material_index material '
        'ON material.frame_id=item.frame_id '
        'JOIN frames frame ON frame.id = item.frame_id '
        'JOIN videos video ON video.id = frame.video_id WHERE '
        + ' AND '.join(conditions),
        parameters,
    ).fetchall()
    return {int(row['frame_id']) for row in rows}


def _training_review_unindexed_attribute_frame_ids(
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
) -> Optional[set[int]]:
    """历史索引未完成时保留原始 JSON 语义；回填完成后不再调用。"""
    if confidence not in {'', 'low', 'boundary', 'high'}:
        raise ValueError('模型置信度筛选无效')
    if match_mode and match_mode not in _TRAINING_REVIEW_MODE_FILTERS:
        raise ValueError('对局模式筛选无效')
    if match_kind and match_kind not in _TRAINING_REVIEW_KIND_FILTERS:
        raise ValueError('对局性质筛选无效')
    if view_context and view_context not in _TRAINING_REVIEW_VIEW_FILTERS:
        raise ValueError('观看方式筛选无效')
    if scene and scene not in _HERO_SCREEN_TYPES | {'hero_select', 'other'}:
        raise ValueError('画面类型筛选无效')
    heroes = (
        [hero.strip()]
        if isinstance(hero, str) and hero.strip()
        else [str(value).strip() for value in hero if str(value).strip()]
    )
    heroes = list(dict.fromkeys(heroes))
    if hero_scope not in {'all', 'direct'}:
        raise ValueError('英雄证据范围无效')
    if len(heroes) > 100:
        raise ValueError('英雄筛选数量过多')
    if not any(
        (
            streamer,
            source_type,
            scene,
            match_mode,
            match_kind,
            view_context,
            heroes,
            confidence,
        )
    ):
        return None

    item_rows = conn.execute(
        'SELECT item.*,video.streamer FROM training_review_items item '
        'JOIN frames frame ON frame.id=item.frame_id '
        'JOIN videos video ON video.id=frame.video_id'
    ).fetchall()
    matching = {int(row['frame_id']) for row in item_rows}
    if streamer:
        matching &= {
            int(row['frame_id'])
            for row in item_rows
            if str(row['streamer']) == streamer
        }
    if match_kind:
        matching &= {
            int(row['frame_id'])
            for row in item_rows
            if str(row['match_kind_label'] or '') == match_kind
        }
    if view_context:
        matching &= {
            int(row['frame_id'])
            for row in item_rows
            if str(row['view_context_label'] or '') == view_context
        }

    source_rows: Sequence[sqlite3.Row] = ()
    if source_type or scene or match_mode or confidence:
        source_rows = conn.execute(
            'SELECT source.frame_id,source.source_type,'
            'source.suggestions_json,'
            "json_extract(source.suggestions_json,'$.hero_select.label') "
            'AS hero_select_suggestion_label,'
            "json_extract(source.suggestions_json,'$.hero_select.confidence') "
            'AS hero_select_suggestion_confidence,'
            "json_extract(source.suggestions_json,'$.match_mode.label') "
            'AS match_mode_suggestion_label,'
            "json_extract(source.suggestions_json,'$.match_mode.confidence') "
            'AS match_mode_suggestion_confidence,'
            "json_extract(source.suggestions_json,'$.result_panel.label') "
            'AS result_panel_suggestion_label,'
            "json_extract(source.suggestions_json,'$.result_panel.confidence') "
            'AS result_panel_suggestion_confidence,'
            "json_extract(source.metadata_json,'$.hero_context_suggestion.screen_type') "
            'AS hero_context_screen_type,'
            "json_extract(source.metadata_json,'$.hero_context_suggestion.confidence') "
            'AS hero_context_confidence,'
            "json_extract(source.metadata_json,'$.game_mode') AS game_mode,"
            "json_extract(source.metadata_json,'$.mode_class') AS mode_class,"
            "json_extract(source.metadata_json,'$.screen_type') "
            'AS source_screen_type,'
            "json_extract(source.metadata_json,'$.stage_class') "
            'AS source_stage_class,'
            "json_extract(source.metadata_json,'$.model_outputs[0].stage_class') "
            'AS model_stage_class,'
            "json_extract(source.metadata_json,'$.model_outputs[0].mode_class') "
            'AS model_mode_class,'
            "json_extract(source.metadata_json,'$.manual_correction.after.game_mode') "
            'AS manual_game_mode FROM training_review_sources source'
        ).fetchall()
    if source_type:
        matching &= {
            int(row['frame_id'])
            for row in source_rows
            if str(row['source_type']) == source_type
        }
    if scene or match_mode:
        signals = _training_review_material_signals(source_rows)
        matching &= {
            int(row['frame_id'])
            for row in item_rows
            if (
                not scene
                or (
                    _training_review_material_scene(
                        row, signals.get(int(row['frame_id']), {})
                    )
                    or 'other'
                )
                == scene
            )
            if (
                not match_mode
                or (
                    str(row['match_mode_label'] or '') == 'unreadable'
                    if match_mode == 'unreadable'
                    else _training_review_material_mode(
                        row, signals.get(int(row['frame_id']), {})
                    )
                    == match_mode
                )
            )
        }
    if confidence:
        confidence_ids = set()
        for row in source_rows:
            suggestions = _training_review_json_object(row['suggestions_json'])
            values = [
                _training_review_float(value.get('confidence'))
                for value in suggestions.values()
                if isinstance(value, dict)
            ]
            matches_confidence = {
                'low': any(value < 0.6 for value in values),
                'boundary': any(0.6 <= value <= 0.85 for value in values),
                'high': any(value >= 0.85 for value in values),
            }[confidence]
            if matches_confidence:
                confidence_ids.add(int(row['frame_id']))
        matching &= confidence_ids
    if heroes:
        placeholders = ','.join('?' for _hero in heroes)
        hero_rows = conn.execute(
            'SELECT DISTINCT frame_id FROM training_review_hero_slots WHERE '
            f'confirmed_label IN ({placeholders}) OR '
            f'suggested_label IN ({placeholders})',
            (*heroes, *heroes),
        ).fetchall()
        matching &= {int(row['frame_id']) for row in hero_rows}
    return matching


def _training_review_attribute_frame_ids(
    conn: sqlite3.Connection,
    *,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
    review_reason: str = '',
) -> Optional[set[int]]:
    implementation = (
        _training_review_indexed_attribute_frame_ids
        if training_review_material_index_complete(conn)
        else _training_review_unindexed_attribute_frame_ids
    )
    matching = implementation(
        conn,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        match_kind=match_kind,
        view_context=view_context,
        hero=hero,
        hero_scope=hero_scope,
        confidence=confidence,
    )
    reason_condition, reason_parameters = _training_review_reason_condition(
        review_reason
    )
    if not reason_condition:
        return matching
    rows = conn.execute(
        'SELECT item.frame_id FROM training_review_items item WHERE '
        + reason_condition,
        reason_parameters,
    ).fetchall()
    reason_ids = {int(row['frame_id']) for row in rows}
    return reason_ids if matching is None else matching & reason_ids


def _training_review_visible_frame_ids(
    conn: sqlite3.Connection,
    *,
    status: str,
    source_scope: str,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
    review_reason: str = '',
    prefill_ready_only: bool = False,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Tuple[List[int], Dict[int, Dict[str, Any]]]:
    if status not in _TRAINING_REVIEW_STATUSES | {
        'all',
        'needs_review',
        'missing_player',
        'legacy_hero',
        'migration_review',
        'human_confirmed',
        'missing_afk',
    }:
        raise ValueError('训练复核状态无效')
    source_frame_ids = _training_review_origin_frame_ids(conn, source_scope)
    attribute_frame_ids = _training_review_attribute_frame_ids(
        conn,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        match_kind=match_kind,
        view_context=view_context,
        hero=hero,
        hero_scope=hero_scope,
        confidence=confidence,
        review_reason=review_reason,
    )
    indexed = training_review_material_index_complete(conn)
    base = (
        'SELECT frame_id FROM training_review_items '
        "ORDER BY CASE review_status WHEN 'pending' THEN 0 WHEN 'partial' THEN 1 "
        "WHEN 'confirmed' THEN 2 ELSE 3 END, updated_at DESC, frame_id DESC"
    )
    parameters: tuple[Any, ...] = ()
    if status == 'needs_review':
        if indexed:
            base = (
                'SELECT item.frame_id FROM training_review_items item '
                'JOIN training_review_material_index material '
                'ON material.frame_id=item.frame_id '
                "WHERE item.review_status IN ('pending', 'partial') "
                f'ORDER BY {_TRAINING_REVIEW_INDEXED_ARAM_PRIORITY}, '
                "CASE WHEN item.review_status = 'pending' THEN 0 ELSE 1 END, "
                'material.source_created_at DESC,material.source_offset DESC,'
                'item.updated_at DESC,item.frame_id DESC'
            )
        else:
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
    elif status == 'missing_afk':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            f'WHERE {_MISSING_AFK_REVIEW} '
            'ORDER BY item.updated_at DESC, item.frame_id DESC'
        )
    elif status == 'migration_review':
        material_join = (
            'JOIN training_review_material_index material '
            'ON material.frame_id=item.frame_id '
            if indexed
            else ''
        )
        legacy_condition = (
            'material.is_legacy=1'
            if indexed
            else 'EXISTS (SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id=item.frame_id '
            "AND source.source_type LIKE 'legacy_%')"
        )
        priority = (
            _TRAINING_REVIEW_INDEXED_ARAM_PRIORITY
            if indexed
            else _TRAINING_REVIEW_ARAM_PRIORITY
        )
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            + material_join
            + "WHERE item.review_status='confirmed' AND "
            + legacy_condition
            + f' AND NOT ({_UNIFIED_MANUAL_REVIEWED}) '
            + f'ORDER BY {priority},item.updated_at DESC,item.frame_id DESC'
        )
    elif status == 'human_confirmed':
        base = (
            'SELECT item.frame_id FROM training_review_items item '
            "WHERE item.review_status = 'confirmed' "
            f'AND ({_UNIFIED_MANUAL_REVIEWED}) '
            'ORDER BY item.reviewed_at DESC, item.frame_id DESC'
        )
    elif status == 'pending':
        if indexed:
            base = (
                'SELECT item.frame_id FROM training_review_items item '
                'JOIN training_review_material_index material '
                'ON material.frame_id=item.frame_id '
                "WHERE item.review_status='pending' "
                f'ORDER BY {_TRAINING_REVIEW_INDEXED_ARAM_PRIORITY},'
                'material.source_created_at DESC,material.source_offset DESC,'
                'item.updated_at DESC,item.frame_id DESC'
            )
        else:
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
    prefill_visible_ids: Optional[set[int]] = None
    if prefill_ready_only:
        prefill_visible_ids = {
            int(row['frame_id'])
            for row in conn.execute(
                'SELECT material.frame_id FROM training_review_material_index material '
                'JOIN training_review_items item ON item.frame_id=material.frame_id '
                "WHERE material.prefill_status='ready' OR "
                "item.review_status='skipped' OR (item.review_status='confirmed' "
                f'AND ({_UNIFIED_MANUAL_REVIEWED}))'
            ).fetchall()
        }
    groups = (
        training_review_result_groups(conn) if result_groups is None else result_groups
    )
    visible = [
        int(row['frame_id'])
        for row in rows
        if (source_frame_ids is None or int(row['frame_id']) in source_frame_ids)
        if (attribute_frame_ids is None or int(row['frame_id']) in attribute_frame_ids)
        if (prefill_visible_ids is None or int(row['frame_id']) in prefill_visible_ids)
        if groups.get(int(row['frame_id']), {}).get(
            'result_group_representative_frame_id', int(row['frame_id'])
        )
        == int(row['frame_id'])
    ]
    return visible, groups


def training_review_frame_ids(
    conn: sqlite3.Connection,
    *,
    status: str,
    source_scope: str,
    streamer: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
    review_reason: str = '',
    prefill_ready_only: bool = False,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[int]:
    default_new_queue = (
        status == 'needs_review'
        and source_scope == 'new'
        and not any(
            (
                streamer,
                source_type,
                scene,
                match_mode,
                match_kind,
                view_context,
                hero,
                hero_scope != 'all',
                confidence,
                review_reason,
            )
        )
    )
    if default_new_queue:
        ready_condition = (
            "AND material.prefill_status='ready' " if prefill_ready_only else ''
        )
        rows = conn.execute(
            'SELECT item.frame_id FROM training_review_items item '
            'JOIN training_review_material_index material '
            'ON material.frame_id=item.frame_id '
            "WHERE item.review_status IN ('pending','partial') "
            'AND material.is_new=1 '
            + ready_condition
            + 'AND material.result_group_representative_frame_id=item.frame_id '
            f'ORDER BY {_TRAINING_REVIEW_INDEXED_ARAM_PRIORITY},'
            "CASE WHEN item.review_status='pending' THEN 0 ELSE 1 END,"
            'material.source_created_at DESC,material.source_offset DESC,'
            'item.updated_at DESC,item.frame_id DESC'
        ).fetchall()
        if rows or training_review_material_index_complete(conn):
            return [int(row['frame_id']) for row in rows]
    if default_new_queue and not prefill_ready_only:
        rows = conn.execute(
            """
            WITH source_summary AS (
                SELECT source.frame_id,
                       MAX(source.source_created_at) AS source_created_at,
                       MAX(COALESCE(
                           CAST(json_extract(
                               source.metadata_json, '$.at_ms'
                           ) AS INTEGER),
                           CAST(json_extract(
                               source.metadata_json, '$.result_at_ms'
                           ) AS INTEGER),
                           0
                       )) AS source_offset,
                       MAX(CASE WHEN source.source_type IN (
                           'worker', 'result_archive', 'manual_correction'
                       ) THEN 1 ELSE 0 END) AS is_new,
                       MAX(CASE WHEN json_extract(
                           source.suggestions_json, '$.hero_select.label'
                       ) = 'select_aram' THEN 1 ELSE 0 END) AS selects_aram,
                       MAX(CASE WHEN
                           json_extract(
                               source.suggestions_json, '$.match_mode.label'
                           ) = 'aram'
                           OR json_extract(
                               source.metadata_json, '$.game_mode'
                           ) = 'aram'
                           OR json_extract(
                               source.metadata_json, '$.mode_class'
                           ) = 'aram'
                           OR json_extract(
                               source.metadata_json,
                               '$.model_outputs[0].mode_class'
                           ) = 'aram'
                       THEN 1 ELSE 0 END) AS suggests_aram
                FROM training_review_sources source
                JOIN training_review_items pending
                  ON pending.frame_id = source.frame_id
                 AND pending.review_status IN ('pending', 'partial')
                GROUP BY source.frame_id
            ),
            confirmed_aram_videos AS (
                SELECT DISTINCT frame.video_id
                FROM training_review_items known
                JOIN frames frame ON frame.id = known.frame_id
                WHERE known.review_status = 'confirmed'
                  AND (
                      known.match_mode_label = 'aram'
                      OR known.hero_select_label = 'select_aram'
                  )
            )
            SELECT item.frame_id
            FROM training_review_items item
            JOIN frames frame ON frame.id = item.frame_id
            JOIN source_summary summary ON summary.frame_id = item.frame_id
            LEFT JOIN confirmed_aram_videos confirmed_aram
              ON confirmed_aram.video_id = frame.video_id
            WHERE item.review_status IN ('pending', 'partial')
              AND summary.is_new = 1
            ORDER BY CASE
                         WHEN summary.selects_aram = 1 THEN 0
                         WHEN summary.suggests_aram = 1 THEN 1
                         WHEN confirmed_aram.video_id IS NOT NULL THEN 2
                         ELSE 3
                     END,
                     CASE WHEN item.review_status = 'pending' THEN 0 ELSE 1 END,
                     COALESCE(summary.source_created_at, 0) DESC,
                     COALESCE(summary.source_offset, 0) DESC,
                     item.updated_at DESC,
                     item.frame_id DESC
            """
        ).fetchall()
        groups = (
            training_review_result_groups(conn)
            if result_groups is None
            else result_groups
        )
        return [
            int(row['frame_id'])
            for row in rows
            if groups.get(int(row['frame_id']), {}).get(
                'result_group_representative_frame_id', int(row['frame_id'])
            )
            == int(row['frame_id'])
        ]
    visible, _groups = _training_review_visible_frame_ids(
        conn,
        status=status,
        source_scope=source_scope,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        match_kind=match_kind,
        view_context=view_context,
        hero=hero,
        hero_scope=hero_scope,
        confidence=confidence,
        review_reason=review_reason,
        prefill_ready_only=prefill_ready_only,
        result_groups=result_groups,
    )
    return visible


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
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
    review_reason: str = '',
    prefill_ready_only: bool = False,
) -> List[Dict[str, Any]]:
    items, _total = training_review_page(
        conn,
        status=status,
        limit=limit,
        offset=offset,
        source_scope=source_scope,
        streamer=streamer,
        hero_screen_type=hero_screen_type,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        match_kind=match_kind,
        view_context=view_context,
        hero=hero,
        hero_scope=hero_scope,
        confidence=confidence,
        review_reason=review_reason,
        prefill_ready_only=prefill_ready_only,
    )
    return items


def _training_review_indexed_page_frame_ids(
    conn: sqlite3.Connection,
    *,
    status: str,
    source_scope: str,
    streamer: str,
    source_type: str,
    scene: str,
    match_mode: str,
    match_kind: str,
    view_context: str,
    hero: Sequence[str] | str,
    hero_scope: str,
    confidence: str,
    prefill_ready_only: bool,
    limit: int,
    offset: int,
) -> Tuple[List[int], int]:
    if source_scope not in _TRAINING_REVIEW_SOURCE_SCOPES:
        raise ValueError('训练复核数据来源无效')
    if status not in _TRAINING_REVIEW_STATUSES | {'all', 'needs_review'}:
        raise ValueError('训练复核状态无效')
    if scene and scene not in _HERO_SCREEN_TYPES | {'hero_select', 'other'}:
        raise ValueError('画面类型筛选无效')
    if match_mode and match_mode not in _TRAINING_REVIEW_MODE_FILTERS:
        raise ValueError('对局模式筛选无效')
    if match_kind and match_kind not in _TRAINING_REVIEW_KIND_FILTERS:
        raise ValueError('对局性质筛选无效')
    if view_context and view_context not in _TRAINING_REVIEW_VIEW_FILTERS:
        raise ValueError('观看方式筛选无效')
    if confidence not in {'', 'low', 'boundary', 'high'}:
        raise ValueError('模型置信度筛选无效')
    conditions = ['material.result_group_representative_frame_id=item.frame_id']
    parameters: List[Any] = []
    if status == 'needs_review':
        conditions.append("item.review_status IN ('pending','partial')")
    elif status != 'all':
        conditions.append('item.review_status=?')
        parameters.append(status)
    if prefill_ready_only:
        conditions.append(
            "(material.prefill_status='ready' OR item.review_status='skipped' "
            "OR (item.review_status='confirmed' AND "
            f'({_UNIFIED_MANUAL_REVIEWED})))'
        )
    if source_scope == 'new':
        conditions.append('material.is_new=1')
    elif source_scope == 'legacy':
        conditions.append('material.is_legacy=1')
    if streamer:
        conditions.append('video.streamer=?')
        parameters.append(streamer)
    if source_type:
        conditions.append(
            'EXISTS (SELECT 1 FROM training_review_sources source '
            'WHERE source.frame_id=item.frame_id AND source.source_type=?)'
        )
        parameters.append(source_type)
    if scene:
        conditions.append('material.scene=?')
        parameters.append(scene)
    if match_mode:
        if match_mode == 'unreadable':
            conditions.append('item.match_mode_label=?')
            parameters.append(match_mode)
        elif match_mode == 'blitz':
            conditions.append('(item.match_mode_label=? OR material.match_mode=?)')
            parameters.extend((match_mode, match_mode))
        else:
            conditions.append('material.match_mode=?')
            parameters.append(match_mode)
    if match_kind:
        conditions.append('item.match_kind_label=?')
        parameters.append(match_kind)
    if view_context:
        conditions.append('item.view_context_label=?')
        parameters.append(view_context)
    heroes = _normalized_training_review_heroes(hero)
    if heroes:
        condition, hero_parameters = _training_review_related_hero_condition(
            heroes, scope=hero_scope, require_matching_scene=bool(scene)
        )
        conditions.append(condition)
        parameters.extend(hero_parameters)
    if confidence:
        column = {
            'low': 'has_low_confidence',
            'boundary': 'has_boundary_confidence',
            'high': 'has_high_confidence',
        }[confidence]
        conditions.append(f'material.{column}=1')
    from_sql = (
        ' FROM training_review_items item '
        'JOIN training_review_material_index material '
        'ON material.frame_id=item.frame_id '
        'JOIN frames frame ON frame.id=item.frame_id '
        'JOIN videos video ON video.id=frame.video_id '
    )
    where_sql = ' WHERE ' + ' AND '.join(conditions)
    count_row = conn.execute(
        'SELECT COUNT(*)' + from_sql + where_sql, parameters
    ).fetchone()
    total = 0 if count_row is None else int(count_row[0])
    hero_order_sql = ''
    hero_order_parameters: List[Any] = []
    if heroes and hero_scope == 'all':
        hero_order_sql, hero_order_parameters = _training_review_related_hero_order(
            heroes
        )
    if status in {'needs_review', 'pending'}:
        direct_scene_order = (
            'CASE WHEN material.prefill_screen_type=material.scene '
            'THEN 0 ELSE 1 END,'
            if scene in _HERO_SCREEN_TYPES
            else ''
        )
        order_sql = (
            ' ORDER BY '
            + hero_order_sql
            + direct_scene_order
            + f'{_TRAINING_REVIEW_INDEXED_ARAM_PRIORITY},'
            "CASE WHEN item.review_status='pending' THEN 0 ELSE 1 END,"
            'material.source_created_at DESC,material.source_offset DESC,'
            'item.updated_at DESC,item.frame_id DESC'
        )
    else:
        order_sql = (
            ' ORDER BY '
            + hero_order_sql
            + "CASE item.review_status WHEN 'pending' THEN 0 "
            "WHEN 'partial' THEN 1 WHEN 'confirmed' THEN 2 ELSE 3 END,"
            'item.updated_at DESC,item.frame_id DESC'
        )
    page_rows = conn.execute(
        'SELECT item.frame_id' + from_sql + where_sql + order_sql + ' LIMIT ? OFFSET ?',
        (*parameters, *hero_order_parameters, int(limit), int(offset)),
    ).fetchall()
    return [int(row['frame_id']) for row in page_rows], total


def training_review_page(
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
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
    review_reason: str = '',
    prefill_ready_only: bool = False,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    if limit < 1 or limit > 10_000 or offset < 0:
        raise ValueError('训练复核分页参数无效')
    if status == 'legacy_hero':
        if source_scope == 'new':
            return [], 0
        stats = legacy_hero_review_stats(
            conn,
            streamer=streamer,
            screen_type=hero_screen_type,
            prefill_ready_only=prefill_ready_only,
        )
        return (
            list_legacy_hero_review_items(
                conn,
                streamer=streamer,
                screen_type=hero_screen_type,
                limit=limit,
                offset=offset,
                prefill_ready_only=prefill_ready_only,
            ),
            int(stats['remaining_groups']),
        )
    if (
        (prefill_ready_only or training_review_material_index_complete(conn))
        and not review_reason
        and status in _TRAINING_REVIEW_STATUSES | {'all', 'needs_review'}
    ):
        frame_ids, total = _training_review_indexed_page_frame_ids(
            conn,
            status=status,
            source_scope=source_scope,
            streamer=streamer,
            source_type=source_type,
            scene=scene,
            match_mode=match_mode,
            match_kind=match_kind,
            view_context=view_context,
            hero=hero,
            hero_scope=hero_scope,
            confidence=confidence,
            prefill_ready_only=prefill_ready_only,
            limit=limit,
            offset=offset,
        )
        groups = (
            training_review_result_groups(conn)
            if result_groups is None
            else result_groups
        )
        items = get_training_review_items(conn, frame_ids, result_groups=groups)
        hero_matches = training_review_hero_filter_matches(conn, frame_ids, hero)
        for item in items:
            item['hero_filter_matches'] = hero_matches.get(int(item['frame_id']), [])
        return items, total
    visible, result_groups = _training_review_visible_frame_ids(
        conn,
        status=status,
        source_scope=source_scope,
        streamer=streamer,
        source_type=source_type,
        scene=scene,
        match_mode=match_mode,
        match_kind=match_kind,
        view_context=view_context,
        hero=hero,
        hero_scope=hero_scope,
        confidence=confidence,
        review_reason=review_reason,
        prefill_ready_only=prefill_ready_only,
        result_groups=result_groups,
    )
    result = get_training_review_items(
        conn, visible[offset : offset + limit], result_groups=result_groups
    )
    hero_matches = training_review_hero_filter_matches(
        conn, [int(item['frame_id']) for item in result], hero
    )
    for item in result:
        item['hero_filter_matches'] = hero_matches.get(int(item['frame_id']), [])
    return result, len(visible)


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
    match_kind: str = '',
    view_context: str = '',
    hero: Sequence[str] | str = (),
    hero_scope: str = 'all',
    confidence: str = '',
    review_reason: str = '',
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
        match_kind=match_kind,
        view_context=view_context,
        hero=hero,
        hero_scope=hero_scope,
        confidence=confidence,
        review_reason=review_reason,
    )
    return len(visible)


def training_review_allows_partial_hero_lineup(
    *,
    match_kind_label: Optional[str],
    result_panel_label: Optional[str],
    result_occlusion: str,
) -> bool:
    return match_kind_label == 'practice' or (
        result_panel_label == 'result_panel' and result_occlusion == 'occluded'
    )


def save_training_review(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    match_flow_label: Optional[str],
    match_mode_label: Optional[str],
    hero_select_label: Optional[str],
    result_panel_label: Optional[str],
    match_kind_label: Optional[str] = None,
    view_context_label: Optional[str] = None,
    hero_select_variant: Optional[str] = None,
    hero_select_visibility: Optional[str] = None,
    hero_layout_label: Optional[str] = None,
    panel_render_state: str = 'clear',
    ocr_usable: str = 'yes',
    result_occlusion: str = 'none',
    occluder_types: Sequence[str] = (),
    status: str = 'confirmed',
    notes: str = '',
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
    hydrate: bool = True,
    commit: bool = True,
) -> Dict[str, Any]:
    labels = {
        'match_flow': match_flow_label,
        'match_mode': match_mode_label,
        'match_kind': match_kind_label,
        'view_context': view_context_label,
        'hero_select': hero_select_label,
        'result_panel': result_panel_label,
    }
    for task, label in labels.items():
        if label is not None and label not in _TRAINING_REVIEW_LABELS[task]:
            if task == 'match_kind':
                raise ValueError('对局性质标签无效')
            if task == 'view_context':
                raise ValueError('观看方式标签无效')
            raise ValueError(f'{task} 标签无效')
    if (
        hero_select_variant is not None
        and hero_select_variant not in _HERO_SELECT_VARIANTS
    ):
        raise ValueError('英雄选择类型无效')
    if hero_select_label == 'select_aram':
        if hero_select_variant not in (None, 'random'):
            raise ValueError('大乱斗英雄选择只能标记为随机选英雄')
    elif hero_select_label in ('select_3v3', 'select_5v5', 'select_blitz'):
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
    if match_flow_label != 'match_flow' and (
        match_kind_label is not None or view_context_label is not None
    ):
        raise ValueError('非对局画面不能填写对局性质或观看方式')
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
    allows_partial_lineup = training_review_allows_partial_hero_lineup(
        match_kind_label=match_kind_label,
        result_panel_label=result_panel_label,
        result_occlusion=normalized_occlusion,
    )
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
            if lineup is None or lineup['screen_type'] != hero_layout_label:
                raise ValueError('请先画出并确认英雄头像')
            if lineup['review_status'] != 'confirmed' or not lineup['slots']:
                if allows_partial_lineup:
                    raise ValueError('请至少画出并确认一个实际可见的英雄头像')
                raise ValueError('请先画满并确认全部英雄头像')
            if not allows_partial_lineup and (
                len(lineup['slots']) != int(lineup['team_size']) * 2
            ):
                raise ValueError('请先画满并确认全部英雄头像')
    if status != 'skipped' and result_panel_label != 'result_panel':
        delete_box(conn, int(frame_id), 'result_panel', commit=False)
    timestamp = now()
    conn.execute(
        """
        INSERT INTO training_review_items
            (frame_id, match_flow_label, match_mode_label, match_kind_label,
             view_context_label, hero_select_label, hero_select_variant,
             hero_select_visibility, result_panel_label, hero_layout_label,
             panel_render_state, ocr_usable, result_occlusion, occluder_types,
             review_status, notes, created_at, updated_at, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(frame_id) DO UPDATE SET
            match_flow_label=excluded.match_flow_label,
            match_mode_label=excluded.match_mode_label,
            match_kind_label=excluded.match_kind_label,
            view_context_label=excluded.view_context_label,
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
            match_kind_label,
            view_context_label,
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
        commit=False,
    )
    from . import model_quality

    model_quality.refresh_frame(conn, int(frame_id), commit=False)
    refresh_training_review_material_index(conn, int(frame_id), commit=False)
    if commit:
        conn.commit()
    if not hydrate:
        return {
            'frame_id': int(frame_id),
            'match_flow_label': match_flow_label,
            'match_mode_label': match_mode_label,
            'hero_select_label': hero_select_label,
            'hero_select_variant': hero_select_variant,
            'hero_select_visibility': normalized_select_visibility,
            'result_panel_label': result_panel_label,
            'hero_layout_label': hero_layout_label,
            'panel_render_state': normalized_render_state,
            'ocr_usable': normalized_ocr,
            'result_occlusion': normalized_occlusion,
            'occluder_types': normalized_occluders,
            'review_status': status,
            'notes': notes[:1000],
            'updated_at': timestamp,
            'reviewed_at': timestamp if status in ('confirmed', 'skipped') else None,
        }
    item = get_training_review_item(conn, int(frame_id), result_groups=result_groups)
    if item is None:
        raise KeyError(frame_id)
    return item


def hero_layout_key(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ValueError('图片尺寸必须为正数')
    return f'{width / height:.3f}'


def update_frame_dimensions(
    conn: sqlite3.Connection,
    frame_id: int,
    width: int,
    height: int,
    *,
    commit: bool = True,
) -> bool:
    if width <= 0 or height <= 0:
        raise ValueError('图片尺寸必须为正数')
    cursor = conn.execute(
        'UPDATE frames SET width=?,height=? WHERE id=? ' 'AND (width<=0 OR height<=0)',
        (int(width), int(height), int(frame_id)),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


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
        'suggested_label, suggestion_confidence, confirmed_label, is_afk, updated_at '
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
            'is_afk': (None if slot['is_afk'] is None else bool(int(slot['is_afk']))),
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
    refresh_training_review_material_index(conn, int(frame_id))
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
    refresh_material_index: bool = True,
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
    if refresh_material_index:
        refresh_training_review_material_index(conn, int(frame_id))
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
    require_complete: bool = True,
    refresh_material_index: bool = True,
    commit: bool = True,
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
        raw_is_afk = value.get('is_afk') if 'is_afk' in value else None
        if raw_is_afk is not None and not isinstance(raw_is_afk, bool):
            raise ValueError('挂机标记必须是布尔值')
        normalized.append((hero_label, raw_is_afk, side, slot))
    drawn_positions = {
        (str(slot['side']), int(slot['slot'])) for slot in lineup['slots']
    }
    required_positions = expected if require_complete else drawn_positions
    if not required_positions:
        raise ValueError('必须至少确认一个实际可见的英雄位置')
    if positions != required_positions:
        if not require_complete:
            raise ValueError('必须确认所有已画出的英雄位置')
        raise ValueError(f'必须确认完整的 {team_size * 2} 个英雄位置')
    if lineup['screen_type'] != 'result_page' and any(
        is_afk is not None for _label, is_afk, _side, _slot in normalized
    ):
        raise ValueError('只有真正结算图采集挂机标签')
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
        if (normalized_player_side, normalized_player_slot) not in positions:
            raise ValueError('主播英雄位置无效')
    elif player_side is not None or player_slot is not None:
        raise ValueError('主播英雄位置状态冲突')
    timestamp = now()
    conn.executemany(
        'UPDATE training_review_hero_slots SET confirmed_label = ?, '
        'is_afk=COALESCE(?,is_afk), updated_at = ? '
        'WHERE frame_id = ? AND side = ? AND slot = ?',
        [
            (
                label,
                int(is_afk) if is_afk is not None else None,
                timestamp,
                int(frame_id),
                side,
                slot,
            )
            for label, is_afk, side, slot in normalized
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
                    {'side': side, 'slot': slot, 'hero_label': label, 'is_afk': is_afk}
                    for label, is_afk, side, slot in normalized
                ],
                'player_status': normalized_player_status,
                'player_side': normalized_player_side,
                'player_slot': normalized_player_slot,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ),
        commit=False,
    )
    labels_by_position = {
        (side, slot): (label, is_afk) for label, is_afk, side, slot in normalized
    }
    for slot in lineup['slots']:
        label, is_afk = labels_by_position[(str(slot['side']), int(slot['slot']))]
        slot['confirmed_label'] = label
        if is_afk is not None:
            slot['is_afk'] = is_afk
        slot['updated_at'] = timestamp
    lineup.update(
        {
            'review_status': 'confirmed',
            'player_status': normalized_player_status,
            'player_side': normalized_player_side,
            'player_slot': normalized_player_slot,
            'updated_at': timestamp,
            'reviewed_at': timestamp,
        }
    )
    from . import model_quality

    model_quality.refresh_frame(conn, int(frame_id), hero_lineup=lineup, commit=False)
    if refresh_material_index:
        refresh_training_review_material_index(conn, int(frame_id), commit=False)
    if commit:
        conn.commit()
    return lineup


def training_review_stats(
    conn: sqlite3.Connection,
    *,
    result_groups: Optional[Dict[int, Dict[str, Any]]] = None,
    include_material_suggestions: bool = True,
    hero_catalog: Sequence[Dict[str, str]] = (),
) -> Dict[str, Any]:
    duplicates = training_review_duplicate_result_frame_ids(
        conn, result_groups=result_groups
    )
    item_rows = conn.execute(
        'SELECT frame_id,review_status,match_flow_label,match_mode_label,'
        'hero_select_label,hero_select_variant,result_panel_label,'
        'hero_layout_label FROM training_review_items'
    ).fetchall()
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
    category_source_rows = conn.execute(
        'SELECT frame_id,source_type FROM training_review_sources'
    ).fetchall()
    for row in category_source_rows:
        frame_id = int(row['frame_id'])
        if frame_id not in visible_ids:
            continue
        categories_by_frame.setdefault(frame_id, set()).add(
            _training_review_source_category(row['source_type'])
        )
    source_rows = []
    if include_material_suggestions:
        source_rows = conn.execute(
            'SELECT source.frame_id,source.source_type,'
            "json_extract(source.suggestions_json,'$.hero_select.label') "
            'AS hero_select_suggestion_label,'
            "json_extract(source.suggestions_json,'$.hero_select.confidence') "
            'AS hero_select_suggestion_confidence,'
            "json_extract(source.suggestions_json,'$.match_mode.label') "
            'AS match_mode_suggestion_label,'
            "json_extract(source.suggestions_json,'$.match_mode.confidence') "
            'AS match_mode_suggestion_confidence,'
            "json_extract(source.suggestions_json,'$.result_panel.label') "
            'AS result_panel_suggestion_label,'
            "json_extract(source.suggestions_json,'$.result_panel.confidence') "
            'AS result_panel_suggestion_confidence,'
            "json_extract(source.metadata_json,'$.hero_context_suggestion.screen_type') "
            'AS hero_context_screen_type,'
            "json_extract(source.metadata_json,'$.hero_context_suggestion.confidence') "
            'AS hero_context_confidence,'
            "json_extract(source.metadata_json,'$.game_mode') AS game_mode,"
            "json_extract(source.metadata_json,'$.mode_class') AS mode_class,"
            "json_extract(source.metadata_json,'$.screen_type') "
            'AS source_screen_type,'
            "json_extract(source.metadata_json,'$.stage_class') "
            'AS source_stage_class,'
            "json_extract(source.metadata_json,'$.model_outputs[0].stage_class') "
            'AS model_stage_class,'
            "json_extract(source.metadata_json,'$.model_outputs[0].mode_class') "
            'AS model_mode_class,'
            "json_extract(source.metadata_json,'$.manual_correction.after.game_mode') "
            'AS manual_game_mode FROM training_review_sources source '
            'JOIN training_review_items item ON item.frame_id=source.frame_id '
            "WHERE item.review_status IN ('pending','partial') AND (("
            "COALESCE(item.hero_select_label,'') NOT IN "
            "('select_3v3','select_aram','select_5v5','select_blitz') AND "
            "COALESCE(item.hero_layout_label,'') NOT IN "
            "('gameplay_hud','scoreboard','result_page') AND "
            "COALESCE(item.result_panel_label,'')!='result_panel') OR ("
            "COALESCE(item.match_mode_label,'') NOT IN "
            "('3v3','aram','5v5','blitz') AND "
            "COALESCE(item.hero_select_label,'') NOT IN "
            "('select_3v3','select_aram','select_5v5','select_blitz')))"
        ).fetchall()
    hero_rows = []
    if include_material_suggestions and hero_catalog:
        hero_rows = conn.execute(
            'SELECT lineup.frame_id,lineup.screen_type,'
            'lineup.review_status AS lineup_review_status,'
            'item.review_status AS item_review_status,'
            'slot.suggested_label,slot.confirmed_label '
            'FROM training_review_hero_lineups lineup '
            'JOIN training_review_hero_slots slot '
            'ON slot.frame_id=lineup.frame_id '
            'JOIN training_review_items item ON item.frame_id=lineup.frame_id'
        ).fetchall()
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
    prefill_status_by_frame = {
        int(row['frame_id']): str(row['prefill_status'])
        for row in conn.execute(
            'SELECT frame_id,prefill_status FROM training_review_material_index'
        ).fetchall()
    }
    scope_ids = {
        'new': {
            frame_id
            for frame_id, categories in categories_by_frame.items()
            if categories & {'worker', 'result_archive', 'manual_correction'}
        },
        'legacy': legacy_ids,
    }
    for scope, frame_ids in scope_ids.items():
        scope_rows = [row for row in visible_rows if int(row['frame_id']) in frame_ids]
        scope_statuses: Dict[str, int] = {}
        for row in scope_rows:
            review_status = str(row['review_status'])
            scope_statuses[review_status] = scope_statuses.get(review_status, 0) + 1
        scope_prefill_statuses: Dict[str, int] = {}
        for frame_id in frame_ids:
            prefill_status = prefill_status_by_frame.get(frame_id, 'pending')
            scope_prefill_statuses[prefill_status] = (
                scope_prefill_statuses.get(prefill_status, 0) + 1
            )
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
            'prefill_statuses': scope_prefill_statuses,
            'prefill_ready': int(scope_prefill_statuses.get('ready', 0)),
            'prefill_waiting': sum(
                int(scope_prefill_statuses.get(value, 0))
                for value in ('pending', 'queued', 'running')
            ),
            'prefill_failed': int(scope_prefill_statuses.get('failed', 0)),
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
        'prefill': training_review_prefill_stats(conn),
        'legacy_data': legacy_data,
        'material_suggestions': (
            (
                training_review_material_suggestions(conn, hero_catalog=hero_catalog)
                if training_review_material_index_complete(conn)
                else _training_review_material_suggestions(
                    visible_rows,
                    source_rows,
                    categories_by_frame,
                    hero_rows=hero_rows,
                    hero_catalog=hero_catalog,
                )
            )
            if include_material_suggestions
            else None
        ),
    }
