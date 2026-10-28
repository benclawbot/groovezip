#!/usr/bin/env python3
# Procedural background generator for 5 music-themed web design mockups.
# Produces 1600x1000 PNGs using numpy + Pillow (bundled runtime).
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1600, 1000

FONT_CANDIDATES = [
    "/home/tom/.config/chromium/Default/Extensions/hehggadaopoacecdllhhajmbjkdcmajg/1.26.901.11451_0/codex-sidepanel/assets/KaTeX_SansSerif-Bold-CFMepnvq.ttf",
    "/home/tom/.config/chromium/Default/Extensions/hehggadaopoacecdllhhajmbjkdcmajg/1.26.901.11451_0/codex-sidepanel/assets/KaTeX_Main-Bold-waoOVXN0.ttf",
]
def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return None

def to_img(arr):
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8), "RGB")

def comp(arr, overlay, ox=0, oy=0):
    ov = np.asarray(overlay, np.float32) / 255.0
    a = ov[..., 3:4]
    col = ov[..., :3]
    h, w = ov.shape[:2]
    sl = arr[oy:oy + h, ox:ox + w]
    arr[oy:oy + h, ox:ox + w] = sl * (1.0 - a) + col * a

def glow(arr, cx, cy, sx, sy, color, peak):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dx = xx - cx; dy = yy - cy
    g = np.exp(-0.5 * ((dx / sx) ** 2 + (dy / sy) ** 2)) * peak
    c = np.asarray(color, np.float32)
    arr[:] = arr + g[..., None] * c

def grain(arr, amt, mono=True):
    n = np.random.rand(H, W, 1).astype(np.float32) if mono else np.random.rand(H, W, 3).astype(np.float32)
    n = (n - 0.5) * 2 * amt
    arr[:] = arr * (1.0 + n)

def darken_center(arr, strength=0.55, rx=430, ry=420):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W / 2) / rx) ** 2 + ((yy - H / 2) / ry) ** 2)
    m = strength + (1.0 - strength) / (1.0 + d ** 1.2)
    arr *= m[..., None]

def vignette_edges(arr, strength=0.45, inner=0.55):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W / 2) / (W * 0.64)) ** 2 + ((yy - H / 2) / (H * 0.64)) ** 2)
    m = 1.0 - strength * np.clip(d - inner, 0, None) / (1 - inner)
    arr *= m[..., None]

