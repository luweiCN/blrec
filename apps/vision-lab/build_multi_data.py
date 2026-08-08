"""构建多输出头数据集:content(虚荣/非虚荣) + stage(6 类,拆分结算页) + mode(3 类)。

- stage 标签:in_match / pre_match / out_of_match / result_page / victory_defeat / transition
  (content=vainglory 才有;not_vainglory 帧 stage=-1 不参与 loss)
- mode 标签:3v3 / aram / 5v5(用户标注了 game_mode 才有;否则 -1 不参与 loss)
- content 标签:vainglory / not_vainglory(全部帧都有)
- 按视频 8:1:1 防泄漏切分,输出为 JSON 清单(样本 → 文件+标签)
"""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, '.')
import labeler.db as db
from labeler.config import DB_PATH, FRAME_DIR

OUT = Path('data/datasets/multi-v1')

STAGE_CLS = ['in_match', 'pre_match', 'out_of_match', 'result_page',
             'victory_defeat', 'transition']
MODE_CLS = ['3v3', 'aram', '5v5']
CONTENT_CLS = ['vainglory', 'not_vainglory']


def stage_of(ann):
    if ann['content_family'] != 'vainglory':
        return None
    st = ann['screen_type']
    if st == 'result_page':
        return 'result_page'
    if st == 'victory_defeat_animation':
        return 'victory_defeat'
    return ann['game_context'] or 'out_of_match'


conn = db.connect(DB_PATH)
rows = conn.execute(
    'SELECT a.frame_id, f.sha256, f.video_id FROM annotations a '
    'JOIN frames f ON f.id = a.frame_id '
    "WHERE a.annotation_status = 'complete'").fetchall()
samples = []
for r in rows:
    ann = dict(conn.execute(
        'SELECT * FROM annotations WHERE frame_id = ?', (r['frame_id'],)
    ).fetchone())
    img = FRAME_DIR / f"{r['sha256']}.jpg"
    if not img.exists():
        continue
    stage = stage_of(ann)
    mode = ann['game_mode'] if ann['game_mode'] in MODE_CLS else None
    content = ann['content_family'] if ann['content_family'] in CONTENT_CLS else None
    samples.append({
        'frame_id': r['frame_id'], 'sha': r['sha256'],
        'video_id': r['video_id'],
        'content': content, 'stage': stage, 'mode': mode,
    })
conn.close()

# 统计
print('content:', dict(Counter(s['content'] for s in samples)))
print('stage:', dict(Counter(s['stage'] for s in samples)))
print('mode:', dict(Counter(s['mode'] for s in samples)))

# 按视频分层切分(手工指定,保证 val/test 覆盖结算页/胜负动画 + 三模式):
#   train: 3v3(168,169,173,254,579) + aram(287) + 5v5(601)
#   val:   3v3(494,516) + 5v5(13)   rp32/vd14
#   test:  aram(541) + 3v3(604)     rp27/vd36
SPLIT = {
    '168': 'train', '169': 'train', '173': 'train', '254': 'train',
    '287': 'train', '579': 'train', '601': 'train', '912': 'train',
    '494': 'val', '13': 'val', '516': 'val',
    '541': 'test', '604': 'test',
}
n_train = sum(1 for v in SPLIT.values() if v == 'train')
n_val = sum(1 for v in SPLIT.values() if v == 'val')
n_test = sum(1 for v in SPLIT.values() if v == 'test')
print(f'视频数 {len(SPLIT)}: train {n_train} / val {n_val} / test {n_test}')

for s in samples:
    s['split'] = SPLIT.get(str(s['video_id']), 'train')

# 写 JSON 清单(样本内容轻量,图片复用 FRAME_DIR 原图,不复制)
OUT.mkdir(parents=True, exist_ok=True)
for sp in ('train', 'val', 'test'):
    items = [s for s in samples if s['split'] == sp]
    with open(OUT / f'{sp}.json', 'w') as f:
        for s in items:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')
    cs = Counter(s['content'] for s in items)
    ss = Counter(s['stage'] for s in items)
    ms = Counter(s['mode'] for s in items)
    print(f'{sp}: content={dict(cs)} stage={dict(ss)} mode={dict(ms)}')
print('完成:', OUT)
