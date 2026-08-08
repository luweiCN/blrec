"""训练多输出头模型 multi-v2:一个 ResNet18 骨干,同时输出
content(虚荣/非虚荣) + stage(7 类,拆分积分板) + mode(3 类)。

- mode 标签已由 build_multi_v2_data.py 按界面策略生成:
  对局中/胜负动画: 5v5 保持,3v3与大乱斗统一标 3v3(推断时输出"3v3 或大乱斗")
  积分板/结算页/英雄选择/排队/天赋选择: 真实标签
  其他界面: mode=None 不参与 loss
- stage=None 的帧(content=not_vainglory)不参与 stage loss
- 早停按 val 三头平均 top1;MPS 训练
"""
import json
import sys
from collections import Counter
from pathlib import Path

import torch
import torch.nn as nn
import torchvision
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms

sys.path.insert(0, '.')

DATA = Path('data/datasets/multi-v2')
FRAME_DIR = Path('data/frames')

CONTENT_CLS = ['vainglory', 'not_vainglory']
STAGE_CLS = ['gameplay', 'scoreboard', 'result_page', 'victory_defeat',
             'pre_match', 'out_of_match', 'transition', 'talent_select']
MODE_CLS = ['3v3', 'aram', '5v5']

# mode 类别权重:3v3 样本多,aram/5v5 少
MODE_WEIGHTS = [1.0, 3.0, 2.5]

IMG_SIZE = 224


def load_items(split):
    items = []
    with open(DATA / f'{split}.json') as f:
        for line in f:
            d = json.loads(line)
            d['path'] = FRAME_DIR / f"{d['sha']}.jpg"
            items.append(d)
    return items


class MultiDataset(Dataset):
    def __init__(self, split, augment=False):
        self.items = load_items(split)
        if augment:
            self.tf = transforms.Compose([
                transforms.RandomHorizontalFlip(p=0.5),
                transforms.ColorJitter(brightness=0.2, contrast=0.2,
                                       saturation=0.2, hue=0.05),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406],
                                     [0.229, 0.224, 0.225]),
            ])
        else:
            self.tf = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406],
                                     [0.229, 0.224, 0.225]),
            ])

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        it = self.items[i]
        img = Image.open(it['path']).convert('RGB').resize(
            (IMG_SIZE, IMG_SIZE), Image.BILINEAR)
        x = self.tf(img)
        content = CONTENT_CLS.index(it['content']) if it['content'] else -1
        stage = STAGE_CLS.index(it['stage']) if it['stage'] else -1
        mode = MODE_CLS.index(it['mode']) if it['mode'] else -1
        return x, content, stage, mode


class MultiHeadModel(nn.Module):
    def __init__(self, pretrained=False):
        super().__init__()
        if pretrained:
            try:
                weights = torchvision.models.ResNet18_Weights.IMAGENET1K_V1
                backbone = torchvision.models.resnet18(weights=weights)
            except Exception as e:
                print(f'预训练权重不可用({e}),使用随机初始化')
                backbone = torchvision.models.resnet18(weights=None)
        else:
            backbone = torchvision.models.resnet18(weights=None)
        feat = backbone.fc.in_features
        backbone.fc = nn.Identity()
        self.backbone = backbone
        self.content_head = nn.Linear(feat, len(CONTENT_CLS))
        self.stage_head = nn.Linear(feat, len(STAGE_CLS))
        self.mode_head = nn.Linear(feat, len(MODE_CLS))

    def forward(self, x):
        f = self.backbone(x)
        return (self.content_head(f), self.stage_head(f), self.mode_head(f))


def run_epoch(model, loader, opt, device, train):
    model.train(train)
    total = 0
    correct = {k: 0 for k in ('content', 'stage', 'mode')}
    counted = {k: 0 for k in ('content', 'stage', 'mode')}
    loss_sum = 0.0
    ce = nn.CrossEntropyLoss()
    ce_mode = nn.CrossEntropyLoss(
        weight=torch.tensor(MODE_WEIGHTS, dtype=torch.float32, device=device))
    for x, c, s, m in loader:
        x = x.to(device)
        c = c.to(device)
        s = s.to(device)
        m = m.to(device)
        out_c, out_s, out_m = model(x)
        loss = out_c.sum() * 0.0
        if train:
            opt.zero_grad()
        content_mask = c >= 0
        stage_mask = s >= 0
        mode_mask = m >= 0
        if content_mask.any():
            loss = loss + ce(out_c[content_mask], c[content_mask])
        if stage_mask.any():
            loss = loss + ce(out_s[stage_mask], s[stage_mask])
        if mode_mask.any():
            loss = loss + 1.5 * ce_mode(out_m[mode_mask], m[mode_mask])
        if train:
            loss.backward()
            opt.step()
        loss_sum += loss.item()
        total += len(x)
        for name, out, t in (('content', out_c, c), ('stage', out_s, s),
                             ('mode', out_m, m)):
            mask = t >= 0
            if mask.any():
                pred = out.argmax(1)[mask]
                correct[name] += (pred == t[mask]).sum().item()
                counted[name] += mask.sum().item()
    acc = {k: (correct[k] / counted[k] if counted[k] else 0.0)
           for k in correct}
    return loss_sum / max(1, total), acc


def main():
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = MultiHeadModel(pretrained=False).to(device)
    train_ds = MultiDataset('train', augment=True)
    val_ds = MultiDataset('val')
    train_loader = DataLoader(train_ds, batch_size=64, shuffle=True,
                              num_workers=2)
    val_loader = DataLoader(val_ds, batch_size=64, num_workers=2)
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=60)

    best = -1.0
    best_state = None
    patience = 0
    for epoch in range(60):
        loss, _ = run_epoch(model, train_loader, opt, device, True)
        vloss, vacc = run_epoch(model, val_loader, opt, device, False)
        score = sum(vacc.values()) / 3
        sched.step()
        print(f'epoch {epoch+1:02d} loss={loss:.4f} '
              f'val_c={vacc["content"]:.3f} val_s={vacc["stage"]:.3f} '
              f'val_m={vacc["mode"]:.3f} (avg {score:.3f})')
        if score > best:
            best = score
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            patience = 0
        else:
            patience += 1
            if patience >= 15:
                print(f'早停于 epoch {epoch+1},best avg={best:.3f}')
                break
    model.load_state_dict(best_state)
    torch.save(model.state_dict(), 'data/models/multi-v2.pt')
    print(f'已保存 data/models/multi-v1.pt (best avg {best:.3f})')


if __name__ == '__main__':
    main()
