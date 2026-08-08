"""评估 multi-v2:在指定集合上跑三头,输出各头准确率与混淆。

用法:python eval_multi_v2.py [val|test]  (默认 val+test 都跑)
"""
import json
import sys
from collections import Counter, defaultdict

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

SPLITS = sys.argv[1:] or ['val', 'test']


def run_split(sp):
    items = [json.loads(l)
             for l in open(f'data/datasets/multi-v2/{sp}.json')]
    conf = {h: defaultdict(Counter) for h in ('content', 'stage', 'mode')}
    total = {h: Counter() for h in ('content', 'stage', 'mode')}
    with torch.no_grad():
        for it in items:
            img = Image.open(f"data/frames/{it['sha']}.jpg").convert('RGB')
            x = tf(img)[None].to(DEVICE)
            out_c, out_s, out_m = model(x)
            pred_c = CONTENT_CLS[out_c.argmax(1).item()]
            pred_s = STAGE_CLS[out_s.argmax(1).item()]
            pred_m = MODE_CLS[out_m.argmax(1).item()]
            if it['content']:
                conf['content'][it['content']][pred_c] += 1
                total['content'][it['content']] += 1
            if it['stage']:
                conf['stage'][it['stage']][pred_s] += 1
                total['stage'][it['stage']] += 1
            if it['mode']:
                conf['mode'][it['mode']][pred_m] += 1
                total['mode'][it['mode']] += 1
    print(f'===== {sp} ({len(items)} 张) =====')
    for h in ('content', 'stage', 'mode'):
        accs = []
        for t in sorted(total[h]):
            c = conf[h][t]
            acc = c[t] / total[h][t]
            accs.append(acc)
            wrong = {k: v for k, v in c.items() if k != t}
            print(f'  {t:14s} {c[t]:4d}/{total[h][t]:4d} = {acc*100:5.1f}%  '
                  f'误判: {dict(sorted(wrong.items(), key=lambda kv: -kv[1]))}')
        n = sum(total[h].values())
        ok = sum(conf[h][t][t] for t in total[h])
        print(f'  >> {h} 合计 {ok}/{n} = {ok/n*100:.1f}%')


for sp in SPLITS:
    run_split(sp)
