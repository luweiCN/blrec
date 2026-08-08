"""虚荣视觉标注工作台 —— 自检脚本。

用法: .venv/bin/python selftest.py [--nas] [--smoke <video_id>]
--nas    直接测试 NAS 访问(不经 API)
--smoke  对指定视频跑完整冒烟:抽帧(dense_interval 全片)→ 自动分组 → 标注 → 画框 → 导出 → 清理
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

BASE = 'http://127.0.0.1:8800'


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def post(path: str, body=None) -> dict:
    data = json.dumps(body or {}).encode('utf-8')
    req = urllib.request.Request(
        BASE + path, data=data, headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode('utf-8'))


def put(path: str, body: dict) -> dict:
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        BASE + path, data=data, headers={'Content-Type': 'application/json'},
        method='PUT',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--nas', action='store_true', help='NAS 直连检查')
    parser.add_argument('--smoke', type=int, help='完整冒烟(指定视频 id)')
    args = parser.parse_args()

    if args.nas:
        from labeler.nas import NasClient
        nas = NasClient()
        videos = nas.list_videos()
        print(f'[OK] NAS 直连: {len(videos)} 个视频')
        if videos:
            d = nas.ffprobe_duration(videos[0]['remote_path'])
            print(f'[OK] ffprobe 时长: {d:.1f}s ({videos[0]["filename"]})')
        return 0

    if args.smoke:
        vid = args.smoke
        print(f'[..] 视频 #{vid} 冒烟:抽帧(dense_interval 全片 4fps)…')
        post('/api/extract', {
            'video_ids': [vid], 'strategy': 'dense_interval',
            'params': {'start_ms': 0, 'end_ms': 3600_000, 'fps': 4},
        })
        for _ in range(120):
            time.sleep(2)
            st = get('/api/extract/state')
            if not st['running'] and not st['progress']:
                break
            p = list(st['progress'].values())[0]
            if p.get('status') == 'failed':
                print(f"[FAIL] 抽帧失败: {p.get('error')}")
                return 1
        frames = get('/api/frames?video_id=%d&limit=1000' % vid)['frames']
        print(f'[OK] 抽帧完成: {len(frames)} 帧(真实 PTS)')
        if not frames:
            print('[FAIL] 无帧')
            return 1
        print(f'      示例帧: id={frames[0]["id"]} pts={frames[0]["timestamp_ms"]}ms '
              f'{frames[0]["width"]}x{frames[0]["height"]} sha={frames[0]["sha256"][:8]}')

        r = post('/api/events/auto-group', {'video_id': vid})
        print(f'[OK] 事件分组: {r["events"]} 个事件')

        fid = frames[0]['id']
        put(f'/api/frames/{fid}/annotation', {
            'content_family': 'vainglory', 'game_context': 'in_match',
            'screen_type': 'result_page', 'game_mode': '3v3',
            'match_kind': 'unknown', 'view_context': 'played',
            'quality_flags': [], 'black_bars': 'none', 'ocr_usable': 'yes',
            'notes': 'selftest',
        })
        put(f'/api/frames/{fid}/box', {
            'box_type': 'result_panel', 'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.5,
        })
        print(f'[OK] 标注+画框: 帧 #{fid} result_page + result_panel 框')

        exp = post('/api/export', {'task_id': 'result_detector',
                                   'include_negatives': True})
        print(f"[OK] 导出: {exp['version']} 正 {exp['positive']} / 负 {exp['negative']}")
        print(f"     位置: {exp['dir']}")
        print('[OK] 冒烟通过')
        return 0

    cfg = get('/api/config')
    tasks = get('/api/tasks')
    videos = get('/api/videos')
    stats = get('/api/stats')
    print(f'[OK] 服务在线: {len(tasks)} 个训练目标, {len(videos)} 个视频')
    print(f'[OK] 统计: 帧 {stats["frames"]} / 事件 {stats["events"]} / '
          f'已标 {stats["frames_labeled"]} / 结算正样本 {stats["result_positives"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
