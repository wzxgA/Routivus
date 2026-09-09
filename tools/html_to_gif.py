"""把 tuidemo.html 演示录制成 GIF。

不修改源 HTML：通过 Playwright 注入 init script 给大延迟提速（>=120ms 的
setTimeout 按倍率压缩），逐帧截图 .term 区域，最后用 Pillow 合成全局调色板 GIF。

浏览器：优先用系统自带 Edge（channel=msedge），无需下载 Chromium；
若 Edge 不可用则回落 chromium（需 playwright install）。

结束判定：静止 --idle-sec 秒（默认 12s，真实时长 = idle_sec * speed）。
结尾自动裁掉多余静止帧，只保留 --tail-ms 的停顿。

用法:
    uv run --with playwright --with pillow python tools/html_to_gif.py
        [--html Routivus-docs/tuidemo.html] [--out Routivus-docs/tuidemo.gif]
        [--speed 0.55] [--fps 8] [--scale 0.8] [--max-sec 240] [--idle-sec 12]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import time
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent


async def _launch(p):
    """优先 Edge，失败回落 Chromium。"""
    try:
        return await p.chromium.launch(
            channel="msedge", args=["--force-color-profile=srgb"]
        )
    except Exception:
        return await p.chromium.launch(args=["--force-color-profile=srgb"])


async def record(html: Path, speed: float, fps: int, max_sec: int, idle_sec: int):
    """打开页面并按 fps 截图，返回 (png bytes 列表, hash 列表, 单帧毫秒)."""
    frame_ms = 1000 / fps
    async with async_playwright() as p:
        browser = await _launch(p)
        page = await browser.new_page(viewport={"width": 1240, "height": 980})
        try:
            await page.emulate_media(reduced_motion="no-preference")
        except Exception:
            pass
        # 注入提速：大延迟压缩，打字逐字节奏保留
        await page.add_init_script(
            """
            (() => {
                const f = %s;
                const orig = window.setTimeout.bind(window);
                window.setTimeout = function(fn, ms, ...rest) {
                    ms = ms || 0;
                    return orig(fn, ms >= 120 ? ms * f : ms, ...rest);
                };
            })();
            """ % speed
        )
        await page.goto(html.as_uri())
        await page.wait_for_timeout(400)
        # 冻结光标闪烁：保证静止期帧哈希一致，用于自动检测演示结束
        await page.add_style_tag(
            content=".cursor{animation:none !important;opacity:1 !important}"
        )
        el = await page.query_selector(".term")

        pngs: list[bytes] = []
        hashes: list[str] = []
        last, idle = None, 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_sec:
            png = await el.screenshot(type="png")
            digest = hashlib.md5(png).hexdigest()
            pngs.append(png)
            hashes.append(digest)
            idle = idle + 1 if digest == last else 0
            last = digest
            if idle >= fps * idle_sec:  # 静止 idle_sec 秒 => 演示结束
                break
            await asyncio.sleep(max(0.0, frame_ms / 1000 - 0.045))
        await browser.close()
        return pngs, hashes, frame_ms


def build_gif(pngs, hashes, frame_ms, scale, out: Path, tail_ms=1200, colors=200):
    imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in pngs]
    w = max(1, int(imgs[0].width * scale))
    h = max(1, int(imgs[0].height * scale))
    imgs = [im.resize((w, h), Image.LANCZOS) for im in imgs]

    # 结尾只保留 tail_ms 的静止停顿
    cut = len(imgs)
    while cut > 1 and hashes[cut - 1] == hashes[cut - 2]:
        cut -= 1
    cut = min(len(imgs), cut + max(1, int(tail_ms / frame_ms)))
    imgs, hashes = imgs[:cut], hashes[:cut]

    # 合并连续重复帧（长停顿只存一帧）
    frames, durs = [], []
    i = 0
    while i < len(imgs):
        j = i
        while j + 1 < len(imgs) and hashes[j + 1] == hashes[i]:
            j += 1
        frames.append(imgs[i])
        durs.append(int((j - i + 1) * frame_ms))
        i = j + 1

    # 全局调色板（抽样帧拼条量化，避免逐帧调色板闪烁）
    step = max(1, len(frames) // 8)
    sample = frames[::step][:8]
    strip = Image.new("RGB", (w, h * len(sample)))
    for k, f in enumerate(sample):
        strip.paste(f, (0, k * h))
    pal = strip.quantize(colors=colors)
    qframes = [f.quantize(palette=pal, dither=Image.NONE) for f in frames]

    qframes[0].save(
        out, save_all=True, append_images=qframes[1:],
        duration=durs, loop=0, optimize=True,
    )
    total = sum(durs) / 1000
    size = out.stat().st_size / 1e6
    print(f"GIF 完成: {out}")
    print(f"  帧数 {len(qframes)}  时长 {total:.1f}s  尺寸 {w}x{h}  大小 {size:.1f}MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default="Routivus-docs/tuidemo.html")
    ap.add_argument("--out", default="Routivus-docs/tuidemo.gif")
    ap.add_argument("--speed", type=float, default=0.55,
                    help=">=120ms 延迟的压缩倍率，越小越快")
    ap.add_argument("--fps", type=int, default=8)
    ap.add_argument("--scale", type=float, default=0.8)
    ap.add_argument("--max-sec", type=int, default=240)
    ap.add_argument("--idle-sec", type=int, default=12,
                    help="静止多少真实秒判定演示结束（页内时长 = idle_sec*speed）")
    args = ap.parse_args()

    html = (ROOT / args.html).resolve()
    out = (ROOT / args.out).resolve()
    print(f"录制 {html} -> {out}")
    pngs, hashes, frame_ms = asyncio.run(
        record(html, args.speed, args.fps, args.max_sec, args.idle_sec)
    )
    print(f"采集 {len(pngs)} 帧，合成中…")
    build_gif(pngs, hashes, frame_ms, args.scale, out)


if __name__ == "__main__":
    main()
