# BLREC Analysis Worker

Analysis Worker 在 Mac 上领取 BLREC Server 的对局分析任务，并使用独立打包的
ONNX 模型、英雄参考图、OpenCV、ONNX Runtime 和 RapidOCR 完成分析。它不进入
NAS 的 Server 镜像，也不直接访问生产数据库。

正常分析还会从已经解码的帧中限量挑选旧画面状态、BP、关键界面、结算检测和
地图证据，再按图片合并成统一四项预标：对局流程、英雄选择、对局模式和结算
面板。结算检测会兼顾高置信代表帧与低置信边界帧。候选只带模型建议，人工结论由
Vision Lab 复核；Worker 不会把预标直接当成训练真值。
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
  --execution-provider coreml
```

`deploy/macos/` 提供 launchd 模板。Worker 只通过版本化 HTTP 分析协议领任务和
回传结果；缓存、日志与 token 均位于源码目录之外。

新增候选任务同时改变共享分析协议，所以正式启用这批采样逻辑时需要先更新兼容
新候选格式的 NAS Server，再更新 MacBook Pro Worker。只在 Vision Lab 中新增
标注或训练模型，不需要更新 NAS。新模型通过验收并生成完整模型包后，仍需后续
Worker 模型包加载器和影子管线接入；生成 ZIP 本身不等于已经部署。
