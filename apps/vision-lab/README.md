# 虚荣视觉标注工作台(Vainglory Vision Lab)

《虚荣》(Vainglory)所有视觉模型共用的数据工作台:从 NAS 录制视频抽帧 → 事件分组 →
分层人工标注 → 导出不可变数据集版本 → 本机训练与模型验证。
当前统一训练七个目标：四个核心任务（是否处于对局流程、英雄选择及其模式、
对局画面模式、结算面板检测）和三个按需运行的英雄增强任务（英雄头像位置、
英雄头像身份、主播本人位置）。旧 BP、光栅、关键界面和通用分层标注完整保留
并迁移复用。

## 核心原则

1. **一份共享数据,多种训练目标**:视频、原始帧、事件、基础标注全局共享;
   训练目标(结算检测/游戏状态/模式/窗口/同局判断)只决定展示优先级与导出格式。
2. **原始数据永久保留**:抽帧保存**原始分辨率 + 真实 PTS**;缩略图只是浏览派生物;
   训练图(如 640 letterbox)在导出时生成。
3. **以事件为单位**:结算页持续 12 秒被抽成 48 帧,它们属于同一个事件。
   工具自动按"感知哈希近似 + 时间接近"聚类,用户确认后从事件中挑 3~6 张代表帧。
4. **层级条件式标注**:顶层"是否虚荣画面"→"游戏内/外"→"具体界面"→ 辅助属性,
   界面只显示当前有意义的字段,不要求填无关选项。
5. **导出不可变版本**:每次导出/训练都生成新的 `*-vN`,记录筛选条件、数量、分布、
   来源与 git commit,禁止覆盖;切分按**整段视频**划分,防止同视频/同事件跨集合泄漏。
6. **模型预标、人工裁决**:worker 保存高价值候选帧并给出建议标签;
   建议不直接进入训练集，已经人工确认的结果也不会被后续预标覆盖。

## 目录结构

```
apps/vision-lab/
├── labeler/
│   ├── config.py       # 分层标签体系、抽帧配方、NAS/路径常量
│   ├── db.py           # SQLite:视频/帧/事件/标注/框/配对/预测/任务/版本/审计
│   ├── nas.py          # NAS SSH(SSH_ASKPASS + sudo stdin)+ 容器 ffmpeg 真实 PTS 抽帧
│   ├── extract.py      # 6 种抽帧配方、sha256/phash、模型预打分
│   ├── events.py       # 事件自动聚类(phash+时间)
│   ├── stats.py        # 数据检查统计
│   ├── export.py       # JSONL/YOLO/COCO 导出 + 不可变版本 + 按视频切分
│   ├── worker_candidates.py # 同步 worker 上传的模型预标候选帧
│   ├── training.py     # 训练任务、进度、历史版本与本机测试发布
│   ├── training_runner.py # Ultralytics 训练子进程
│   ├── server.py       # FastAPI(端口 8800)
│   └── static/         # 工作台前端(深色主题,原生 JS)
├── train.py            # 训练画面分类模型(ultralytics;正式训练在标注完成后)
├── pyproject.toml      # 独立 Python 包、依赖和命令入口
├── requirements.txt    # 运行依赖
├── requirements-train.txt
└── data/               # 外置工作数据(gitignore):库、帧、数据集、模型和训练产物
```

## 安装与启动

```bash
cd apps/vision-lab
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export SYNO_ADMIN_USERNAME=你的群晖用户名
export SYNO_ADMIN_PASSWORD=你的群晖密码
./start.sh
# 打开 http://127.0.0.1:8800
```

默认工作目录是源码目录下的 `data/`；安装为 Python 包后默认使用
`~/.local/share/blrec-vision-lab`。也可以用 `VISION_LAB_DATA_DIR` 明确指定，
因此数据、权重和训练产物不会进入发布包。

## NAS 导入

- 数据源页点"同步 NAS 视频清单":SSH 扫描 `192.168.50.24:/volume1/docker/blrec-next/rec`
  (只读,绝不修改/删除 NAS 文件),支持按主播/房间号过滤。
