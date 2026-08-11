# 代码接入与模型包

## 目标

模型只在 MacBook Pro 的 Analysis Worker 中执行。Vision Lab 负责生产和验证模型包，NAS 上的 BLREC Server 只协调任务、保存结果和记录使用了哪个模型包。

目标运行链路是：

```text
录像
  │
  ├─ 每约 60 秒：关键帧粗扫 → 二态时间线
  │                    └─ 状态变化区间：每 5 秒局部复核
  │
  ├─ 开局候选窗口：英雄选择分类 → 开始锚点 + 初始模式证据
  │
  ├─ 已进入对局：保存 match_flow 区间
  │
  ├─ match_flow → 游戏外：产生结束边界
  │
  ├─ 从结束边界向前：结算面板检测
  │                         │
  │                         └─ 确认后才做 OCR、英雄和版式识别
  │
  └─ 模式仍有歧义：对局模式分类 + 旧天赋／光栅证据
                            │
                            └─ 证据融合 → 3V3／ARAM／5V5／unknown

已确认关键帧（按局缓存，不逐帧运行）
  ├─ 英雄头像位置检测 → 一次批量识别 6／10 个英雄
  ├─ 积分板／结算全图 → 主播本人位置识别
  └─ 位置与身份合并 → HUD／积分板／结算交叉核对
```

## 当前代码接入点

| 位置 | 当前职责 |
|---|---|
| `apps/analysis-worker/src/blrec_analysis_worker/resources.py` | 仅为未配置模型包时的旧模型兼容路径提供资源 |
| `apps/analysis-worker/src/blrec_analysis_worker/model_package.py` | 校验版本包、复现训练预处理并加载七个新模型 |
| `apps/analysis-worker/src/blrec_analysis_worker/cli.py` | 选择 ONNX Runtime provider，加载模型包并创建分析器 |
| `apps/analysis-worker/src/blrec_analysis_worker/remote.py` | 领取任务、下载视频、回传结果、分析摘要和训练候选 |
| `src/blrec/vainglory/stage_classifier.py` | `multi-v2` 预处理、推理、平滑和窗口推断 |
| `src/blrec/vainglory/result_detection.py` | 结算面板 ONNX 推理和坐标还原 |
| `src/blrec/vainglory/sampling.py` | 单遍关键帧抽取、真实 PTS、自适应补帧和结算窗口解码 |
| `src/blrec/vainglory/analyzer.py` | 时间线、结算窗口、OCR、英雄识别和候选采集的级联流程 |
| `src/blrec/vainglory/analysis_protocol.py` | Worker 与 Server 之间的结果／训练候选编码校验 |
| `apps/vision-lab/labeler/export.py` | 冻结各任务的不可变数据集快照 |
| `apps/vision-lab/labeler/training.py` | 定义当前训练任务、冻结累计快照、启动训练并记录版本 |
| `apps/vision-lab/labeler/model_testing.py` | 按 run 的不可变快照验收 ONNX，并组装带哈希和数据锁的模型包 |

未指定 `--model-package` 时，Analysis Worker 仍可临时回退到散装旧模型：

```text
apps/analysis-worker/src/blrec_analysis_worker/models/
├── multi-v2.onnx
├── result-detector-v1.onnx
└── result-panel.onnx
```

这条路径只用于升级兼容。正式新管线必须加载 Vision Lab 生成的不可变模型包；
Worker 会在领取任务前校验包状态、七个角色、预处理契约、类别顺序和文件哈希。
模型包由命令行 `--model-package` 或环境变量 `BLREC_VISION_MODEL_PACKAGE` 指定。

## 时间线状态机

模型判断单帧，状态机判断一局。建议把状态机的核心对象明确分开：

- `ScreenObservation`：时间点、画面状态、置信度、模型包版本。
- `StartAnchor`：BP 时间、BP 类型、模式证据，以及后续是否真正进入对局。
- `MatchFlow`：已经由连续 `in_match`／`talent_select`／`post_match` 观测确认的对局区间。
- `EndBoundary`：从对局流转到稳定游戏外，或被下一段明确开局截断的时间点。
- `ResultCandidate`：结算检测、关键界面分类和 OCR 的逐层结果。
- `ModeEvidence`：证据来源、模式、时间、置信度和是否为强证据。

这些可以先作为 `analyzer.py` 内的数据类实现，不需要为了第一轮接入立即拆成复杂框架。

### 开始点处理

