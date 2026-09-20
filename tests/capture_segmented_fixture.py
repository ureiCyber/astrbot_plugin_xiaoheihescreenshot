"""Local Chromium smoke test for DPR2 capture and safe segmented stitching."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

from capture_sample import install_astrbot_stubs


def load_plugin_module():
    install_astrbot_stubs()
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("xiaoheihe_segment_fixture", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_html(colors: list[tuple[int, int, int]], band_height: int) -> str:
    bands = "".join(
        f'<section data-index="{index + 1}" '
        f'style="height:{band_height}px;background:rgb{colour};"></section>'
        for index, colour in enumerate(colors)
    )
    return '<!doctype html><style>html,body{margin:0}</style>' + bands


def _assert_band_positions(
    image: Image.Image,
    colors: list[tuple[int, int, int]],
    band_height: int,
    total_css_height: int,
) -> None:
    rgb_image = image.convert("RGB")
    sample_x = max(0, image.width // 4)
    for index, expected in enumerate(colors):
        css_center = (index + 0.5) * band_height
        sample_y = min(
            image.height - 1,
            round(css_center * image.height / total_css_height),
        )
        actual = rgb_image.getpixel((sample_x, sample_y))
        if max(abs(actual[channel] - expected[channel]) for channel in range(3)) > 18:
            raise AssertionError(
                f"band {index + 1} is misplaced or altered: "
                f"expected {expected} near y={sample_y}, got {actual}"
            )


async def run(output: Path, chrome: Path) -> None:
    module = load_plugin_module()
    plugin_class = module.XiaoheihePlugin
    dpr = module.DEVICE_SCALE_FACTOR
    css_width = 430

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(chrome),
        )
        context = await browser.new_context(
            viewport={"width": css_width, "height": 932},
            device_scale_factor=dpr,
        )
        page = await context.new_page()
        plugin = plugin_class.__new__(plugin_class)
        plugin.debug = True
        try:
            colors = [
                (211, 47, 47),
                (56, 142, 60),
                (25, 118, 210),
                (245, 124, 0),
                (123, 31, 162),
                (0, 137, 123),
                (194, 24, 91),
                (104, 159, 56),
                (48, 63, 159),
                (230, 74, 25),
                (81, 45, 168),
                (0, 151, 167),
                (175, 180, 43),
                (93, 64, 55),
                (69, 90, 100),
                (198, 40, 40),
                (46, 125, 50),
                (21, 101, 192),
            ]
            page_info = await page.evaluate(
                "() => ({width: window.innerWidth, dpr: window.devicePixelRatio})"
            )
            if page_info != {"width": css_width, "dpr": dpr}:
                raise AssertionError(f"unexpected page geometry: {page_info}")

            short_band_height = 400
            short_css_height = 3 * short_band_height
            await page.set_content(
                _fixture_html(colors[:3], short_band_height),
                wait_until="load",
            )
            short_png = await plugin._capture_segmented_screenshot(
                page,
                content_height=short_css_height,
                css_width=css_width,
                segment_height=2000,
            )
            short_jpeg = plugin._normalize_for_qq(short_png)
            with tempfile.TemporaryDirectory(prefix="xiaoheihe-short-capture-") as temp_dir:
                short_output = Path(temp_dir) / "short-dpr2.jpg"
                short_output.write_bytes(short_jpeg)
                if short_output.stat().st_size > module.MAX_IMAGE_BYTES:
                    raise AssertionError("short page exceeded the JPEG byte limit")
                with Image.open(short_output) as image:
                    if image.format != "JPEG":
                        raise AssertionError(f"expected JPEG, got {image.format}")
                    if image.size != (css_width * dpr, short_css_height * dpr):
                        raise AssertionError(
                            f"short page unexpectedly scaled: {image.size}"
                        )
                    _assert_band_positions(
                        image,
                        colors[:3],
                        short_band_height,
                        short_css_height,
                    )
                    print(
                        f"SHORT {image.width}x{image.height} DPR={dpr} "
                        f"bands=3 output={short_output}",
                        flush=True,
                    )

            long_band_height = 628
            long_css_height = len(colors) * long_band_height
            await page.set_content(
                _fixture_html(colors, long_band_height),
                wait_until="load",
            )
            long_png = await plugin._capture_segmented_screenshot(
                page,
                content_height=long_css_height,
                css_width=css_width,
                segment_height=2000,
            )
            long_jpeg = plugin._normalize_for_qq(long_png)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(long_jpeg)
            if output.stat().st_size > module.MAX_IMAGE_BYTES:
                raise AssertionError("long page exceeded the JPEG byte limit")

            with Image.open(output) as image:
                expected_size = plugin_class._safe_image_size(
                    css_width * dpr,
                    long_css_height * dpr,
                )
                if image.format != "JPEG":
                    raise AssertionError(f"expected JPEG, got {image.format}")
                if image.size != expected_size:
                    raise AssertionError(
                        f"long page dimensions {image.size} != safe size {expected_size}"
                    )
                if max(image.size) > module.MAX_IMAGE_DIMENSION:
                    raise AssertionError(f"maximum edge lock exceeded: {image.size}")
                if image.width * image.height > module.MAX_IMAGE_PIXELS:
                    raise AssertionError(f"pixel-count lock exceeded: {image.size}")
                if image.height != module.MAX_IMAGE_DIMENSION:
                    raise AssertionError(
                        f"long fixture should trigger the edge lock: {image.size}"
                    )
                if image.width >= css_width * dpr:
                    raise AssertionError(
                        f"long fixture was not scaled at its safety boundary: {image.size}"
                    )
                _assert_band_positions(
                    image,
                    colors,
                    long_band_height,
                    long_css_height,
                )
                print(
                    f"LONG {image.width}x{image.height} DPR={dpr} "
                    f"bands={len(colors)} bytes={output.stat().st_size} output={output}",
                    flush=True,
                )
        finally:
            await context.close()
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--chrome",
        type=Path,
        default=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    )
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.chrome))


if __name__ == "__main__":
    main()