- 凭据只从环境变量读取;ssh 认证走 `SSH_ASKPASS`,sudo 密码走 ssh stdin 管道,
  不出现在命令行、不落盘、不写入库/日志/前端。

## 新模型统一打标与 Worker 候选闭环

MacBook Pro 上的 Analysis Worker 只复用正常分析已经解码的帧，不为收集数据
另扫一遍录像。旧 5 秒一帧模型、开局候选和结算检测结果会合并成“一图多预标”
候选；同一图片按 SHA-256 只保存一份。结算检测同时保留高置信代表帧和低置信
边界帧。候选图片与模型建议由 BLREC Server 写入 NAS 的
`/volume1/docker/blrec-next/vision-data/candidates`；Server 只存文件，
不运行 ONNX。

打开“新模型统一打标”页点“同步 NAS（Worker＋历史结算图）”会：

1. 下载本机没有的候选图片和预标；
2. 导入 BLREC 已识别对局保存的历史结算截图；
3. 拉取 NAS 上已有的人工复核结果，便于换电脑审查；
4. 回传本机已确认或已跳过的结果。

每张图一次确认对局流程、对局模式、英雄选择和结算面板四项。只有真正结算正样本
需要画一个完整大框；积分板、商店和黄色光栅旧框会保留，但不要求继续绘制。
图片已禁止浏览器拖动。模型建议不能直接进入训练集；`unreadable` 会按任务分别
排除。本地未回传结果遇到远端修改时只标冲突，不会被静默覆盖。

## 抽帧策略(6 种配方)

| 配方 | 说明 |
|---|---|
| **uniform_every_n_seconds** | 全片每 N 秒抽 1 帧原始分辨率(默认 5 秒)。不依赖旧模型,最可靠;**推荐用于负样本/背景采集** |
| existing_model_hits | 旧模型全片粗扫(2s/帧小图)命中结算面板,命中前后 ±5s 按 4fps 密集抽原图(依赖旧模型,可能不准) |
| dense_around_candidate | 同左,但候选点由 params.candidates 提供 |
| uniform_random | 粗扫时间点中随机选 N 个,每点抽 1 帧原图 |
| manual_timestamps | 手动时间点(ms),每点抽 1 帧原图 |
| dense_interval | 指定起止区间按帧率密集抽原图 |
| transition_windows | 粗扫帧感知哈希突变处,前后 ±3s 密集抽帧 |

**推荐工作流(不依赖旧模型)**:负样本用 `uniform_every_n_seconds`(间隔 5 秒)抽全片;
结算正样本从 NAS 已识别结果(如 `vainglory-result-frames` 目录)导入,后续版本提供导入入口。

所有帧保存**真实 PTS(showinfo 解析,毫秒)**、原始分辨率、SHA-256、感知哈希、
来源策略与模型置信度;内容重复帧(sha256)自动去重。**不要只抽模型疑似帧**,
必须混入 uniform_random 负样本,否则评估会严重偏差。

## 标注规则(分层)

1. **是否为虚荣画面**:vainglory / not_vainglory / uncertain(非虚荣可附原因)。
2. **游戏内/外**:in_match / out_of_match / transition / unknown。
3. **具体界面**(按上下文显示):
   - 游戏内:gameplay / scoreboard(对局进行中的积分板)/ death_scoreboard /
     ingame_shop / skill_info / settings_or_pause / victory_defeat_animation /
     **result_page(赛后结算界面)** / spectate_or_replay_hud / other_in_match
   - 游戏外:main_lobby / hero_roster / global_store / matchmaking / hero_select /
     loading / settings / other_out_of_match
   - **积分板 vs 结算界面**:比赛仍在进行、临时打开的玩家数据面板 = scoreboard;
     比赛结束、含最终胜负和整局数据的面板 = result_page。不能用 REPLAY 字样
     或 OCR 结果反推(界面常驻提示)。
   - **接受/拒绝匹配不是 BP**:标为 `match_confirm`;训练 BP 分类器时它只是
     `not_bp` 负样本，绝不作为 3V3/5V5/大乱斗 BP 证据。
