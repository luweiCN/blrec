# Transcode Remux Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在原文件重传仍被 B 站明确判定转码失败后，以 FFmpeg 流复制重新封装并替换原 AID 的失败分 P。

**Architecture:** Migration 15 持久化每个分 P 的修复阶段和尝试次数。现有第一阶段保留；ReviewWatcher 再次确认同一分 P 终止失败后排队第二阶段。独立 remux 工具安全调用 ffmpeg/ffprobe，验证后复用 UPOS 上传和原稿编辑。

**Tech Stack:** Python asyncio、SQLite leases、FFmpeg、ffprobe、pytest。

### Task 1: 分 P 修复历史

**Files:**
- Create: `src/blrec/bili_upload/migrations/0015_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/task_actions.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_task_actions.py`

- [ ] 写失败测试：每个分 P 最多一次 original 和一次 remux，重启保留阶段，处理中不升级。
- [ ] 添加阶段/次数/诊断/临时路径字段和原子阶段转换。
- [ ] 运行聚焦测试。

### Task 2: 安全重新封装与校验

**Files:**
- Create: `src/blrec/bili_upload/transcode_remux.py`
- Test: `tests/bili_upload/test_transcode_remux.py`

- [ ] 写失败测试：参数数组、`shell=False`、时间戳规范化、超时、视频/音频流、时长容差、不可读输出和临时文件清理。
- [ ] 实现 `ffmpeg -fflags +genpts -i INPUT -map 0 -c copy -avoid_negative_ts make_zero OUTPUT`，输出使用独占临时文件。
- [ ] 用 ffprobe JSON 校验至少一个视频流、源文件存在的音频流、正时长和可读性。
- [ ] 运行 `pytest -q tests/bili_upload/test_transcode_remux.py`。

### Task 3: 第二阶段协调器

**Files:**
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Test: `tests/bili_upload/test_task_actions.py`
- Test: `tests/bili_upload/test_review.py`

- [ ] 写失败测试：第一阶段后再次终止失败才 remux；只替换失败分 P；沿用原 AID/BVID；未知编辑结果暂停；耗尽后终止。
- [ ] 接入 remux、UPOS、原稿编辑和审核轮询，阶段外部写前持久化，结束后始终清理临时文件。
- [ ] 运行上传、审核和任务动作聚焦测试。

### Task 4: 页面和运行环境

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,spec.ts}`
- Modify: `compose.synology.yml`

- [ ] 展示“原文件重传/重新封装/等待转码/自动修复失败”和诊断摘要。
- [ ] 确认镜像包含 ffmpeg/ffprobe，运行后端与前端聚焦测试。
