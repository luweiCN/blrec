# 虚荣视觉模型独立调用说明(不依赖本项目 Python 服务)

三个模型均为标准 **ONNX Runtime 格式**,任何语言/框架(ONNX Runtime 支持 Python / C++ / C# / Java / JavaScript / Go 等)都可以独立加载运行。
`data/models/` 目录(相对于本脚本目录):

| 模型 | 文件 | 大小 | 作用 |
|---|---|---|---|
| multi-v2 | `data/models/multi-v2.onnx` | 44.7 MB | 一帧同时输出:是否虚荣 / 阶段(8类) / 模式(3类) |
| result-detector-v1 | `data/models/result-detector-v1.onnx` | 12.3 MB | 定位结算面板位置(框) |
| result-panel | `data/models/result-panel.onnx` | 10.6 MB | 同上(旧模型,blrec 自带,可对比) |

> 另保留 `multi-v2.pt`(PyTorch 格式,仅供本项目重训/调试用,接入方请使用 .onnx)。

---

## 1. multi-v2(多输出头分类)

### 输入

- 节点名:`images`,形状 `[1, 3, 224, 224]`,float32,RGB
- 预处理(必须一致):
  1. 缩放到 224×224(直接拉伸,**不保持比例、不加边**)
  2. 像素 / 255.0
  3. 按 ImageNet 归一化:`(x - mean) / std`
     `mean = [0.485, 0.456, 0.406]`, `std = [0.229, 0.224, 0.225]`

### 输出(三个头,均为原始 logits,需自己 softmax)

| 输出节点 | 形状 | 类别顺序(固定,按索引) |
|---|---|---|
| `content` | [1, 2] | 0=vainglory(虚荣), 1=not_vainglory(非虚荣) |
| `stage` | [1, 8] | 0=gameplay(对局中), 1=scoreboard(积分板), 2=result_page(结算页), 3=victory_defeat(胜负动画), 4=pre_match(赛前), 5=out_of_match(游戏外), 6=transition(转场), 7=talent_select(天赋选择) |
| `mode` | [1, 3] | 0=3v3, 1=aram(大乱斗), 2=5v5 |

### 必须实现的程序规则(重要)

1. **大乱斗强区分**:`stage` 头 top1 == 7(talent_select,天赋选择)→ 模式直接判定为 **aram(大乱斗)**,忽略 mode 头。天赋选择是大乱斗特有界面,100% 可靠。
2. **对局中不确定性**:`stage` top1 == 0(gameplay)且 `mode` top1 == 0(3v3)时,输出应为 **"3v3 或大乱斗(待确认)"**(两者同地图,单帧不可区分;用积分板/结算页/天赋选择做局级确认)。
3. 积分板(1)、结算页(2)、赛前(4)、天赋选择(7)帧的 mode 头结果可信,直接使用。

### Python(onnxruntime)示例

```python
import numpy as np
import onnxruntime as ort
from PIL import Image

sess = ort.InferenceSession('data/models/multi-v2.onnx',
                            providers=['CPUExecutionProvider'])
img = Image.open('frame.jpg').convert('RGB').resize((224, 224))
x = np.asarray(img, dtype=np.float32) / 255.0
x = (x - np.array([0.485, 0.456, 0.406], dtype=np.float32)) \
    / np.array([0.229, 0.224, 0.225], dtype=np.float32)
x = x.transpose(2, 0, 1)[None].astype(np.float32)  # [1,3,224,224]

content, stage, mode = sess.run(None, {'images': x})

def softmax(v):
    e = np.exp(v - v.max()); return e / e.sum()

c = int(content[0].argmax())       # 0=虚荣
s = int(stage[0].argmax())         # 7=天赋选择
m = int(mode[0].argmax())          # 1=大乱斗
conf = float(softmax(stage[0])[s])

if s == 7:      # 天赋选择 → 大乱斗(规则)
    mode_name = 'aram(大乱斗)'
elif s == 0 and m == 0:
    mode_name = '3v3 或大乱斗(待确认)'
else:
    mode_name = ['3v3', 'aram(大乱斗)', '5v5'][m]
```

---

## 2. result-detector-v1 / result-panel(结算面板检测)

两个模型结构、输入输出完全相同,可互换(建议优先 result-detector-v1)。

### 输入

- 节点名:`images`,形状 `[1, 3, 640, 640]`,float32,RGB
- 预处理(YOLO 标准 letterbox,必须一致):
  1. 按比例缩放原图到 640×640 内(长边 640)
  2. 剩余区域用 **灰边 114 填充**(不是黑色 0)
  3. 像素 / 255.0(注意:是 /255,不做 ImageNet 归一化)

### 输出

- 节点名:`output0`,形状 `[1, 5, 8400]`
- 每列 = 一个候选框:`[cx, cy, w, h, confidence]`,坐标相对 **640×640 输入图**(letterbox 后)
- 后处理:
  1. 过滤 `confidence < 0.25`
  2. NMS(IoU 阈值 0.45)去重
  3. 把框坐标从 letterbox 坐标系**映射回原图**:减去 padding、除以缩放比
- 若 NMS 后无框 → 该帧无结算面板

### Python 示例

```python
import numpy as np
import onnxruntime as ort
from PIL import Image

sess = ort.InferenceSession('data/models/result-detector-v1.onnx',
                            providers=['CPUExecutionProvider'])
img = Image.open('frame.jpg').convert('RGB')
w, h = img.size
scale = min(640 / w, 640 / h)
nw, nh = round(w * scale), round(h * scale)
canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
canvas[(640 - nh)//2:(640 - nh)//2 + nh, (640 - nw)//2:(640 - nw)//2 + nw] = \
    np.asarray(img.resize((nw, nh)), dtype=np.uint8)
x = (canvas.transpose(2, 0, 1) / 255.0)[None].astype(np.float32)

out = sess.run(None, {'images': x})[0][0]  # [5, 8400]
cx, cy, bw, bh, conf = out
mask = conf >= 0.25
boxes = np.stack([cx - bw/2, cy - bh/2, cx + bw/2, cy + bh/2], 1)[mask]
confs = conf[mask]
# (此处按置信度降序 + IoU 0.45 做 NMS)
# 取 NMS 后的第一个框,映射回原图:
# x1 = (x1 - pad_x) / scale,  y1 = (y1 - pad_y) / scale ...
# pad_x = (640 - nw)//2, pad_y = (640 - nh)//2
```

---

## 3. 建议的粗扫工作流(接入方可参考)

```
1. 粗扫:整段视频每 5 秒取一帧 → multi-v2
   → 记录每帧的 (时间, content, stage, mode)
2. 定位结算区间:连续出现 stage ∈ {result_page(2), victory_defeat(3)} 的片段
   → 前后各扩 30 秒为细扫区间
3. 细扫:区间内每 0.5~1 秒取一帧 → multi-v2,过滤出 result_page 帧
4. 抠图:result_page 帧跑 result-detector-v1 → 拿结算面板框 → 抠出供 OCR/哈希匹配
5. 局级模式判定:该局粗扫帧出现 talent_select(7) → 大乱斗;
   否则取 scoreboard/result_page 帧的 mode 头;都没有 → 默认 3v3
```

## 注意事项

- multi-v2 的类别索引顺序**固定**,不要按字母序假设;
- 两个检测模型输出的坐标是 letterbox 坐标系,务必做映射回原图;
- 全部模型均为 fp32,CPU 可直接跑(J4125 量级:multi-v2 约 40~80ms/帧,检测约 100~200ms/帧);
- 需要更高性能可自行用 onnxruntime 量化(INT8)或转 Core ML。