4. **辅助属性**(仅游戏内):game_mode(3v3/5v5/**aram 大乱斗**/**blitz 闪电战**/
   unknown,无证据必须 unknown)、match_kind(pvp/bot/practice,默认 unknown)、
   view_context(played/spectated/replay)、black_bars(程序建议人工修正)、
   画质异常多选(模糊/低码率/遮挡/半透明/偏色/撕裂,正常不选)、
   ocr_usable(仅 result_page 辅助)。
5. **边界框**(原图归一化坐标):viewport_bbox(游戏画面范围,蓝)、
   result_panel_bbox(结算面板,绿,result_page 必填);支持拖拽绘制/移动。
6. **代表帧**:每个事件确认后,选 3~6 张有差异的代表帧(淡入/清晰/遮挡/点击后/消失)。

## 快捷键

| 键 | 功能 | 键 | 功能 |
|---|---|---|---|
| R | 结算页(result_page) | N | 非结算(游戏画面) |
| U | 不确定 | V | 虚荣画面 |
| G | 游戏画面 | S | 积分板 |
| Space | 播放/暂停事件 | ← / → | 逐帧 |
| Shift+←/→ | 上一/下一事件 | Enter | 下一项 |
| Cmd/Ctrl+Z | 撤销 | F | 适应画布 |

备注/搜索框聚焦时快捷键不触发。所有标注**自动保存**(顶部显示状态),关闭页面后可断点继续。

## 3V3 / 大乱斗光栅专项

打开 `http://127.0.0.1:8800/?task=mode_gate`，左侧选择“光栅专项”。专项只显示
当前轮次挑选的视频，并从建议位置或上次位置继续：

- 切换到每一帧后会自动进入画框状态：大乱斗候选默认画光栅，3V3 候选默认画
  开放入口，不需要每张都先点一次类型按钮。
- 大乱斗画面看到黄色光栅：选“有黄色光栅”，把画面里所有清楚可见的光栅
  分别圈出来；一个框只包一处光栅。
- 3V3 画面看到同一地图入口且确定没有光栅：选“开放入口”，圈住光栅本来会
  横着挡住的位置，不要圈整条路或任意空地。
- 没看到光栅，也没有清楚的开放入口可标：选“没有可标记位置，跳过这张”。
  该帧会记为无证据，不作为 3V3 负样本。

快捷键为 `B`（光栅）、`O`（开放入口）、`N`（无证据并下一帧）、
`Esc`（结束连续画框）、`Enter`（下一帧）。已有框可以拖动，右上角 `×` 只删除
当前框。专项标签使用独立数据表，不会改写通用 `annotations`、
`boxes` 或帧的 `labeled` 状态。

## 导出

数据检查页查看统计(事件数/正负样本/模式分布/质量覆盖/冲突缺失);
数据集版本页点"导出 result-detector 数据集":

- `data/datasets/result-detector-vN/`:images(按 train/val/test)、labels(YOLO txt)、
  `coco_annotations.json`、`data.yaml`、`samples.jsonl`(权威清单,含全部字段)。
- 正样本 = screen_type=result_page 且有 result_panel_bbox;负样本 = 其他已标帧
  (含积分板 hard negative 与随机负样本)。
- 切分以整段视频为单位(≈8:1:1),同视频/同事件的帧只属于一个集合。
- 版本不可变,禁止覆盖;记录筛选条件、数量、分布、路径与 git commit。

“训练与模型”页当前生成：

- `match-flow-classifier-vN`:对局流程中 / 非对局画面二分类。
- `hero-select-classifier-vN`:非英雄选择 / 3V3 / 大乱斗 / 5V5 英雄选择。
- `match-mode-classifier-vN`:3V3 / 大乱斗 / 5V5 对局画面分类。
- `result-detector-vN`:结算面板框检测;没有框的 `result_page` 不会被误当成负样本。

