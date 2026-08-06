import sys, os
sys.path.insert(0, "C:/Users/堇岳/.workbuddy/binaries/python/envs/default/site-packages")

import imageio_ffmpeg
import imageio

VIDEO = "C:/Users/堇岳/Downloads/QQ20260806-204655.mp4"
OUTDIR = "C:/Users/堇岳/Desktop/dian-shang-ai-image/_frames"
os.makedirs(OUTDIR, exist_ok=True)

reader = imageio.get_reader(VIDEO, "ffmpeg")
frames = []
for f in reader:
    frames.append(f)
reader.close()
n = len(frames)
print("实际总帧数:", n, "尺寸:", frames[0].shape)

num = 12
step = (n - 1) / (num - 1)
idxs = sorted(set(int(round(k * step)) for k in range(num)))
print("抽取帧序号:", idxs)

paths = []
for i, idx in enumerate(idxs):
    f = frames[idx]
    h, w = f.shape[:2]
    scale = min(1.0, 720.0 / w)
    if scale < 1.0:
        nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
        f = f[::max(1, h // nh), ::max(1, w // nw)]
    p = os.path.join(OUTDIR, f"frame_{i:02d}_{idx:04d}.jpg")
    imageio.imwrite(p, f, "pillow", quality=82)
    paths.append(p)
print("保存:", paths)