# ----------------------------------------------------------------------------
# 1) DISCO / VINYL
# ----------------------------------------------------------------------------
def gen_vinyl():
    arr = np.zeros((H, W, 3), dtype=np.float32)
    # moody vertical base: dark violet
    t = np.linspace(0, 1, H)[:, None, None].astype(np.float32)
    arr[:] = np.asarray([0.05, 0.02, 0.10], np.float32) * (1 - t) + np.asarray([0.17, 0.05, 0.21], np.float32) * t
    # moody background glows (edges bright, hot pink / raspberry / purple)
    glow(arr, W * 0.03, H * 0.05, 560, 460, (0.86, 0.20, 0.46), 0.42)
    glow(arr, W * 1.0, H * 0.97, 600, 520, (0.74, 0.07, 0.60), 0.48)
    glow(arr, W * 0.5, H * 1.05, 900, 260, (0.42, 0.10, 0.62), 0.34)
    glow(arr, W * 0.92, H * 0.02, 500, 360, (0.60, 0.12, 0.80), 0.30)

    # bokeh overlay
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    bcols = [(255, 120, 190, 70), (255, 60, 160, 90), (200, 120, 255, 60), (255, 200, 120, 55), (120, 220, 255, 45)]
    for _ in range(46):
        c = bcols[np.random.randint(0, len(bcols))]
        x = np.random.uniform(0, W); y = np.random.uniform(0, H)
        r = np.random.uniform(7, 40)
        d.ellipse([x - r, y - r, x + r, y + r], fill=c)
    ov = ov.filter(ImageFilter.GaussianBlur(8))
    comp(arr, ov)

    # vinyl record (off-center right)
    cx, cy, rx, ry = 1150.0, 500.0, 352.0, 350.0
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    u = (xx - cx) / rx; v = (yy - cy) / ry
    rad = np.hypot(u, v)
    discm = rad <= 1.0
    # grooves
    gro = (np.sin(rad * np.pi * 2.0 * 42) * 0.5 + 0.5)
    sheen = np.exp(-((u * 0.82 + 0.40) ** 2 + (v * 0.55) ** 2) * 1.4)
    base = 0.055 + 0.05 * gro + 0.16 * sheen
    lab = rad <= 0.235
    lrings = (np.abs(np.mod(rad * 5.0, 1.0) - 0.5)).clip(0, 1)
    base = np.where(lab, 0.30 * np.ones_like(rad), np.where(rad > 0.965, base * 0.5, base))
    # disc color channels (violet-tinted black)
    disc_img = np.stack([base * 0.96, base * 0.94, base * 1.12], -1)
    disc_img = np.where(discm[..., None], disc_img, arr)
    # label accent: gold/pink thin ring + center hole
    labring = (rad > 0.21) & (rad <= 0.235)
    hole = rad <= 0.055
    disc_img = np.where(hole[..., None], disc_img * 0.6, disc_img)
    arr[:] = np.where(discm[..., None], disc_img, arr)
    # raspberry neon rim glow
    rg = np.exp(-(((rad - 1.0) / 0.085) ** 2)) * 0.9
    mask_rg = rad < 1.45
    arr += rg[..., None] * np.array([1.0, 0.16, 0.66], np.float32) * mask_rg[..., None]
    # thin bright rim line
    rim = np.abs(rad - 1.0) < 0.007
    arr[rim] = arr[rim] * 0.4 + np.array([1.0, 0.35, 0.85])
    # deep purple outer glow behind disc
    rg2 = np.exp(-(((rad - 1.25) / 0.22) ** 2)) * 0.5
    arr += rg2[..., None] * np.array([0.5, 0.16, 0.9], np.float32) * (rad < 2.0)[..., None]
    arr = np.clip(arr, 0, 1)

    # sparkle glints / disco-ball sparkles
    ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(ov2)
    sp = [(255, 255, 255, 210), (255, 150, 210, 160), (180, 220, 255, 150), (255, 210, 130, 150)]
    for _ in range(70):
        x = np.random.uniform(0, W); y = np.random.uniform(0, H)
        s = np.random.uniform(2.5, 9.0); c = sp[np.random.randint(0, len(sp))]
        # 4-point flare
        d2.line([x - s, y, x + s, y], fill=c, width=1)
        d2.line([x, y - s, x, y + s], fill=c, width=1)
        d2.ellipse([x - s * 0.35, y - s * 0.35, x + s * 0.35, y + s * 0.35], fill=c)
    ov2 = ov2.filter(ImageFilter.GaussianBlur(1.0))
    comp(arr, ov2)

    # dark clean center vignette so UI text reads well
    darken_center(arr, strength=0.58, rx=440, ry=430)
    vignette_edges(arr, strength=0.35, inner=0.6)
    grain(arr, 0.045)
    return arr

