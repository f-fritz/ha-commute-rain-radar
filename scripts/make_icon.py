"""Draw the integration icon: a 4x4 rain matrix in the heatmap colours.

Writes icon.png (256 px) and icon@2x.png (512 px) to
`custom_components/commute_rain/brand/`.
"""

from pathlib import Path

from PIL import Image, ImageDraw

from commute_rain.plots import RAIN_CLASSES

ROOT = Path(__file__).resolve().parent.parent
DST = ROOT / "custom_components" / "commute_rain" / "brand"

# Rain class per cell, top row = end of the ride. A band moving across the
# route, sloping up-left as in the real heatmap.
CELLS = [
    [4, 2, 0, 0],
    [3, 4, 2, 0],
    [1, 3, 4, 2],
    [0, 1, 3, 4],
]
SUPERSAMPLE = 4


def draw(size: int) -> Image.Image:
    s = size * SUPERSAMPLE
    gap = s * 0.04
    cell = (s - 5 * gap) / 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for r, row in enumerate(CELLS):
        for c, cls in enumerate(row):
            x0, y0 = gap + c * (cell + gap), gap + r * (cell + gap)
            d.rounded_rectangle((x0, y0, x0 + cell, y0 + cell), radius=cell * 0.18, fill=RAIN_CLASSES[cls][3])
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    draw(256).save(DST / "icon.png")
    draw(512).save(DST / "icon@2x.png")
    print(f"wrote icons to {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
