# BLREC Analysis Worker

Analysis Worker 在 Mac 上领取 BLREC Server 的对局分析任务，并使用通过验收的
版本化视觉模型包、OpenCV、ONNX Runtime 和 RapidOCR 完成分析。它不进入 NAS
的 Server 镜像，也不直接访问生产数据库。

正常分析还会从已经解码的帧中限量挑选对局流程、英雄选择、对局模式、结算检测
和英雄阵容样本。结算检测会兼顾高置信代表帧与低置信边界帧。候选只带新模型
建议，人工结论由 Vision Lab 复核；Worker 不会把预标直接当成训练真值。
Server 将候选原子写入 NAS 的
`/volume1/docker/blrec-next/vision-data/candidates`；视觉素材不得写入 `config`。

## 开发验证

在仓库根目录安装共享后端后，再安装 Worker：

```bash
python -m pip install -e '.[dev]'
python -m pip install -e 'apps/analysis-worker[test]'
python -m pytest apps/analysis-worker/tests tests/vainglory/test_analyzer.py
```

本机只运行源码测试。正式 wheel、sdist 和发布附件由 GitHub Actions 构建；不要
为此启动本机 Docker。

## 运行

Worker 需要本机 `ffmpeg`、NAS API 地址、Worker token 文件和 OCR 服务地址：

```bash
blrec-analysis-worker run \
  --server http://192.168.50.24:2234 \
  --token-file /path/to/worker-token \
  --ocr-url http://127.0.0.1:18080 \
  --model-package /path/to/unpacked/vg-vision-package \
  --execution-provider coreml
```

`--model-package` 也可以通过 `BLREC_VISION_MODEL_PACKAGE` 指定，并且是必填项。
Worker 启动时会校验包状态、七个模型角色、类别顺序和每个 ONNX 的 SHA-256；
不完整、未验收或被修改的模型包会直接拒绝加载。Worker wheel 不再携带旧模型或
SIFT 英雄参考图，也不存在旧模型回退路径。

版本化模型包使用 `timeline-v2` 管线：一级时间线默认每 60 秒做一次关键帧粗扫，
只在对局内／外状态发生变化的两个粗扫点之间补做 5 秒局部扫描。FFmpeg 跳过
普通帧并保留源时间戳；结算检测不会跟着一级扫描逐帧
运行，而只在时间线推断
出的疑似结束窗口内以 4 FPS 扫描。最终选中的训练候选会回到原视频取高分辨率帧，
低分辨率时间线图不直接作为主要训练素材。

`deploy/macos/` 提供 launchd 模板。Worker 只通过版本化 HTTP 分析协议领任务和
回传结果；缓存、日志与 token 均位于源码目录之外。

新管线会回传模型包、关键帧／补帧、时间线分段、结算窗口和候选统计，所以正式
启用时需要先更新 NAS Server，再更新 MacBook Pro Worker。只在 Vision Lab 中
新增标注或重新训练，不需要更新 NAS；生成模型包本身也不等于已经部署。
