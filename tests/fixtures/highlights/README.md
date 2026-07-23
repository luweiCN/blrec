# 高光剪辑实媒体测试

测试默认不保存大型视频。真实媒体矩阵会在临时目录自动生成不同 GOP、
B 帧、音频、H.264/H.265、FLV 索引和 MP4 封装的素材，并执行 160 个剪辑组合：

```bash
BLREC_RUN_HIGHLIGHT_MEDIA_TESTS=1 \
  python -m pytest tests/bili_upload/test_highlight_cut_ffmpeg.py -q
```

该矩阵是 GitHub 发布工作流的必过门禁，不依赖私有录像文件。另可用本地真实录像
执行单素材检查：

```bash
BLREC_HIGHLIGHT_FIXTURE=/tmp/blrec-highlight-fixture/source.mp4 \
  python -m pytest tests/bili_upload/test_highlight_cut.py -k real_ffmpeg -q
```

除 160 个组合外，门禁还包含 10 个首尾画面对比、1 个音频先于视频开始的时间戳错位用例和 1 个旧版多分段用例，共 172 例。测试会确认输出为浏览器可播放的 H.264，按输入保留音频流，持续时间和首尾画面符合预检结果，并可从头到尾完整解码。流复制未通过语义验收时允许自动进入快速或顺序转码兜底。