旧 `bp-classifier-*`、`key-screen-classifier-*`、`mode-gate-detector-*` 和
`screen-state-*` 快照与训练记录仍可追溯，但不再显示为新一轮主动训练任务。

## 数据位置与备份

- 工作库:`data/lab.db`(交互式);权威导出:`data/datasets/*/samples.jsonl`。
- 基础权重:`data/models/base/`;已训练模型:`data/models/`。
- 标注工作帧:`data/frames/<sha256>.jpg`;缩略图:`data/thumbs/`。这里同时包含临时候选和已确认但尚未冻结的数据，不能直接按目录批量删除。
- 永久训练资产:`data/datasets/<version>/`。每个版本都自包含 train／val／test 图片、标签和 `samples.jsonl`，不依赖 `data/frames`、NAS 候选目录或原视频。
- 备份:直接复制 `data/` 目录即可(帧与库一体);NAS 文件为只读源,不需要备份。
- 重装/迁移:保留 `data/` 即保留全部标注。

只有旧模型建议、未经人工确认的图片属于可清理候选；人工确认但尚未冻结的图片必须继续保留。`dataset_versions` 表示已经冻结，`training_runs.dataset_version_id` 表示该快照已经被某次训练使用。验证集和测试集也属于永久资产。当前没有自动清理入口，后续实现时必须同时检查人工确认状态和所有快照 SHA-256，不能只看候选创建时间。

## 训练与模型版本

首次使用前执行 `.venv/bin/pip install -r requirements-train.txt`。
打开“训练与模型”页，每个任务会显示当前可用数量、缺少条件和建议量。
点“用当前数据开始训练”时:

1. 先冻结当前数据为新的不可变快照。
2. 后台串行训练，页面显示 epoch、百分比、指标和日志;其间可以继续打标。
3. 训练成功后保留 ONNX、数据快照、配置和指标。
4. 后续增加标注再点一次，会生成新快照和新模型，不会改动旧版本。

快照会记录所有可用数据与取样规则。为避免几千张普通画面淹没少数的结算页/
计分板，三分类快照会优先保留人工确认和赛后易混淆负样本，再按固定规则抽取
其他画面;结算检测试训最多取 1500 张负样本并优先保留计分板 hard negative。

训练优先使用 Apple MPS，不可用时退回 CPU。训练成功后在“模型测试”页选择该
run；页面直接读取 run 绑定的不可变快照和 ONNX，不需要先覆盖本机测试模型。
人工记录通过／不通过后，只有通过的 run 才能组装模型包。四个核心角色不齐，
或已选择模型的固定测试集缺少必要类别时，只能生成 `incomplete` 研究包；不能
作为生产部署包。三个英雄增强模型可以在各自验收通过后一起加入模型包。

模型包 ZIP 不会自动修改 NAS 或 MacBook Pro Worker。当前 Worker 的新模型包
加载器和影子管线尚未上线；完成后部署也只需要更新 MacBook Pro Worker，NAS
Server 仍不加载视觉模型。旧的 `publish-local` API 暂时保留兼容，但不是新的验收
与生产发布流程。

## 独立打包与发布

```bash
cd apps/vision-lab
.venv/bin/python -m build
```

产物仅包含工作台代码和静态页面，不包含 `data/`、视频、图片、数据集或模型。
Vision Lab 使用自己的 `vision-lab-v<版本>` 标签和 GitHub Release，不触发服务端
镜像、Dashboard 或插件发布。

第一阶段目标量:30~50 个独立结算事件、每事件 3~5 张代表正样本(约 120~250 张)、
800~1500 张负样本(重点含积分板);训练后通过主动学习追加数据。

## 安全与边界

- NAS 密码/Cookie/token 不输出、不记录、不提交。
- NAS 视频只读;不修改生产 Compose/数据库/线上分析结果。
- 不新增生产网络连接;大型图片、SQLite、虚拟环境、权重全部 gitignore。
- 改动仅限标注工具及其文档,不重构 BLREC 录制/上传/发布逻辑。
