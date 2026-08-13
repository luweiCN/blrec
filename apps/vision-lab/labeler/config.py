"""虚荣视觉标注工作台 —— 常量与配置。"""

import os
from pathlib import Path


def _default_work_dir() -> Path:
    source_data = Path(__file__).resolve().parent.parent / 'data'
    if source_data.is_dir():
        return source_data
    return Path.home() / '.local' / 'share' / 'blrec-vision-lab'


# 可变数据不属于 Python 包；源码运行时兼容现有 data/，安装后使用用户数据目录。
WORK_DIR = Path(
    os.environ.get('VISION_LAB_DATA_DIR', str(_default_work_dir()))
).expanduser()
FRAME_DIR = WORK_DIR / 'frames'
THUMB_DIR = WORK_DIR / 'thumbs'
EXPORT_DIR = WORK_DIR / 'datasets'
DB_PATH = WORK_DIR / 'lab.db'
LOCAL_VIDEO_DIR = WORK_DIR / 'videos'  # 打标时下载到本地的视频(mp4)
MODELS_DIR = WORK_DIR / 'models'  # 训练好的模型(onnx)
THUMB_WIDTH = 960  # 网页显示缩略图宽度(原始帧永久保留)

# NAS 信息(凭据只从环境变量读取,见 AGENTS.md)
NAS_HOST = os.environ.get('VISION_LAB_NAS_HOST', '192.168.50.24').strip()
NAS_REC_DIR = os.environ.get(
    'VISION_LAB_NAS_RECORDING_DIR', '/volume1/docker/blrec-next/rec'
).rstrip('/')
NAS_TRAINING_CANDIDATE_DIR = os.environ.get(
    'VISION_LAB_NAS_CANDIDATE_DIR',
    os.environ.get(
        'BLREC_LABELING_NAS_CANDIDATE_DIR',
        '/volume1/docker/blrec-next/vision-data/candidates',
    ),
)
NAS_RESULT_FRAME_DIR = os.environ.get(
    'VISION_LAB_NAS_RESULT_FRAME_DIR', '/cfg/vainglory-result-frames'
).rstrip('/')

# MacBook Pro Analysis Worker 模型包发布。SSH 只使用系统密钥/SSH agent，
# Vision Lab 不保存 Worker 密码。
WORKER_SSH_HOST = os.environ.get('VISION_LAB_WORKER_SSH_HOST', '192.168.50.143').strip()
WORKER_SSH_USER = os.environ.get(
    'VISION_LAB_WORKER_SSH_USER', os.environ.get('USER', '')
).strip()
WORKER_SSH_PORT = os.environ.get('VISION_LAB_WORKER_SSH_PORT', '22').strip()
WORKER_SSH_IDENTITY = os.environ.get('VISION_LAB_WORKER_SSH_IDENTITY', '').strip()
WORKER_MODEL_ROOT = os.environ.get(
    'VISION_LAB_WORKER_MODEL_ROOT',
    '~/Library/Application Support/BLRECAnalysisWorker/model-packages',
).strip()
WORKER_LAUNCHD_LABEL = os.environ.get(
    'VISION_LAB_WORKER_LAUNCHD_LABEL', 'com.luwei.blrec-analysis-worker'
).strip()
WORKER_LAUNCHD_PLIST = os.environ.get(
    'VISION_LAB_WORKER_LAUNCHD_PLIST',
    '~/Library/LaunchAgents/com.luwei.blrec-analysis-worker.plist',
).strip()

VIDEO_EXTS = {'.flv', '.mp4', '.ts', '.mkv', '.m4s'}

# ---------- 分层标注体系 ----------

# 顶层:是否为《虚荣》画面
CONTENT_FAMILIES = {
    'vainglory': '虚荣画面',
    'not_vainglory': '非虚荣画面',
    'uncertain': '不确定',
}

# 非虚荣画面的可选原因
NON_VAINGLORY_TYPES = {
    'other_game': '其他游戏',
    'desktop_or_app': '桌面/应用界面',
    'web_or_video_page': '网页/视频页',
    'stream_cover_or_overlay': '直播封面/贴片',
    'black_or_loading': '黑屏/加载',
    'other': '其他',
}

