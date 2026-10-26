"""The app icon, drawn in code.

Drawn rather than shipped as a file so the window and the build use one source:
the window renders it at runtime for its title bar, and the build script writes the
same drawing out as a multi-size .ico / .png for the executable.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

BACKGROUND = (22, 101, 92)  # deep teal
AWNING = (255, 255, 255)
AWNING_ALT = (242, 170, 76)  # warm amber stripe


def render(size: int = 256) -> Image.Image:
    """A market stall: striped scalloped awning over a counter, on a rounded tile."""
    scale = 4  # draw large, then downsample, for smooth edges at every size
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=BACKGROUND)

    left, right = int(s * 0.16), int(s * 0.84)
    top, bottom = int(s * 0.24), int(s * 0.44)
    stripes = 5
    width = (right - left) / stripes
    for i in range(stripes):
        x0 = left + i * width
        colour = AWNING if i % 2 == 0 else AWNING_ALT
        draw.rectangle((x0, top, x0 + width, bottom), fill=colour)
        # Scallop hanging under each stripe.
        draw.pieslice(
            (x0, bottom - width / 2, x0 + width, bottom + width / 2), 0, 180, fill=colour
        )

    # Posts and counter.
    post = int(s * 0.035)
    counter_top = int(s * 0.64)
    draw.rectangle((left + post, bottom, left + 2 * post, counter_top), fill=AWNING)
    draw.rectangle((right - 2 * post, bottom, right - post, counter_top), fill=AWNING)
    draw.rounded_rectangle(
        (left, counter_top, right, int(s * 0.78)), radius=int(s * 0.02), fill=AWNING
    )
    return image.resize((size, size), Image.LANCZOS)


def write_ico(path: str) -> None:
    """Every size Windows asks for, in one file."""
    render(256).save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