1. 按时间向后扫描状态观测。
2. 看到 `pre_match` 时建立候选区间，不立即创建对局。
3. 在候选区间附近运行 BP 分类，可能得到多个 `StartAnchor`。
4. 只有后面出现稳定 `in_match`，该候选才变成有效开局。
5. 如果选英雄后退出、再次选英雄并最终进入游戏，选择与这段 `in_match` 最近且位于它之前的有效 BP 锚点。

因此“发现 BP”与“确认开局”是两个动作。被取消的 BP 留作日志和训练候选，但不创建空对局。

### 结束点处理

1. `in_match`、`talent_select` 和 `post_match` 统一属于当前 `match_flow`。
2. 当它转为持续的 `out_of_match` 时，建立结束边界。
3. 胜利／失败动画不建立边界，也不规定结算页在它之前还是之后。
4. 对结束边界选择其前方最近的、已经确认进入对局的开始锚点。
5. 从边界向前搜索真正结算页；后面下一局的 BP 只能作为前一局搜索的上界，不能被错误配成前一局的开始点。

当前回扫策略：

- 先对时间线推断出的结束候选窗口以 4 FPS 扫描。
- 没找到时，不直接放弃，也不跳到下一局锚点；从结束边界开始按 45 秒一块向本局开始处倒序扫描。
- 某一块出现结算命中后立即停止继续向前解码，避免“没有结算页”时把整局都按 4 FPS 扫完。
- 找到经过检测、关键界面分类和 OCR 验证的结算页后立即停止。
- 扩展到本局开始仍没有结算页，则记录“有对局流但无可见结算”，不伪造结算结果。

例如：100 秒出现一次后来取消的 BP，180 秒再次 BP，220 秒进入对局，600 秒出现结算页，800 秒开始下一局。前一局应使用 180 秒的有效锚点；800 秒只限制前一局搜索不能越过下一局，不能被当成前一局开始。

### 对临时退出和回来的处理

切应用、最小化、黑屏和重连先进入 `transition`，短暂出现时不立刻结束对局。只有后面稳定回到 `out_of_match` 才形成结束边界；若重新回到 `in_match`，则继续原来的 `match_flow`。

具体持续时间阈值应成为状态机配置并写入管线版本，不能隐藏在模型阈值中。

## 一级取帧与真实时间戳

一级时间线默认每约 60 秒取一个关键帧粗扫点；相邻粗扫点状态不同时，只在两点
之间每 5 秒补扫。它不等于从视频开头连续解码，也不等于只保留任意关键帧：

1. FFmpeg 在一遍读取中跳过普通帧，用固定长度的时间桶表达式选取关键帧；
2. 同一遍读取通过 `showinfo` 取得选中关键帧的真实 PTS，不需要先做一遍
   FFprobe 全关键帧索引，命令长度也不会随视频时长增长；
3. 相邻关键帧间隔超过容许值时，只在缺口目标点做 seek 后的短解码补帧；
4. 每个观察同时保存目标时间、真实源 PTS、来源（`keyframe`／`seek_fill`）和
   模型包 ID；后续精扫一律使用真实源 PTS；
5. 如果某个粗扫时间桶没有可复用的关键帧，仅该目标点进入 `seek_fill`，
   不把整段视频回退成普通帧连续解码。

禁止用“输出第几帧 × 固定间隔”合成源时间戳。GOP 间隔并不固定，这种做法会让模型
画面与时间线错位，并把结算精扫窗口定位到错误位置。

Worker 需要记录关键帧数量、补帧数量、一级解码耗时以及相对视频时长的处理速度。
取帧策略是否更快以目标 MacBook Pro 的整段录像基准为准，不凭单帧推理速度判断。
同一台 Mac 的 334.3 分钟固定录像基准中，60 秒粗扫加 5 秒边界复核约 97.4 秒，
命中 18/18 个实际展示的结算且 0 多报；旧 5 秒级联扫描约 700.1 秒。

## 按需推理与性能

增加模型数量不等于每帧成本相加。以一小时录像为例：

- 对局流程模型每 60 秒粗扫一次，一小时约 60 个固定点；只在状态边界区间增加
  5 秒局部复核。
