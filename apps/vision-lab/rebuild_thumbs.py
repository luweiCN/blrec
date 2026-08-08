"""一次性:把已有帧缩略图重建为 960px(原图不动)。"""
import sys
sys.path.insert(0, '.')
from PIL import Image
from labeler import config, db

conn = db.connect(config.DB_PATH)
rows = conn.execute(
    'SELECT id, frame_path, thumb_path FROM frames').fetchall()
config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
n = 0
for r in rows:
    fp, tp = r['frame_path'], r['thumb_path']
    try:
        img = Image.open(fp)
        t = img.copy()
        t.thumbnail((config.THUMB_WIDTH, config.THUMB_WIDTH))
        t.convert('RGB').save(tp, quality=80)
        n += 1
        if n % 200 == 0:
            print(f'{n}/{len(rows)}', flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f'frame {r["id"]} 失败: {exc}', flush=True)
print(f'重建 {n} 张缩略图完成', flush=True)
conn.close()
