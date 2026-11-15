"""把方形图标裁成圆形，四角透明，并生成 ico / favicon / avatar。

用法：.venv/Scripts/python.exe scripts/make_round_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent.parent
SOURCE = BASE / "assets" / "Anime_app_icon__chibi_style_bu_2026-11-13T11-34-25.png"
ROUND_PNG = BASE / "assets" / "icon_round.png"
ICO_PATH = BASE / "assets" / "icon.ico"
FAVICON = BASE / "static" / "favicon.png"
AVATAR = BASE / "static" / "avatar.png"

SIZE = 1024
SCALE = 4  # 超采样倍数，让圆形边缘平滑无锯齿


def circular(image: Image.Image, size: int = SIZE) -> Image.Image:
    """居中裁成正方形后套圆形蒙版，圆外完全透明。"""
    image = image.convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize(
        (size * SCALE, size * SCALE), Image.LANCZOS
    )

    mask = Image.new("L", (size * SCALE, size * SCALE), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size * SCALE - 1, size * SCALE - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(image.resize((size, size), Image.LANCZOS), (0, 0), mask)
    return result


def main() -> None:
    source = Image.open(SOURCE)
    round_icon = circular(source)

    ROUND_PNG.parent.mkdir(parents=True, exist_ok=True)
    round_icon.save(ROUND_PNG)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    round_icon.save(ICO_PATH, format="ICO", sizes=sizes)

    round_icon.resize((256, 256), Image.LANCZOS).save(FAVICON)
    round_icon.resize((192, 192), Image.LANCZOS).save(AVATAR)

    print("round png:", ROUND_PNG)
    print("ico:", ICO_PATH)
    print("favicon:", FAVICON)
    print("avatar:", AVATAR)

    # 校验四角确实透明
    corner = round_icon.getpixel((2, 2))
    center = round_icon.getpixel((SIZE // 2, SIZE // 2))
    print("corner alpha:", corner[3], "center alpha:", center[3])


if __name__ == "__main__":
    main()