# 虚荣画面:对局阶段(第二层,必填)
GAME_STAGES = {
    'out_of_match': '游戏外',
    'pre_match': '对局前',
    'in_match': '对局进行中',
    'post_match': '对局结束后',
    'transition': '转场或中断',
    'unknown': '无法确定',
}

# 各对局阶段的具体界面(第三层,必填)
STAGE_SCREEN_TYPES = {
    'out_of_match': {
        'main_lobby': '游戏主页',
        'backpack': '背包',
        'hero_list': '英雄列表',
        'hero_detail': '英雄详情',
        'skin_list': '皮肤列表',
        'skin_detail': '皮肤详情',
        'talent_list': '天赋列表',
        'talent_detail': '天赋详情',
        'out_store': '游戏外商店',
        'profile_or_rank': '个人资料或排行榜',
        'out_settings': '设置',
        'other_out': '其他游戏外界面',
    },
    'pre_match': {
        'matchmaking': '匹配排队',
        'match_confirm': '匹配确认（接受／拒绝）',
        'hero_select_bp': '英雄选择(BP/征召)',
        'hero_select_blind': '英雄选择(盲选)',
        'hero_select_aram': '英雄选择(大乱斗)',
        'lineup_confirm': '阵容确认',
        'load_before_match': '进入游戏加载',
        'other_pre': '其他对局前画面',
    },
    'in_match': {
        'gameplay': '普通战斗画面',
        'scoreboard': '游戏内积分板',
        'death_scoreboard': '死亡积分板',
        'talent_select': '天赋选择',
        'ingame_shop': '游戏内商店',
        'equipment_select': '选择出装',
        'skill_info': '技能详情',
        'settings_or_pause': '设置或暂停',
        'other_in_match': '其他游戏内画面',
    },
    'post_match': {
        'victory_defeat_animation': '水晶爆炸或胜负动画',
        'result_page': '真正结算页面',
        'other_post': '其他赛后画面',
    },
    'transition': {
        'switch_app': '切换 APP',
        'minimized': '游戏最小化',
        'reconnect': '重连',
        'black_or_loading_trans': '黑屏或加载',
        'exiting_match': '正在退出对局',
        'other_transition': '其他转场',
    },
}

# 旧枚举 → 新枚举迁移(旧标注数据一次性转换)
GAME_CONTEXT_MIGRATION = {
    'in_match': 'in_match',
    'out_of_match': 'out_of_match',
    'transition': 'transition',
    'unknown': 'unknown',
}
SCREEN_TYPE_MIGRATION = {
    'main_lobby': 'main_lobby',
    'hero_roster': 'hero_list',
    'global_store': 'out_store',
    'matchmaking': 'matchmaking',
    'hero_select': 'hero_select_bp',
    'loading': 'load_before_match',
    'settings': 'out_settings',
    'other_out_of_match': 'other_out',
    'gameplay': 'gameplay',
    'scoreboard': 'scoreboard',
    'death_scoreboard': 'death_scoreboard',
    'ingame_shop': 'ingame_shop',
    'skill_info': 'skill_info',
    'settings_or_pause': 'settings_or_pause',
    'victory_defeat_animation': 'victory_defeat_animation',
    'result_page': 'result_page',
    'spectate_or_replay_hud': 'other_in_match',
    'other_in_match': 'other_in_match',
    'other_post': 'other_post',
    'switch_app': 'switch_app',
    'minimized': 'minimized',
    'reconnect': 'reconnect',
    'black_or_loading_trans': 'black_or_loading_trans',
    'exiting_match': 'exiting_match',
    'other_transition': 'other_transition',
}

# 标注状态(草稿与完成分离)
ANNOTATION_STATUSES = {
    'draft': '草稿',
    'complete': '已完成',
    'needs_review': '待复核',
    'ignored': '已忽略',
}

# 结算检测任务:积分板/死亡积分板 = 重点负样本
SCOREBOARD_HARD_NEGATIVE = {'scoreboard', 'death_scoreboard'}

# 边界框类型(画布上可绘制的框)
BOX_TYPES = {
    'viewport': '游戏窗口范围(可选)',
    'result_panel': '结算面板(结算页必画)',
    'scoreboard_panel': '积分板面板(积分板可画)',
    'shop_panel': '商店/装备面板(商店界面可画)',
    'equipment_panel': '出装面板(选择出装界面可画)',
    'talent_panel': '天赋选择面板(天赋选择界面可画)',
}

