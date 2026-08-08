"""诊断:multi-v2 在天赋选择帧上的表现(期望模式=大乱斗)。"""
import json
import sys
from collections import Counter

import torch
from PIL import Image
from torchvision import transforms

sys.path.insert(0, '.')
from train_multi import CONTENT_CLS, MODE_CLS, STAGE_CLS, MultiHeadModel

DEVICE = 'mps' if torch.backends.mps.is_available() else 'cpu'
model = MultiHeadModel(pretrained=False)
model.load_state_dict(torch.load('data/models/multi-v2.pt',
                                 map_location=DEVICE))
model.eval().to(DEVICE)
tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])

items = [json.loads(l) for l in
         open('data/datasets/multi-v2/val.json')] + \
        [json.loads(l) for l in
         open('data/datasets/multi-v2/train.json')]
talents = [it for it in items if it.get('screen_type') == 'talent_select']
print(f'天赋选择帧共 {len(talents)} 张(期望模式=大乱斗)')
preds = Counter()
stage_preds = Counter()
with torch.no_grad():
    for it in talents:
        img = Image.open(f"data/frames/{it['sha']}.jpg").convert('RGB')
        x = tf(img)[None].to(DEVICE)
        out_c, out_s, out_m = model(x)
        preds[MODE_CLS[out_m.argmax(1).item()]] += 1
        stage_preds[STAGE_CLS[out_s.argmax(1).item()]] += 1
print('mode 预测分布:', dict(preds))
print('stage 预测分布:', dict(stage_preds))
print('--- 逐张(前 10)---')
with torch.no_grad():
    for it in talents[:10]:
        img = Image.open(f"data/frames/{it['sha']}.jpg").convert('RGB')
        x = tf(img)[None].to(DEVICE)
        out_c, out_s, out_m = model(x)
        pm = out_m.softmax(1)[0]
        ps = out_s.softmax(1)[0]
        mi = out_m.argmax(1).item()
        si = out_s.argmax(1).item()
        print(f"帧{it['frame_id']} | mode={MODE_CLS[mi]}({pm[mi]:.2f}) "
              f"| stage={STAGE_CLS[si]}({ps[si]:.2f}) "
              f"| 视频{it['video_id']}")