# ----------------------------------------------------------------------------
# 2) RETRO MIXTAPE / SUNSET
# ----------------------------------------------------------------------------
def gen_sunset():
    arr = np.zeros((H, W, 3), dtype=np.float32)
    t = np.linspace(0, 1, H)[:, None, None].astype(np.float32)
    arr[:] = np.asarray([1.0, 0.88, 0.70], np.float32) * (1 - t) + np.asarray([0.58, 0.12, 0.50], np.float32) * t
    glow(arr, W / 2, H * 0.80, 1400, 620, (1.0, 0.62, 0.26), 0.75)
    glow(arr, W * 0.2, H * 1.06, 900, 300, (0.95, 0.30, 0.45), 0.5)
    glow(arr, W * 1.0, H * 0.05, 700, 400, (1.0, 0.55, 0.55), 0.28)

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    sun_cy = H * 0.72; sun_r = H * 0.30
    d = np.hypot(xx - W / 2, yy - sun_cy)
    sunm = d <= sun_r
    # warm glow disc
    gg = np.exp(-(d / (sun_r * 0.9)) ** 2) * 0.95
    arr += gg[..., None] * np.array([1.0, 0.75, 0.45], np.float32)
    # sun gradient fill (bright top -> rose bottom)
    lt = np.clip((yy - (sun_cy - sun_r)) / (2 * sun_r), 0, 1)[..., None]
    suncol = np.asarray([1.0, 0.93, 0.55], np.float32) * (1 - lt) + np.asarray([1.0, 0.45, 0.68], np.float32) * lt
    # retro horizontal stripes clipped to sun
    structure = np.clip((yy - (sun_cy - sun_r)) / (sun_r + 10.0), 0, 1)[..., None]
    band = 0.5
    stripe_on = (np.floor((yy - (sun_cy - sun_r)) / (sun_r * 0.10) + 0.0)[..., None] % 2) == 0
    s_in = stripe_on & sunm[..., None]
    sunc2 = np.asarray([1.0, 0.55, 0.42], np.float32) * (1 - structure) + np.asarray([0.85, 0.20, 0.52], np.float32) * structure
    final_sun = np.where(s_in, sunc2, suncol)
    arr = np.where(sunm[..., None], final_sun, arr)
    band = None
    # thin bright top arc on sun
    arc = np.abs(d - sun_r) < 2.0
    arr[arc] = arr[arc] * 0.6 + np.array([1.0, 0.95, 0.75]) * 0.4

    # rolling dunes silhouette behind boombox
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d3 = ImageDraw.Draw(ov)
    pts = [(x, H - 30 - 14 * np.sin(x * 0.004 + 1.5) - 10 * np.sin(x * 0.011)) for x in range(0, W, 8)]
    pts += [(W, H + 40), (0, H + 40)]
    d3.polygon(pts, fill=(70, 22, 66, 255))
    ov = ov.filter(ImageFilter.GaussianBlur(6))
    comp(arr, ov)

    # ---- 1980s boombox cassette player ----
    ovb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    db = ImageDraw.Draw(ovb)
    box_l, box_r, box_t, box_b = 430, 1170, H - 345, H - 22
    body = (42, 16, 46, 255)
    # handle
    db.rounded_rectangle([W / 2 - 210, H - 405, W / 2 + 210, H - 320], radius=34, fill=None, outline=(52, 20, 52, 255), width=16)
    db.rounded_rectangle([box_l, box_t, box_r, box_b], radius=46, fill=body, outline=(20, 8, 26, 255), width=10)
    # glowing red meters strip
    db.rectangle([box_l + 40, box_t + 26, box_r - 40, box_t + 70], fill=(60, 24, 60, 255))
    # speakers
    for sxc in (585, 1015):
        db.ellipse([sxc - 145, box_b - 170, sxc + 145, box_b + 20], fill=(26, 8, 32, 255), outline=(70, 24, 64, 255), width=8)
        db.ellipse([sxc - 120, box_b - 145, sxc + 120, box_b + 95], fill=(16, 5, 22, 255))
        dgd = ImageDraw.Draw(ovb)
        for gx in range(sxc - 110, sxc + 111, 22):
            for gy in range(box_b - 135, box_b + 85, 22):
                r2 = np.hypot(gx - sxc, gy - (box_b - 25))
                if r2 < 96:
                    dgd.ellipse([gx - 6, gy - 6, gx + 6, gy + 6], fill=(66, 30, 70, 255))
        db.rectangle([sxc - 40, box_b - 70, sxc + 40, box_b + 10], fill=(30, 10, 36, 255))
    # cassette deck
    db.rounded_rectangle([700, H - 290, 900, H - 40], radius=18, fill=(14, 5, 20, 255), outline=(88, 30, 78, 255), width=8)
    db.line([700, H - 210, 900, H - 210], fill=(58, 20, 58, 255), width=6)
    for spx, spy in ((752, H - 300), (848, H - 300)):
        db.ellipse([spx - 26, spy - 26, spx + 26, spy + 26], outline=(120, 46, 110, 255), width=7)
        db.ellipse([spx, spy - 26, spx, spy + 26], fill=(90, 32, 88, 255))
        db.ellipse([spx - 5, spy - 5, spx + 5, spy + 5], outline=(150, 70, 130, 255), width=2)
        db.ellipse([spx - 9, H - 56, spx + 9, H - 38], fill=(190, 70, 120, 255))
    db.text((720, H - 70), "PLAY", font=load_font(16), fill=(200, 130, 150, 255))
    # buttons / knobs
    db.rounded_rectangle([700, H - 60, 790, H - 16], radius=10, fill=(20, 6, 26, 255))
    db.rounded_rectangle([810, H - 60, 900, H - 16], radius=10, fill=(20, 6, 26, 255))
    db.ellipse([820, H - 40, 892, H - 10], fill=(90, 26, 84, 255), outline=(120, 40, 110, 255), width=6)
    db.ellipse([838, H - 34, 874, H - 2], fill=(30, 10, 34, 255))
    # antenna
    db.line([1030, box_t + 40, 1175, H - 430], fill=(52, 18, 54, 255), width=12)
    db.ellipse([1160, H - 446, 1190, H - 416], fill=(70, 30, 80, 255))
    db.line([460, H - 6, 1140, H - 6], fill=(26, 8, 32, 255), width=10)
    ovb = ovb.filter(ImageFilter.GaussianBlur(1.2))
    # punchy magenta under-glow so the boombox pops
    glow_arr = np.zeros((H, W, 3), dtype=np.float32)
    glow(glow_arr, W / 2, H - 10, 900, 170, (1.0, 0.25, 0.55), 0.5)
    arr = np.clip(arr, 0, 1)
    ovb = ovb.filter(ImageFilter.GaussianBlur(0.8))
    comp(arr, ovb)
    arr = np.clip(arr * 1.0 + glow_arr * 0.0, 0, 1)
    # re-apply glow ABOVE boombox floor light
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    bglow = np.exp(-(((xx - W / 2) / 620) ** 2 + ((yy - (H - 0)) / 240) ** 2)) * 0.35
    ovg = Image.new("RGB", (W, H), (0, 0, 0))
    arr = np.clip(arr + bglow[..., None] * np.array([1.0, 0.3, 0.55], np.float32), 0, 1)
    # short re-composite boombox over that glow so it stays a solid silhouette
    comp(arr, ovb)
    # soft reflection glint on case
    arr = arr * 1.0
    grain(arr, 0.085)  # grainy analog film vibe
    vignette_edges(arr, strength=0.22, inner=0.5)
    return arr

