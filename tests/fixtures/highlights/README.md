# 高光剪辑实媒体测试

测试默认不保存大型视频。需要验证本机 FFmpeg 时，先在临时目录生成素材：

```bash
mkdir -p /tmp/blrec-highlight-fixture
ffmpeg -hide_banner -f lavfi -i testsrc2=size=640x360:rate=30 \
  -f lavfi -i sine=frequency=1000 -t 40 -c:v libx264 -g 60 \
  -c:a aac -y /tmp/blrec-highlight-fixture/source.mp4
```

然后运行：

```bash
BLREC_HIGHLIGHT_FIXTURE=/tmp/blrec-highlight-fixture/source.mp4 \
  python -m pytest tests/bili_upload/test_highlight_cut.py -k real_ffmpeg -q
```

测试会确认输出保留原视频、音频编码，持续时间符合预检结果，并且 FFmpeg 只使用流复制。
