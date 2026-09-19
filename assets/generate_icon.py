#!/usr/bin/env python3
"""
Generates high-resolution application icons (assets/icon.png and assets/icon.svg)
for the YouTube Video Pipeline Orchestrator.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter
import math

ROOT_DIR = Path(__file__).resolve().parent
if ROOT_DIR.name == "assets":
    ASSETS_DIR = ROOT_DIR
else:
    ASSETS_DIR = ROOT_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

SVG_PATH = ASSETS_DIR / "icon.svg"
PNG_PATH = ASSETS_DIR / "icon.png"

# 1. Create SVG Icon
SVG_CONTENT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FF2E44"/>
      <stop offset="55%" stop-color="#E60023"/>
      <stop offset="100%" stop-color="#990014"/>
    </linearGradient>
    <linearGradient id="screenGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#242731"/>
      <stop offset="100%" stop-color="#14161C"/>
    </linearGradient>
    <linearGradient id="playGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="100%" stop-color="#E6E8EE"/>
    </linearGradient>
    <linearGradient id="pipeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#FF334B"/>
      <stop offset="35%" stop-color="#FF9900"/>
      <stop offset="70%" stop-color="#00D26A"/>
      <stop offset="100%" stop-color="#00C8FF"/>
    </linearGradient>
    <filter id="shadow" x="-10%" y="-10%" width="130%" height="130%">
      <feDropShadow dx="0" dy="16" stdDeviation="20" flood-color="#000000" flood-opacity="0.45"/>
    </filter>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="4" stdDeviation="8" flood-color="#000000" flood-opacity="0.3"/>
    </filter>
  </defs>

  <!-- Background Base with Shadow -->
  <rect x="36" y="36" width="440" height="440" rx="96" fill="url(#bgGrad)" filter="url(#shadow)"/>

  <!-- Top Glass Highlight -->
  <path d="M 132 36 L 380 36 C 430 36 476 82 476 132 L 476 180 C 420 120 300 90 132 90 C 80 90 45 105 36 120 L 36 132 C 36 82 82 36 132 36 Z" fill="#ffffff" opacity="0.18"/>

  <!-- Inner Studio Screen -->
  <rect x="76" y="82" width="360" height="348" rx="48" fill="url(#screenGrad)" stroke="#373B47" stroke-width="3"/>

  <!-- Filmstrip perforations (top) -->
  <rect x="98"  y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="136" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="174" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="212" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="250" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="288" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="326" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>
  <rect x="364" y="98" width="22" height="15" rx="4" fill="#ffffff" opacity="0.25"/>

  <!-- Central Play Button (YouTube Icon) -->
  <polygon points="214,180 326,248 214,316" fill="url(#playGrad)" filter="url(#glow)"/>

  <!-- Pipeline Flow Line at Bottom -->
  <line x1="110" y1="382" x2="402" y2="382" stroke="url(#pipeGrad)" stroke-width="5" stroke-linecap="round"/>

  <!-- Pipeline Stage Indicator Dots -->
  <circle cx="125" cy="382" r="7" fill="#FF2E44"/>
  <circle cx="180" cy="382" r="7" fill="#FF7700"/>
  <circle cx="235" cy="382" r="7" fill="#FFC800"/>
  <circle cx="290" cy="382" r="7" fill="#00D26A"/>
  <circle cx="345" cy="382" r="7" fill="#00A8FF"/>
  <circle cx="390" cy="382" r="9" fill="#00E5FF" stroke="#FFFFFF" stroke-width="2"/>

  <!-- Magic Sparkle (AI Generation) -->
  <path d="M 370 148 L 376 164 L 392 170 L 376 176 L 370 192 L 364 176 L 348 170 L 364 164 Z" fill="#FFD700"/>
  <path d="M 334 136 L 337 144 L 345 147 L 337 150 L 334 158 L 331 150 L 323 147 L 331 144 Z" fill="#FFFFFF" opacity="0.9"/>
</svg>
"""

SVG_PATH.write_text(SVG_CONTENT.strip() + "\n", encoding="utf-8")


# 2. Render High-Resolution 512x512 PNG using Pillow with 2x Supersampling (1024x1024 -> 512x512)
SCALE = 2
SIZE = 512 * SCALE
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

def s(val):
    return int(val * SCALE)

# Outer Shadow
shadow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
sdraw = ImageDraw.Draw(shadow_layer)
sdraw.rounded_rectangle(
    [s(36), s(48), s(476), s(488)],
    radius=s(96),
    fill=(0, 0, 0, 140)
)
shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(s(16)))
img = Image.alpha_composite(img, shadow_layer)
draw = ImageDraw.Draw(img)

# Background Squircle Gradient (Red gradient)
# Top-Left (#FF2E44) to Bottom-Right (#990014)
r1, g1, b1 = 255, 46, 68
r2, g2, b2 = 153, 0, 20