# ----------------------------------------------------------------------------
# 3) NEON EDM
# ----------------------------------------------------------------------------
def gen_neon():
    arr = np.zeros((H, W, 3), dtype=np.float32)
    arr[:] = np.asarray([0.012, 0.012, 0.032], np.float32)
    glow(arr, W / 2, H * 0.30, 1400, 320, (0.06, 0.02, 0.22), 0.9)
    glow(arr, W * 0.15, H * 0.85, 900, 420, (0.05, 0.04, 0.20), 0.6)

    # perspective synthwave floor grid
    ovg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dg = ImageDraw.Draw(ovg)
    hx, hy = W / 2, 780
    for k in range(-26, 27):
        x0 = hx + k * 6
        x1 = hx + k * 300
        col = (40, 90, 110, 60)
        dg.line([x0, hy, x1, H], fill=col, width=2)
    for i in range(1, 16):
        y = hy + (i ** 1.7) * 3.0
        dg.line([W * 0.05, y, W * 0.95, y], fill=(60, 40, 120, 46), width=2)
    ovg = ovg.filter(ImageFilter.GaussianBlur(2))
    comp(arr, ovg)

    # laser beams
    ovl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dl = ImageDraw.Draw(ovl)
    dl.line([330, -60, 620, 660], fill=(204, 255, 61, 200), width=14)
    dl.line([1270, -60, 980, 660], fill=(255, 46, 240, 200), width=14)
    dl.line([0, 120, 760, 300], fill=(0, 229, 255, 140), width=8)
    ovl = ovl.filter(ImageFilter.GaussianBlur(14))
    comp(arr, ovl)
    # thin cores
    ovc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(ovc)
    dc.line([330, -60, 620, 660], fill=(225, 255, 140, 120), width=5)
    dc.line([1270, -60, 980, 660], fill=(255, 150, 250, 120), width=5)
    dc.line([0, 120, 760, 300], fill=(120, 244, 255, 90), width=3)
    comp(arr, ovc)

    # equalizer bars (glow + core)
    segs = [((90, 250), (330, 700), "lime"), ((990, 200), (1130, 700), "mag"), ((330, 540), (520, 700), "cyan"), ((1330, 620), (1540, 700), "mag")]
    bcol = {"lime": (204, 255, 61), "mag": (255, 46, 240), "cyan": (0, 229, 255)}
    ov_b = Image.new("RGB", (W, H), (0, 0, 0))
    # draw bars dim first, blur for glow
    ovg2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dgg = ImageDraw.Draw(ovg2)
    bar_clusters = []
    x = 70
    while x < W - 60:
        hgt = np.random.randint(70, 300)
        if 620 < x < 1000:
            hgt = int(hgt * 0.5)  # shorter near center -> clean area
        lab = np.random.choice(["lime", "mag", "cyan"], p=[0.45, 0.35, 0.2])
        top = 700 - hgt
        dgg.rectangle([x, top, x + 30, 700], fill=bcol[lab] + (120,))
        x += 46
    ovg2 = ovg2.filter(ImageFilter.GaussianBlur(16))
    comp(arr, ovg2)
    # crisp cores
    ovb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dbb = ImageDraw.Draw(ovb)
    x = 70
    while x < W - 60:
        hgt = np.random.randint(70, 300)
        if 620 < x < 1000:
            hgt = int(hgt * 0.5)
        lab = np.random.choice(["lime", "mag", "cyan"], p=[0.45, 0.35, 0.2])
        top = 700 - hgt
        dbb.rectangle([x, top, x + 30, 700], fill=bcol[lab][:3] + (235,))
        x += 46
    comp(arr, ovb)

    # crowd silhouettes at bottom
    ovc2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dcd = ImageDraw.Draw(ovc2)
    dcd.polygon([(0, H - 250), (W, H - 250), (W, H + 30), (0, H + 30)], fill=(3, 3, 10, 255))
    for _ in range(170):
        hx2 = np.random.uniform(30, W - 30)
        hy2 = np.random.uniform(H - 250, H - 40)
        rx2 = np.random.uniform(14, 22); ry2 = np.random.uniform(20, 34)
        dcd.ellipse([hx2 - rx2, hy2 - ry2, hx2 + rx2, hy2 + ry2], fill=(8, 8, 18, 255))
    ovc2 = ovc2.filter(ImageFilter.GaussianBlur(5))
    comp(arr, ovc2)

    # floating sparks
    ovs = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(ovs)
    for _ in range(120):
        x = np.random.uniform(0, W); y = np.random.uniform(0, H * 0.75)
        c = bcol[np.random.choice(["lime", "mag", "cyan"])] + (150,)
        s = np.random.uniform(2, 7)
        ds.ellipse([x - s, y - s, x + s, y + s], fill=c)
    ovs = ovs.filter(ImageFilter.GaussianBlur(3))
    comp(arr, ovs)
    vignette_edges(arr, strength=0.42, inner=0.55)
    return arr

