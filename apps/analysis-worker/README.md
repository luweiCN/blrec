# BLREC Analysis Worker

Analysis Worker 在 Mac 上领取 BLREC Server 的对局分析任务，并使用独立打包的
ONNX 模型、英雄参考图、OpenCV、ONNX Runtime 和 RapidOCR 完成分析。它不进入
NAS 的 Server 镜像，也不直接访问生产数据库。

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