bg_mask = Image.new("L", (SIZE, SIZE), 0)
bmdraw = ImageDraw.Draw(bg_mask)
bmdraw.rounded_rectangle(
    [s(36), s(36), s(476), s(476)],
    radius=s(96),
    fill=255
)

bg_gradient = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
for y in range(s(36), s(476)):
    ratio = (y - s(36)) / (s(440))
    r = int(r1 + (r2 - r1) * ratio)
    g = int(g1 + (g2 - g1) * ratio)
    b = int(b1 + (b2 - b1) * ratio)
    for x in range(s(36), s(476)):
        diag = ((x - s(36)) + (y - s(36))) / (s(880))
        cr = max(0, min(255, int(r1 + (r2 - r1) * diag)))
        cg = max(0, min(255, int(g1 + (g2 - g1) * diag)))
        cb = max(0, min(255, int(b1 + (b2 - b1) * diag)))
        bg_gradient.putpixel((x, y), (cr, cg, cb, 255))

img.paste(bg_gradient, (0, 0), bg_mask)

# Top Glass Highlight Arc
highlight_mask = Image.new("L", (SIZE, SIZE), 0)
hdraw = ImageDraw.Draw(highlight_mask)
hdraw.rounded_rectangle(
    [s(40), s(40), s(472), s(220)],
    radius=s(92),
    fill=45
)
hdraw.rectangle([s(40), s(140), s(472), s(220)], fill=0)
highlight_layer = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
img.paste(highlight_layer, (0, 0), highlight_mask)

# Inner Dark Screen
screen_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
scdraw = ImageDraw.Draw(screen_layer)
scdraw.rounded_rectangle(
    [s(76), s(82), s(436), s(430)],
    radius=s(48),
    fill=(22, 24, 30, 255),
    outline=(65, 70, 85, 255),
    width=s(3)
)
img = Image.alpha_composite(img, screen_layer)
draw = ImageDraw.Draw(img)

# Filmstrip Perforations at Top
perf_x_starts = [98, 136, 174, 212, 250, 288, 326, 364]
for px in perf_x_starts:
    draw.rounded_rectangle(
        [s(px), s(98), s(px + 22), s(113)],
        radius=s(4),
        fill=(255, 255, 255, 65)
    )

# Play Button Drop Shadow
play_shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
psdraw = ImageDraw.Draw(play_shadow)
psdraw.polygon(
    [(s(214), s(183)), (s(328), s(251)), (s(214), s(319))],
    fill=(0, 0, 0, 90)
)
play_shadow = play_shadow.filter(ImageFilter.GaussianBlur(s(6)))
img = Image.alpha_composite(img, play_shadow)
draw = ImageDraw.Draw(img)

# Play Triangle (Crisp White/Off-White)
draw.polygon(
    [(s(214), s(180)), (s(326), s(248)), (s(214), s(316))],
    fill=(255, 255, 255, 255)
)

# Pipeline Flow Line (Gradient colors from Red -> Orange -> Yellow -> Green -> Cyan)
points = [
    (125, (255, 46, 68)),
    (180, (255, 119, 0)),
    (235, (255, 200, 0)),
    (290, (0, 210, 106)),
    (345, (0, 168, 255)),
    (390, (0, 229, 255))
]

# Connecting line segments
for i in range(len(points) - 1):
    x_start, c_start = points[i]
    x_end, c_end = points[i + 1]
    draw.line(
        [(s(x_start), s(382)), (s(x_end), s(382))],
        fill=(*c_end, 200),
        width=s(5)
    )

# Dots on pipeline
for x, col in points:
    r = 7
    if x == 390:
        r = 10
        draw.ellipse([s(x - r), s(382 - r), s(x + r), s(382 + r)], fill=(*col, 255), outline=(255, 255, 255, 255), width=s(2))
    else:
        draw.ellipse([s(x - r), s(382 - r), s(x + r), s(382 + r)], fill=(*col, 255))

# Magic Sparkle Star helper
def draw_star(cx, cy, r_outer, r_inner, color):
    poly = []
    for i in range(8):
        angle = i * (math.pi / 4) - (math.pi / 2)
        r = r_outer if i % 2 == 0 else r_inner
        poly.append((s(cx + r * math.cos(angle)), s(cy + r * math.sin(angle))))
    draw.polygon(poly, fill=color)

draw_star(370, 170, 22, 6, (255, 215, 0, 255))
draw_star(334, 147, 12, 3, (255, 255, 255, 230))

# Resize down to 512x512 with LANCZOS for super crisp anti-aliasing
final_img = img.resize((512, 512), Image.Resampling.LANCZOS)
final_img.save(PNG_PATH, "PNG")

print(f"Generated {SVG_PATH}")
print(f"Generated {PNG_PATH} ({final_img.size[0]}x{final_img.size[1]})")
