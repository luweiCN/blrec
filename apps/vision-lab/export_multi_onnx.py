"""导出 multi-v2 为 ONNX(三头输出),并验证 ONNX 与 PyTorch 结果一致。"""
import sys

import numpy as np
import onnxruntime as ort
import torch
from PIL import Image
from torchvision import transforms

sys.path.insert(0, '.')
from train_multi import MultiHeadModel

DEVICE = 'cpu'
model = MultiHeadModel(pretrained=False)
model.load_state_dict(torch.load('data/models/multi-v2.pt',
                                 map_location=DEVICE))
model.eval()

# 导出:输入 [1,3,224,224],输出三个头的 logits
x = torch.randn(1, 3, 224, 224)
torch.onnx.export(
    model, x, 'data/models/multi-v2.onnx',
    input_names=['images'],
    output_names=['content', 'stage', 'mode'],
    dynamic_axes={'images': {0: 'batch'}},
    opset_version=17,
)
print('导出完成')

# 验证:同一张图,pt 与 onnx 输出一致
tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])
img = Image.open('data/frames/8503d39ccb9d45bfc8291fc3d447e3c664f6f3150a5a5a2f6dc236296fea512d.jpg').convert('RGB')
xv = tf(img)[None]
with torch.no_grad():
    pc, ps, pm = model(xv)
sess = ort.InferenceSession('data/models/multi-v2.onnx',
                            providers=['CPUExecutionProvider'])
oc, os_, om = sess.run(None, {'images': xv.numpy()})
for name, a, b in (('content', pc.numpy(), oc), ('stage', ps.numpy(), os_),
                   ('mode', pm.numpy(), om)):
    diff = np.abs(a - b).max()
    print(f'{name}: pt[{a.shape}] vs onnx[{b.shape}] 最大差异 {diff:.6f}')
    assert diff < 1e-4, f'{name} 不一致!'
print('验证通过:ONNX 与 PyTorch 输出一致')