- 英雄选择分类只在 `match_flow` 认为属于对局流程的粗扫帧运行；识别为选英雄时直接给出模式证据。
- 对局模式分类只在属于对局流程、且不是英雄选择的粗扫帧运行。
- 640×640 结算检测只在结束候选窗口和必要的分块回扫中运行，不再跟随每个 5 秒粗扫帧运行。
- 光栅检测只处理无法由 BP／天赋判断的少数对局，优先复用前两分钟约每 5 秒一张的粗扫帧。
- OCR、英雄识别和版式分析只处理已通过视觉模型筛选的结算候选。
- 头像位置检测只在一局首次出现稳定 HUD／积分板或结算页时运行；同局位置和阵容缓存复用。
- 主播本人位置分类只在稳定积分板或结算图上按局运行一至数次；HUD 优先按固定阵容规则计算。
- 6／10 个头像必须一次批量送入身份分类模型，禁止串行加载或调用 6／10 次模型。

训练图片增加不会改变固定网络结构的参数量，也不会让同一模型的单次推理变慢。
额外成本来自新增模型的调用次数，因此英雄模型必须位于条件分支中：默认一局只
运行一至数次，低置信或 HUD／积分板／结算结果冲突时才复查。第一版上线前需要
在目标 MacBook Pro 实测整段视频耗时，不能用开发机上的单帧时间代替管线验收。

每次模型发布都应在 MacBook Pro 记录：

- 各模型调用次数；
- 各阶段总耗时和单次推理 P50／P95；
- 每小时视频的总分析分钟数；
- 候选数、OCR 次数和最终对局数；
- CoreML／CPU provider 及是否发生回退。

性能验收看完整管线总耗时，不用把“某个模型单帧几十毫秒”直接乘以视频总帧数。

## 推理接口组织

第一轮应采用最小改动：保留 `VaingloryVideoAnalyzer` 作为编排者，为每种模型增加小而明确的接口，不在接入模型时顺便重写整套分析器。

建议接口职责如下：

```text
MatchFlowClassifier.classify(frame) -> MatchFlowPrediction
HeroSelectClassifier.classify(frame) -> HeroSelectPrediction
MatchModeClassifier.classify(frame) -> MatchModePrediction
ResultPanelDetector.detect(frame) -> ResultPanelDetection | None
PlayerPositionClassifier.classify(frame) -> PlayerPositionPrediction
```

每个加载器只负责：

1. 从模型包清单读取本角色的配置；
2. 校验 ONNX SHA-256 和标签顺序；
3. 执行清单规定的预处理；
4. 把原始输出转换成有类型的预测结果；
5. 将检测坐标还原到原图。

状态平滑、锚点配对、证据融合和回扫属于管线逻辑，不应复制到各模型加载器中。

### 代码位置的两步走

短期为降低风险，可以继续在 `src/blrec/vainglory/` 中增加模型接口和状态机，并由 Analysis Worker 调用。行为稳定后，再把只属于推理的实现移到：

```text
apps/analysis-worker/src/blrec_analysis_worker/vainglory/
├── model_package.py
├── match_flow.py
├── hero_select.py
├── match_mode.py
├── result_panel.py
├── timeline.py
└── pipeline.py
```

Worker／Server 共用的协议 DTO 和业务结果格式仍留在共享包。迁移推理代码不是第一批模型上线的前置条件，避免把模型接入变成一次大范围重构。

## 模型包目录

模型包解压目录示例：

```text
apps/analysis-worker/src/blrec_analysis_worker/model_packages/
└── vainglory/
    └── vg-vision-2026.08.09.1/
        ├── manifest.json
        ├── dataset-lock.json
        ├── metrics.json
        └── models/
            ├── match_flow.onnx
            ├── hero_select.onnx
            ├── match_mode.onnx
            ├── result_panel.onnx
            ├── hero_avatar.onnx
            ├── hero_identity.onnx
            └── player_position.onnx
```

`vg-vision-2026.08.09.1` 只是示例。模型包 ID 一旦发布不可复用或覆盖；哪怕只替换其中一个 ONNX，也必须生成新包 ID。

模型包不是写死在 Worker wheel 中，而是作为经过验收的独立发布资产解压到 Worker
源码／虚拟环境之外。launchd 只引用当前完整包目录，因此模型更新无需把七个 ONNX
散落复制进安装包，也能以完整包为单位回滚。

## `manifest.json` 最低内容

示例只展示结构，实际 SHA-256、阈值和标签必须由冻结流程生成：