# 关键边界说明(界面常驻提示)
SCOREBOARD_VS_RESULT_HINT = (
    '积分板 vs 结算界面:比赛仍在进行、临时打开的玩家数据面板一律是 scoreboard / '
    'death_scoreboard;比赛结束后出现、包含最终胜负和整局数据的面板才是 result_page。'
    '不能用右上角 REPLAY 字样或 OCR 结果反推。'
)

# 游戏模式(仅在有足够证据时填写;不得乱猜)
GAME_MODES = {
    '3v3': '3V3',
    '5v5': '5V5',
    'aram': '大乱斗(随机英雄)',
    'blitz': '闪电战(可选英雄)',
    'unknown': '未知',
}

# 真人/人机/练习(独立于地图模式,默认 unknown)
MATCH_KINDS = {
    'pvp': '真人对战',
    'bot': '人机(Alpha/Beta Bot 等)',
    'practice': '练习模式',
    'unknown': '未知',
}

# 视角
VIEW_CONTEXTS = {
    'played': '主播本人操作',
    'spectated': '观战',
    'replay': '回放',
    'unknown': '未知',
}

# 画质异常(可多选;正常画面默认全不选)
QUALITY_FLAGS = [
    ('blurred', '模糊'),
    ('low_bitrate', '低码率'),
    ('occluded', '被遮挡'),
    ('translucent', '半透明'),
    ('color_shift', '偏色'),
    ('torn_or_corrupted', '撕裂/损坏'),
]

# 黑边(程序自动建议,人工修正)
BLACK_BARS = ['none', 'top', 'bottom', 'left', 'right', 'multiple']

OCR_USABLE = {'yes': '可用', 'no': '不可用', 'unknown': '未知'}

# 结算框评估(仅 result_page 时填写)
RESULT_CLARITY = {'clear': '清晰', 'translucent': '半透明', 'unknown': '不确定'}
PANEL_RENDER_STATES = {
    'clear': '正常显示',
    'translucent': '半透明／过渡中',
    'unknown': '看不清',
}
HERO_SELECT_VISIBILITY = {
    'clear': '清晰',
    'occluded': '有遮挡但仍能确认',
    'unknown': '历史数据未记录',
}
RESULT_OCCLUSION = {'none': '无遮挡', 'occluded': '有遮挡', 'unknown': '不确定'}
# 遮挡物类型(可多选,有遮挡时填写)
OCCLUDER_TYPES = [
    ('danmaku', '弹幕'),
    ('gift_banner', '礼物横幅'),
    ('system_device_ui', '系统/设备 UI'),
    ('game_ui', '游戏内其他 UI'),
    ('platform_ui', '直播平台 UI'),
    ('ad_watermark', '广告/水印贴片'),
    ('other', '其他'),
]

# ---------- 抽帧配方 ----------

STRATEGIES = {
    'uniform_every_n_seconds': '全片每 N 秒抽一帧(负样本/背景,推荐)',
    'existing_model_hits': '旧模型命中+邻近帧(结算候选)',
    'dense_around_candidate': '候选前后密集抽帧',
    'uniform_random': '随机背景负样本',
    'manual_timestamps': '手动时间点',
    'dense_interval': '指定区间密集抽帧',
    'transition_windows': '画面突变窗口',
}

# 模型预打分只读取 Vision Lab 自己的模型工作目录。
MODEL_PATH = Path(
    os.environ.get('VISION_LAB_RESULT_MODEL', str(MODELS_DIR / 'result-panel.onnx'))
).expanduser()
MODEL_INPUT_SIZE = 640
MODEL_CONF_THRESHOLD = 0.55

# 抽帧默认
COARSE_SAMPLE_SECONDS = 2  # 模型粗扫间隔(秒)
DENSE_FPS = 4  # 密集抽帧帧率
DENSE_WINDOW_SECONDS = 5  # 候选前后窗口(秒)
DEFAULT_JPEG_QUALITY = 5  # mjpeg 质量(2-31,越小越好;原始分辨率保存)
MAX_FRAMES_PER_VIDEO = 5000  # 单个视频保护上限