# ----------------------------------------------------------------------------
# 4) PASTEL LO-FI
# ----------------------------------------------------------------------------
def blob_cluster(draw, cx, cy, rad, color, n=7, jx=1.6, jy=1.1):
    # puffy lumpy cloud blob
    pts = []
    for i in range(n):
        a = i / n * 2 * np.pi + np.random.uniform(0, 0.4)
        r = rad * np.random.uniform(0.72, 1.0)
        pts.append((cx + np.cos(a) * r * jx, cy + np.sin(a) * r * jy))
    # draw overlapping circles instead (puffy)
    for i in range(n):
        a = i / n * 2 * np.pi
        r = rad * np.random.uniform(0.55, 1.0)
        draw.ellipse([cx + np.cos(a) * r * 0.9 - r, cy + np.sin(a) * r * 0.75 - r, cx + np.cos(a) * r * 0.9 + r, cy + np.sin(a) * r * 0.75 + r], fill=color)

def gen_lofi():
    arr = np.zeros((H, W, 3), dtype=np.float32)
    arr[:] = np.asarray([1.0, 0.955, 0.905], np.float32)          # cream
    # very soft sky wash at top
    glow(arr, W * 0.5, H * 0.0, 900, 300, (0.90, 0.95, 1.0), 0.55)

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    clouds = [
        (W * 0.18, H * 0.20, 150, (255, 214, 232, 235)),   # blush
        (W * 0.72, H * 0.15, 120, (207, 227, 255, 235)),   # pastel blue
        (W * 0.62, H * 0.38, 150, (255, 217, 184, 235)),   # peach
        (W * 0.82, H * 0.55, 135, (230, 215, 255, 235)),   # lavender
        (W * 0.30, H * 0.10, 95, (255, 228, 240, 220)),    # small blush
        (W * 0.5, H * 0.30, 110, (255, 240, 224, 215)),    # cream-peach
        (W * 0.10, H * 0.52, 110, (214, 232, 252, 215)),   # faint blue
        (W * 0.5, H * 0.14, 80, (255, 232, 210, 205)),
    ]
    dv = ImageDraw.Draw(ov)
    for (cx, cy, r, col) in clouds:
        blob_cluster(dv, cx, cy, r, col, n=8)
    ov = ov.filter(ImageFilter.GaussianBlur(16))
    comp(arr, ov)
    # soft sun
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.hypot(xx - W * 0.5, yy - H * 0.62)
    sun = np.exp(-(d / 190.0) ** 2) * 0.9
    arr += sun[..., None] * np.array([1.0, 0.86, 0.72], np.float32) * 0.9
    # rolling pastel hills at bottom
    ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dv2 = ImageDraw.Draw(ov2)
    dvg = ImageDraw.Draw(ov2)
    # far hill lavender
    pts = [(x, H + 20 - 40 * np.sin(x * 0.004) - 22 * np.sin(x * 0.011 + 2.0)) for x in range(0, W, 6)]
    dvg.polygon(pts + [(W, H + 40), (0, H + 40)], fill=(232, 214, 250, 255))
    pts2 = [(x, H + 20 - 28 * np.sin(x * 0.006 + 0.5)) for x in range(0, W, 6)]
    dv2.polygon(pts2 + [(W, H + 40), (0, H + 40)], fill=(255, 224, 216, 255))
    ov2 = ov2.filter(ImageFilter.GaussianBlur(26))
    comp(arr, ov2)

    # tiny calm details: tiny dots / sparkles / a small laptop+cactus silhouette
    ov3 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dv3 = ImageDraw.Draw(ov3)
    for (x, y, s) in [(W * 0.14, H * 0.40, 4), (W * 0.86, H * 0.30, 3), (W * 0.5, H * 0.42, 3.5), (W * 0.25, H * 0.58, 3)]:
        c = (255, 255, 255, 190)
        dv3.ellipse([x - s, y - s, x + s, y + s], fill=c)
    # little round plant on the hill
    px0, py0 = W * 0.5, H * 0.80
    dv3.rounded_rectangle([px0 - 40, py0 - 30, px0 + 40, py0 + 34], radius=12, fill=(255, 255, 255, 235))
    dv3.rectangle([px0 - 40, py0 - 34, px0 - 24, py0 - 6], fill=(120, 168, 140, 235))
    dv3.rectangle([px0 + 24, py0 - 44, px0 + 40, py0 - 16], fill=(148, 196, 156, 235))
    dv3.rounded_rectangle([px0 - 34, py0 - 30, px0 + 34, py0 + 32], radius=10, fill=(186, 214, 190, 245))
    ov3 = ov3.filter(ImageFilter.GaussianBlur(4))
    comp(arr, ov3)
    grain(arr, 0.02)
    return arr