```json
{
  "schema_version": 2,
  "package_id": "vg-vision-2026.08.09.1",
  "pipeline_version": "timeline-v2",
  "status": "ready",
  "models": {
    "hero_select": {
      "file": "models/hero-select.onnx",
      "sha256": "<64 hex characters>",
      "kind": "classification",
      "input": {
        "width": 512,
        "height": 288,
        "color": "RGB",
        "resize": "aspect_fit_letterbox",
        "pad_value": 114,
        "preserve_full_image": true,
        "scale": "0_to_1"
      },
      "classes": ["not_select", "select_3v3", "select_aram", "select_5v5"],
      "dataset_version": "hero-select-classifier-vN",
      "training_run_id": "hero-select-<run-id>"
    }
  },
  "runtime": {
    "coarse_interval_ms": 60000,
    "maximum_keyframe_distance_ms": 5000,
    "result_scan_fps": 4,
    "thresholds": {
      "match_flow": 0.55,
      "hero_select": 0.55,
      "match_mode": 0.5,
      "result_panel": 0.55,
      "hero_avatar": 0.25,
      "hero_identity": 0.5,
      "player_position": 0.5
    }
  },
  "compatibility": {
    "analysis_protocol_version": 2,
    "product": "blrec-analysis-worker"
  }
}
```

关键规则：

- 运行时标签顺序只信清单，不能按目录名重新排序，也不能硬编码猜测。
- Vision Lab 训练生成的 `.json` 类别映射应在组包时转换并校验进清单。
- 每个模型都必须单独声明预处理，不能假设所有 YOLO 导出都相同。
- 新分类训练和 Vision Lab 验收共用 512×288 全图等比补边实现；Worker 加载器必须按清单复现同一确定性流程，并用同一张图片做张量级或输出级对照测试。
- 阈值属于模型包／管线版本；修改阈值也要留下可追溯版本。
- `dataset-lock.json` 保存每个训练快照 ID、样本清单哈希和切分信息。
- `metrics.json` 同时保存单帧指标、难例分组指标、完整录像回放结果和性能基线。

Worker 启动时应先校验整个包，再一次性创建所有 session。任何文件缺失、哈希不符、标签不匹配或 provider 无法加载时直接拒绝领取任务，不能悄悄混用旧模型和新模型。

## Worker 与 Server 协议

Worker 内部候选仍兼容五路旧任务输出：

- `screen_state`
- `bp_review`
- `key_screen_review`
- `result_detector`
- `mode_gate`

Server 最终把同图输出合并为 `unified_review` v3。本轮已经同时扩展：

1. `TrainingCandidateTask`、候选框和对应标签白名单；
2. `analysis_protocol.py` 的编码／解码校验；
3. BLREC Server 接收、限量和原子落盘逻辑；
4. Vision Lab 的 NAS 双向候选／复核同步器；
5. 一图四标签的统一复核页面；仅结算正样本保留一个完整面板框。

生产管线的分析完成 payload 已新增向后兼容的 `analysisSummary`，其中包含：

- `modelPackageId`、`pipeline`；
- 关键帧、补帧、时间线样本和结算精扫统计；
- 时间线分段、结算窗口、模式证据摘要和各阶段耗时；
- 各任务训练候选数量。

Server 将模型包 ID 与分析结果一起保存。以后发现某批结果有问题时，可以准确筛选出由哪个模型包产生，而不是根据大概部署日期猜测。

协议新增字段应先让 Server 兼容旧 Worker，再发布新 Worker；在全部 Worker 更新前，Server 不能要求旧版本必须发送新字段。

### 新管线的兼容字段

新 Worker 的 heartbeat 在旧字段之外增加可选信息：

- `model_package_id`；
- 时间线样本、关键帧和补帧数量；
- 已推断的对局段数、结算窗口数和当前精扫窗口；
- 当前候选数、已拒绝候选数和已识别对局数。

阶段依次为 `probing` → `timeline_scan` → `timeline_analysis` →
`result_scan` → `ocr_recognition` → `candidate_upload`。Server 同时继续接受旧
`coarse_scan`／`fine_scan`，管理端把两套状态映射为同一组用户可读阶段。

分析完成 payload 追加可选的 `analysisSummary`：模型包、管线版本、时间线分段、
取帧统计、各阶段耗时和候选统计。较大的逐帧时间线不写进任务数据库，只保留聚合
计数和分段。旧 Worker 不发送时，Server 按历史任务展示，不能报错或伪造版本。

### 统一预标建议

NAS 候选继续使用一图一份 `unified_review` v3。核心建议包含
`match_flow`、`hero_select`、`match_mode` 和 `result_panel`；英雄增强建议放在同一
sidecar 的 `model_outputs`／布局字段中，包括头像框、各框英雄 Top-K、推导出的
HUD／积分板／结算布局和本人位置。建议必须带 run／模型包版本和置信度。

