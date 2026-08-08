"""构建 multi-v2 数据集:content + stage(7 类,拆分积分板) + mode(按界面策略)。

stage 7 类:gameplay / scoreboard / result_page / victory_defeat /
           pre_match / out_of_match / transition
  - scoreboard 从 in_match 拆出(积分板/死亡积分板),让粗扫能看到积分板帧

mode 标签策略(按界面,与用户确认):
  - 对局中/胜负动画(gameplay/victory_defeat):地图相同,3v3 与大乱斗不可分
      → 标签:5v5 保持 5v5;3v3 与大乱斗统一标 3v3(推断时输出"3v3 或大乱斗")
  - 积分板/死亡积分板/结算页(scoreboard/death_scoreboard/result_page):
      可看天赋 → 真实标签 3v3/aram/5v5
  - 天赋选择(talent_select):必然大乱斗 → 标 aram
  - 英雄选择(hero_select_bp/blind/aram):三模式不同 → 真实标签
  - 排队(matchmaking):右下角文字可辨 → 真实标签
  - 其他界面(大厅/商店/出装/阵容确认/设置等):不参与 mode loss(mode=None)
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, '.')
import labeler.db as db
from labeler.config import DB_PATH, FRAME_DIR

OUT = Path('data/datasets/multi-v2')

STAGE_CLS = ['gameplay', 'scoreboard', 'result_page', 'victory_defeat',
             'pre_match', 'out_of_match', 'transition', 'talent_select']
MODE_CLS = ['3v3', 'aram', '5v5']
CONTENT_CLS = ['vainglory', 'not_vainglory']

# screen_type → stage 类
STAGE_MAP = {
    'gameplay': 'gameplay',
    'ingame_shop': 'gameplay',
    'equipment_select': 'gameplay',
    'talent_select': 'talent_select',
    'settings_or_pause': 'gameplay',
    'scoreboard': 'scoreboard',
    'death_scoreboard': 'scoreboard',
    'result_page': 'result_page',
    'victory_defeat_animation': 'victory_defeat',
    'matchmaking': 'pre_match',
    'match_confirm': 'pre_match',
    'hero_select_bp': 'pre_match',
    'hero_select_blind': 'pre_match',
    'hero_select_aram': 'pre_match',
    'lineup_confirm': 'pre_match',
    'reconnect': 'transition',
    'main_lobby': 'out_of_match',
    'hero_roster': 'out_of_match',
    'global_store': 'out_of_match',
    'profile_or_rank': 'out_of_match',
    'hero_list': 'out_of_match',
    'other_out_of_match': 'out_of_match',
}

# screen_type → mode 标签规则
#  True  = 真实标签(game_mode 为准)
#  '3v3' = 合并(3v3 与大乱斗统一标 3v3)
#  None  = 不参与 mode loss
MODE_RULE = {
    'gameplay': '3v3',  # 光栅实验已证伪:224 下学不到,恢复合并(3v3/大乱斗统一)
    'victory_defeat_animation': '3v3',
    'ingame_shop': None,
    'equipment_select': None,
    'talent_select': True,
    'settings_or_pause': None,
    'scoreboard': True,
    'death_scoreboard': True,
    'result_page': True,
    'matchmaking': True,
    # 接受/拒绝界面本身没有可靠模式证据，也绝不是 BP 正样本。
    'match_confirm': None,
    'hero_select_bp': True,
    'hero_select_blind': True,
    'hero_select_aram': True,
    'lineup_confirm': None,
    'reconnect': None,
    'main_lobby': None,
    'hero_roster': None,
    'global_store': None,
    'profile_or_rank': None,
    'hero_list': None,
    'other_out_of_match': None,
}

# 切分:train 必须有 aram(541)与 5v5(601)的可判界面帧才能学;
#       val 覆盖三模式(287 aram/13 5v5/494 3v3);test 验证 3v3 泛化(604/516)
SPLIT = {
    '541': 'train', '601': 'train',
    '168': 'train', '169': 'train', '173': 'train',
    '254': 'train', '579': 'train', '912': 'train',
    '287': 'val', '13': 'val', '494': 'val',
    '604': 'test', '516': 'test',
}


def stage_of(ann):
    if ann['content_family'] != 'vainglory':
        return None
    return STAGE_MAP.get(ann['screen_type'],
                         ann['game_context'] or 'out_of_match')


def mode_of(ann):
    """返回 mode 标签或 None(不参与 loss)。"""
    rule = MODE_RULE.get(ann['screen_type'])
    if rule is True:
        return ann['game_mode'] if ann['game_mode'] in MODE_CLS else None
    if rule == '3v3':
        # 对局中/胜负动画:5v5 保持,3v3 与大乱斗统一标 3v3
        if ann['game_mode'] == '5v5':
            return '5v5'
        if ann['game_mode'] in ('3v3', 'aram'):
            return '3v3'
        return None
    return None


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
    mode = mode_of(ann)
    content = ann['content_family'] if ann['content_family'] in CONTENT_CLS else None
    samples.append({
        'frame_id': r['frame_id'], 'sha': r['sha256'],
        'video_id': r['video_id'], 'screen_type': ann['screen_type'],
        'content': content, 'stage': stage, 'mode': mode,
    })
conn.close()

print('stage:', dict(Counter(s['stage'] for s in samples)))
print('mode:', dict(Counter(s['mode'] for s in samples)))

for s in samples:
    s['split'] = SPLIT.get(str(s['video_id']), 'train')

OUT.mkdir(parents=True, exist_ok=True)
for sp in ('train', 'val', 'test'):
    items = [s for s in samples if s['split'] == sp]
    with open(OUT / f'{sp}.json', 'w') as f:
        for s in items:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')
    print(f'{sp}: {len(items)} 张 | mode={dict(Counter(s["mode"] for s in items))} '
          f'| stage={dict(Counter(s["stage"] for s in items))}')
print('完成:', OUT)
