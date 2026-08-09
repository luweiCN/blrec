# 代码接入与模型包

## 目标

模型只在 MacBook Pro 的 Analysis Worker 中执行。Vision Lab 负责生产和验证模型包，NAS 上的 BLREC Server 只协调任务、保存结果和记录使用了哪个模型包。

目标运行链路是：

```text
录像
  │
  ├─ 每约 5 秒：对局流程分类 → 二态时间线
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
```

## 当前代码接入点

| 位置 | 当前职责 |
|---|---|
| `apps/analysis-worker/src/blrec_analysis_worker/resources.py` | 返回 `multi-v2.onnx`、结算检测器和英雄参考图路径 |
| `apps/analysis-worker/src/blrec_analysis_worker/cli.py` | 选择 ONNX Runtime provider，加载模型并创建分析器 |
| `apps/analysis-worker/src/blrec_analysis_worker/remote.py` | 领取任务、下载视频、回传结果和训练候选 |
| `src/blrec/vainglory/stage_classifier.py` | `multi-v2` 预处理、推理、平滑和窗口推断 |
| `src/blrec/vainglory/result_detection.py` | 结算面板 ONNX 推理和坐标还原 |
| `src/blrec/vainglory/analyzer.py` | 粗扫、精扫、OCR、英雄识别和候选帧收集的级联流程 |
| `src/blrec/vainglory/analysis_protocol.py` | Worker 与 Server 之间的结果／训练候选编码校验 |
| `apps/vision-lab/labeler/export.py` | 冻结各任务的不可变数据集快照 |
| `apps/vision-lab/labeler/training.py` | 定义四个当前训练任务、冻结累计快照、启动训练并记录版本 |
| `apps/vision-lab/labeler/model_testing.py` | 按 run 的不可变快照验收 ONNX，并组装带哈希和数据锁的模型包 |

当前 Analysis Worker 安装包仍直接携带：

```text
apps/analysis-worker/src/blrec_analysis_worker/models/
├── multi-v2.onnx
├── result-detector-v1.onnx
└── result-panel.onnx
```

这能运行，但缺少统一清单，无法单靠文件回答标签顺序、预处理、阈值、训练数据版本和完整性。Vision Lab 现在已经能生成不可变模型包候选；Analysis Worker 的模型包加载器和新时间线管线仍未接入，因此当前生产环境仍使用上述散装旧模型。

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

推荐的回扫策略：

- 先在结束边界之前约 150 秒内以 4 FPS 扫描；通常能直接找到结算页。
- 没找到时，不直接放弃，也不跳到下一局锚点。把搜索区间按块向本局开始处扩展，旧区间先以约 1 FPS 粗扫，命中附近再以 4 FPS 精扫。
- 找到经过检测、关键界面分类和 OCR 验证的结算页后立即停止。
- 扩展到本局开始仍没有结算页，则记录“有对局流但无可见结算”，不伪造结算结果。

例如：100 秒出现一次后来取消的 BP，180 秒再次 BP，220 秒进入对局，600 秒出现结算页，800 秒开始下一局。前一局应使用 180 秒的有效锚点；800 秒只限制前一局搜索不能越过下一局，不能被当成前一局开始。

### 对临时退出和回来的处理

切应用、最小化、黑屏和重连先进入 `transition`，短暂出现时不立刻结束对局。只有后面稳定回到 `out_of_match` 才形成结束边界；若重新回到 `in_match`，则继续原来的 `match_flow`。

具体持续时间阈值应成为状态机配置并写入管线版本，不能隐藏在模型阈值中。

## 按需推理与性能

增加模型数量不等于每帧成本相加。以一小时录像为例：

- 画面状态每 5 秒一次，约 720 次 224×224 分类，是唯一固定成本。
- BP 分类只在每局开头的几秒至几十秒内运行，并在证据一致后提前停止。
- 关键界面分类和 640×640 结算检测只在结束候选窗口运行。
- 光栅检测只处理无法由 BP／天赋判断的少数对局，优先复用前两分钟约每 5 秒一张的粗扫帧。
- OCR、英雄识别和版式分析只处理已通过视觉模型筛选的结算候选。

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

目标目录示例：

```text
apps/analysis-worker/src/blrec_analysis_worker/model_packages/
└── vainglory/
    └── vg-vision-2026.08.09.1/
        ├── manifest.json
        ├── dataset-lock.json
        ├── metrics.json
        └── models/
            ├── match-flow.onnx
            ├── hero-select.onnx
            ├── match-mode.onnx
            └── result-panel.onnx
```

`vg-vision-2026.08.09.1` 只是示例。模型包 ID 一旦发布不可复用或覆盖；哪怕只替换其中一个 ONNX，也必须生成新包 ID。

Analysis Worker 的 `pyproject.toml` 目前只包含 `models/*.onnx`。迁移后要改为递归包含模型包的 JSON 和 ONNX，并增加构建产物测试，防止本地能运行但 wheel 漏模型。

## `manifest.json` 最低内容

示例只展示结构，实际 SHA-256、阈值和标签必须由冻结流程生成：

```json
{
  "schema_version": 1,
  "package_id": "vg-vision-2026.08.09.1",
  "pipeline_version": "timeline-v1",
  "models": {
    "hero_select": {
      "file": "models/hero-select.onnx",
      "sha256": "<64 hex characters>",
      "kind": "classification",
      "input": {
        "width": 224,
        "height": 224,
        "color": "RGB",
        "resize": "letterbox",
        "pad_value": 114,
        "scale": "0_to_1"
      },
      "classes": ["not_select", "select_3v3", "select_aram", "select_5v5"],
      "thresholds": {
        "minimum_confidence": 0.8,
        "minimum_consistent_frames": 2
      },
      "dataset_version": "hero-select-classifier-vN",
      "training_run_id": "hero-select-<run-id>"
    }
  },
  "compatibility": {
    "minimum_worker_version": "<version>",
    "analysis_protocol_version": 1
  }
}
```

关键规则：

- 运行时标签顺序只信清单，不能按目录名重新排序，也不能硬编码猜测。
- Vision Lab 训练生成的 `.json` 类别映射应在组包时转换并校验进清单。
- 每个模型都必须单独声明预处理，不能假设所有 YOLO 导出都相同。
- 当前分类训练依赖 Ultralytics 默认图像变换，Worker 端预处理又由各加载器自行实现；首次发布新分类模型前必须显式统一两端流程，并用同一张图片做张量级或输出级对照测试。
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

生产管线真正切换模型包时，分析完成 payload 还应新增向后兼容字段：

- `modelPackageId`
- `pipelineVersion`
- 可选的模式证据摘要和各模型耗时统计

Server 将模型包 ID 与分析结果一起保存。以后发现某批结果有问题时，可以准确筛选出由哪个模型包产生，而不是根据大概部署日期猜测。

协议新增字段应先让 Server 兼容旧 Worker，再发布新 Worker；在全部 Worker 更新前，Server 不能要求旧版本必须发送新字段。

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

当前模型测试页已经不再要求先覆盖本机 `current` 模型。它直接运行所选 training run 的 ONNX 和该 run 绑定的快照；验收通过后可生成 ZIP 模型包。这个 ZIP 是可追溯的部署候选，不会自动复制到 MacBook Pro，也不会重启 Worker。等模型包加载器和影子管线完成后，再增加带目标核对、备份、安装校验和回滚信息的部署动作，不能把“下载 ZIP”误写成已经上线。

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