Vision Lab 可以把建议写为 pending 英雄布局以便直接确认，但不得覆盖已经人工
确认的标签、框、英雄或本人位置。旧 BP、关键界面和综合候选导入后也进入同一
复核页，旧专项表只保留追溯和兼容，不再形成另一套真值保存流程。

## 从训练到部署

```text
人工确认数据
  → 冻结 dataset-vN
  → 生成 training run
  → 单帧指标验证
  → 固定完整录像回放
  → 组装并校验模型包
  → MacBook Pro 影子运行
  → 小范围启用新决策
  → 发布 Analysis Worker
```

### 影子运行

新模型先运行但不改变最终分析结果，只记录：

- 新旧状态时间线差异；
- 新增和丢失的开始／结束候选；
- 模式证据冲突；
- 结算页、计分板和 OCR 调用差异；
- 总耗时变化。

人工检查差异后，才让新管线参与最终写回。这样可以利用真实 Worker 工作量验证，而不立即污染业务统计。

### 部署哪些地方

| 变更内容 | Vision Lab | MacBook Pro Worker | NAS Server |
|---|---|---|---|
| 只新增标注或重新训练 | 本机更新 | 不需要 | 不需要 |
| 同协议替换模型包 | 可选，仅用于保留训练环境 | **需要** | 不需要 |
| 新增模型调用但结果协议不变 | 需要保留对应训练能力 | **需要** | 不需要 |
| 新增候选任务或 `modelPackageId` 等协议字段 | 需要 | **需要** | **需要先做向后兼容更新** |
| 新增生产数据库字段或管理页面 | 视需求 | 视协议而定 | **需要** |

Vision Lab 的“设为本机测试模型”只影响当前电脑，不会更新 NAS 或 MacBook Pro。BLREC Server 不应因为模型更新而加载 ONNX。

当前“模型验收”页不再要求先覆盖本机 `current` 模型。它直接运行所选 training run 的 ONNX 和该 run 绑定的快照：分类模型结构化展示标准答案、模型答案和各类别概率；检测模型同时显示人工框、预测框、置信度和框重合度。原始 JSON 仅放在折叠的技术详情中。

页面下方的“生成 Worker 发布候选包”要求 `match_flow`、`hero_select`、
`match_mode`、`result_detector`、`hero_avatar_detector`、`hero_identity` 和
`player_position` 七个 run 全部验收通过。生成的 ZIP 是经过哈希约束、可追溯的
部署候选，不会自动复制到 MacBook Pro，也不会重启 Worker；“生成候选包”不等于
已经上线。

### 回滚

- 回滚单位是完整模型包或对应 Analysis Worker 发布版本，不是手工复制某个旧 ONNX。
- 发布前保留当前包和 Worker 安装包。
- 新模型导致错误时，停止 Worker、切回已验证版本、重新启动；NAS 未完成任务会按现有心跳租约重新派发。
- 已由问题模型写回的结果通过 `modelPackageId` 定位，并单独创建重跑任务，不做无范围的全库重算。

## 必需验证

### 模型包测试

- 清单 schema、路径和 SHA-256 校验。
- 每个 ONNX 在 CPU 与目标 CoreML provider 上可加载。
- 每个分类模型用各类至少一张固定图片核对输出索引与标签。
- 检测框从 letterbox 坐标还原到原图后位置正确。
- wheel／安装包中包含完整模型包和 JSON 元数据。

### 管线测试

- 一次 BP 后退出、再次 BP 并成功进入游戏。
- 前一局结算页之后才出现下一局 BP，不能把下一局锚点配给前一局。
- 没有展示结算页时不伪造对局结果。
- 结算页关闭后再次出现胜负动画，不得改变已找到的结算点。
- 切应用后重新回到同一局，不得切成两局。
- 计分板不能通过最终结算验证。
- 3V3／大乱斗证据不足或冲突时输出 `unknown`。
- 窗口化电脑画面、非标准宽高比和遮挡画面不走固定比例裁剪。

### 完整录像回放

至少保留一组覆盖 3V3、大乱斗、5V5、退出重开、无结算、小窗遮挡和错误 OCR 历史案例的固定录像清单。每次模型包发布都生成可比较报告：对局数、开始点、结算点、模式、英雄、OCR、拒绝原因和耗时。
