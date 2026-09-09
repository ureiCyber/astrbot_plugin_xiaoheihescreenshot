"""Live Chromium smoke test for segmented tall-page capture."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

from capture_sample import install_astrbot_stubs


def load_plugin():
    install_astrbot_stubs()
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("xiaoheihe_segment_fixture", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.XiaoheihePlugin


async def run(output: Path, chrome: Path) -> None:
    plugin_class = load_plugin()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(chrome),
        )
        page = await browser.new_page(viewport={"width": 430, "height": 932})
        try:
            colours = [
                f"hsl({round(index * 360 / 18)} 70% 48%)"
                for index in range(18)
            ]
            bands = "".join(
                f'<section data-index="{index + 1}" '
                f'style="height:628px;background:{colour};color:white;'
                'font:700 64px sans-serif;display:grid;place-items:center">'
                f'{index + 1}</section>'
                for index, colour in enumerate(colours)
            )
            await page.set_content(
                '<!doctype html><style>html,body{margin:0}</style>' + bands,
                wait_until="load",
            )
            plugin = plugin_class.__new__(plugin_class)
            plugin.image_quality = 92
            plugin.debug = True
            image_bytes = await plugin._capture_segmented_screenshot(
                page,
                content_height=18 * 628,
                css_width=430,
                segment_height=2000,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(image_bytes)

            with Image.open(io.BytesIO(image_bytes)) as image:
                image = image.convert("RGB")
                samples = [image.getpixel((20, index * 628 + 314)) for index in range(18)]
                blank = [
                    index + 1
                    for index, (red, green, blue) in enumerate(samples)
                    if red > 245 and green > 245 and blue > 245
                ]
                print(
                    f"FIXTURE {image.width}x{image.height} "
                    f"bands=18 blank={blank} output={output}",
                    flush=True,
                )
        finally:
            await page.close()
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