# ----------------------------------------------------------------------------
# 5) 8-BIT ARCADE
# ----------------------------------------------------------------------------
def draw_note_pixel(pX, pY, scale, color, d):
    # chunky 8-bit musical note (filled blocks)
    s = scale
    # stem + head
    for dy in range(0, 9):
        d.rectangle([pX + 2 * s, pY + dy * s, pX + 3 * s, pY + dy * s + s], fill=color)
    d.rectangle([pX, pY + 8 * s, pX + 4 * s, pY + 13 * s], fill=color)
    d.rectangle([pX + 4 * s, pY + 9 * s, pX + 7 * s, pY + 12 * s], fill=color)
    # flag
    d.rectangle([pX + 3 * s, pY, pX + 6 * s, pY + s], fill=color)
    d.rectangle([pX + 4 * s, pY + s, pX + 7 * s, pY + 2 * s], fill=color)
    d.rectangle([pX + 5 * s, pY + 2 * s, pX + 6 * s, pY + 3 * s], fill=color)

def gen_arcade():
    arr = np.zeros((H, W, 3), dtype=np.float32)
    t = np.linspace(0, 1, H)[:, None, None].astype(np.float32)
    arr[:] = np.asarray([0.055, 0.05, 0.17], np.float32) * (1 - t) + np.asarray([0.02, 0.015, 0.09], np.float32) * t
    glow(arr, W / 2, H * 0.4, 1500, 700, (0.10, 0.06, 0.32), 0.6)

    # starfield (pixel squares)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(ov)
    cols = [(255, 255, 255, 255), (120, 230, 255, 255), (255, 120, 240, 240), (200, 255, 160, 200)]
    for _ in range(230):
        x = np.random.uniform(0, W); y = np.random.uniform(0, H * 0.86)
        s = np.random.choice([1, 1, 2, 3, 3])
        ds.rectangle([x, y, x + s, y + s], fill=cols[np.random.randint(0, len(cols))])
    comp(arr, ov)

    # synthwave horizon + grid
    ovg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dg = ImageDraw.Draw(ovg)
    hx, hy = W / 2, 820
    for k in range(-22, 23):
        x0 = hx + k * 8
        dg.line([x0, hy, hx + k * 320, H + 40], fill=(70, 70, 200, 60), width=3)
    for i in range(1, 12):
        y = hy + (i ** 1.8) * 3.5
        dg.line([W * 0.04, y, W * 0.96, y], fill=(120, 90, 230, 70), width=3)
    comp(arr, ovg)

    # ---- pixel arcade machine ----
    ps = 7  # pixel scale
    pw, ph = 108, 142
    small = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    mfx, mfy = int(W / 2 - pw * ps / 2), H - ph * ps
    dA = ImageDraw.Draw(small)
    NAVY = (16, 14, 46, 255)
    NAVY2 = (13, 12, 40, 255)
    # cabinet body
    dA.rectangle([8, 20, pw - 8, ph], fill=NAVY)
    dA.rectangle([8, 20, 13, ph], fill=(38, 40, 92, 255))
    dA.rectangle([pw - 13, 20, pw - 8, ph], fill=(38, 40, 92, 255))
    # marquee
    dA.rectangle([12, 24, pw - 12, 44], fill=(26, 8, 52, 255), outline=(120, 40, 140, 255), width=3)
    # screen
    dA.rectangle([20, 52, pw - 20, 104], fill=(6, 6, 26, 255), outline=(60, 40, 110, 255), width=3)
    # screen content: mini pixel eq bars
    sc = 6
    levels = [9, 13, 6, 16, 10, 5, 12]
    for i, lv in enumerate(levels):
        for j in range(lv):
            col = (255, 46, 240, 255)
            if i % 3 == 0: col = (0, 229, 255, 255)
            if i % 3 == 1: col = (204, 255, 61, 255)
            dA.rectangle([24 + 6 + i * 10, 96 - 6 - j * 10, 24 + 6 + i * 10 + 6, 96 - 6 - j * 10 + 6], fill=col)
    # control panel
    dA.rectangle([16, 108, pw - 16, 124], fill=(40, 22, 84, 255), outline=(140, 90, 220, 255), width=3)
    dA.rectangle([18, 110, pw - 18, 122], fill=(22, 12, 50, 255))
    for bx in (26, 44, 62, 80, 98):
        dA.rectangle([bx, 112, bx + 8, 120], fill=(220, 60, 200, 255))
    dA.rectangle([70, 106, 86, 118], fill=(90, 40, 120, 255), outline=(255, 90, 220, 255), width=2)
    # coin door
    dA.rectangle([46, 128, 62, 142], outline=(150, 120, 240, 255), width=3)
    dA.rectangle([49, 122, 59, 128], fill=(255, 60, 210, 255))
    # marquee glow behind machine (additive)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    mg = np.exp(-0.5 * (((xx - (mfx + pw * ps / 2)) / (pw * ps * 0.8)) ** 2 + ((yy - (mfy + 30)) / 200) ** 2)) * 0.55
    arr = arr + mg[..., None] * np.array([0.55, 0.18, 0.9], np.float32)
    # marquee neon text (pixelated via small canvas text)
    f = load_font(15)
    txt = "MUSIC"
    tw = dA.textlength(txt, font=f)
    dA.text((pw / 2 - tw / 2, 27), txt, font=f, fill=(0, 229, 255, 255))
    dA.text((pw / 2 - tw / 2 - 2, 27), txt, font=f, fill=(255, 60, 240, 255))  # magenta glow layer
    big = small.resize((pw * ps, ph * ps), Image.NEAREST)
    comp(arr, big, mfx, mfy)
    # re-crisp marquee letters overlay (crisp center of marquee)
    big2 = small.copy()
    d2b = ImageDraw.Draw(big2)
    f2 = load_font(15)
    d2b.text((pw / 2 - tw / 2, 27), txt, font=f2, fill=(170, 245, 255, 255))
    # marquee top neon pill notes
    draw_note_pixel(pw - 24, 15, 2, (204, 255, 61, 255), d2b)
    draw_note_pixel(12, 17, 2, (120, 240, 255, 255), d2b)
    bigc = big2.resize((pw * ps, ph * ps), Image.NEAREST)
    comp(arr, bigc, mfx, mfy)

    # floating pixel notes (drawn directly large, chunky)
    ovn = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dn = ImageDraw.Draw(ovn)
    draw_note_pixel(250, H - 420, 8, (204, 255, 61, 240), dn)
    draw_note_pixel(W - 330, H - 620, 9, (255, 46, 240, 235), dn)
    draw_note_pixel(W - 250, H - 300, 6, (0, 229, 255, 235), dn)
    draw_note_pixel(240, H * 0.66, 6, (120, 200, 255, 220), dn)
    draw_note_pixel(150, H * 0.52, 5, (255, 120, 200, 220), dn)
    dn.rectangle([W - 700, H - 60, W - 660, H - 20], fill=(255, 46, 240, 240))
    dn.rectangle([W - 500, H - 120, W - 470, H - 90], fill=(0, 229, 255, 240))
    comp(arr, ovn)
    vignette_edges(arr, strength=0.35, inner=0.6)
    grain(arr, 0.03)
    return arr

def main():
    outdir = "/home/tom/Documents/deepseek-harness/default-workspace/youtube-playlist-downloader/designs/backgrounds"
    jobs = [
        ("bg1-vinyl.png", gen_vinyl),
        ("bg2-cassette.png", gen_sunset),
        ("bg3-neon.png", gen_neon),
        ("bg4-lofi.png", gen_lofi),
        ("bg5-arcade.png", gen_arcade),
    ]
    for fname, fn in jobs:
        np.random.seed(hash(fname) & 0xFFFFFFFF)
        arr = fn()
        img = to_img(arr)
        path = os.path.join(outdir, fname)
        img.save(path, "PNG")
        size = os.path.getsize(path)
        magic = open(path, "rb").read(8)
        print(f"{fname}: {size} bytes, magic={magic}")

if __name__ == "__main__":
    main()
