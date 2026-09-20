"""Manual live-page smoke test for the Xiaoheihe screenshot pipeline."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright


def install_astrbot_stubs() -> None:
    def decorator(*_args, **_kwargs):
        def wrap(function):
            return function
        return wrap

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    event.filter = types.SimpleNamespace(
        command=decorator,
        event_message_type=decorator,
        EventMessageType=types.SimpleNamespace(ALL="ALL"),
    )
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {"__init__": lambda self, *_a, **_k: None})
    api.logger = types.SimpleNamespace(
        info=lambda message: print(f"INFO {message}", flush=True),
        warning=lambda message: print(f"WARN {message}", flush=True),
        error=lambda message: print(f"ERROR {message}", flush=True),
    )
    api.AstrBotConfig = dict
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
    })


def load_plugin():
    install_astrbot_stubs()
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("xiaoheihe_capture_sample", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def capture(url: str, output: Path, chrome: Path) -> None:
    module = load_plugin()
    plugin_class = module.XiaoheihePlugin
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(chrome),
        )
        plugin = plugin_class.__new__(plugin_class)
        plugin.cookies = ""
        plugin.debug = True
        plugin._browser = browser

        context = await browser.new_context(
            viewport={"width": 430, "height": 932},
            device_scale_factor=module.DEVICE_SCALE_FACTOR,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
                "Mobile/15E148 Safari/604.1"
            ),
            is_mobile=True,
            has_touch=True,
        )
        try:
            page = await context.new_page()
            state = await plugin._open_link_page_with_capture(page, url)
            image_bytes = await plugin._prepare_and_screenshot(page, state)
            image_bytes = plugin._normalize_for_qq(image_bytes)
            diagnostics = await page.evaluate(
                """() => {
                    const slides = Array.from(document.querySelectorAll(
                        '.bbs-link-img-slider .swiper-slide, '
                        + '.bbs-link-img-slider-box .swiper-slide'
                    ));
                    const details = slides.map((slide, offset) => {
                        const layer = slide.querySelector('.bbs-link-img-slider-item');
                        const image = slide.querySelector('img');
                        const rect = slide.getBoundingClientRect();
                        const imageRect = image?.getBoundingClientRect();
                        const background = layer
                            ? getComputedStyle(layer).backgroundImage
                            : 'none';
                        return {
                            index: offset + 1,
                            synthetic: slide.dataset.astrbotSynthetic === 'true',
                            loadedImage: Boolean(image && image.complete && image.naturalWidth > 0),
                            hasBackground: Boolean(background && background !== 'none'),
                            top: Math.round(rect.top + window.scrollY),
                            bottom: Math.round(rect.bottom + window.scrollY),
                            height: Math.round(rect.height),
                            imageTop: imageRect
                                ? Math.round(imageRect.top + window.scrollY)
                                : null,
                            imageHeight: imageRect ? Math.round(imageRect.height) : null,
                        };
                    });
                    const slider = document.querySelector('.bbs-link-img-slider');
                    const box = slider?.querySelector('.bbs-link-img-slider-box');
                    const wrapper = box?.querySelector('.swiper-wrapper');
                    const rectInfo = element => {
                        if (!element) return null;
                        const rect = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        return {
                            top: Math.round(rect.top + window.scrollY),
                            bottom: Math.round(rect.bottom + window.scrollY),
                            height: Math.round(rect.height),
                            overflow: style.overflow,
                            position: style.position,
                        };
                    };
                    const visibleElements = Array.from(document.querySelectorAll('body *'))
                        .map(element => {
                            const rect = element.getBoundingClientRect();
                            const style = getComputedStyle(element);
                            const directText = Array.from(element.childNodes)
                                .filter(node => node.nodeType === Node.TEXT_NODE)
                                .map(node => node.textContent || '')
                                .join(' ')
                                .trim();
                            const tag = element.tagName.toLowerCase();
                            const semanticPaint = [
                                'img', 'svg', 'canvas', 'video', 'iframe', 'input',
                                'button', 'hr'
                            ].includes(tag)
                                || style.backgroundImage !== 'none'
                                || directText.length > 0;
                            return {
                                tag,
                                className: typeof element.className === 'string'
                                    ? element.className.slice(0, 160)
                                    : '',
                                top: Math.round(rect.top + window.scrollY),
                                bottom: Math.round(rect.bottom + window.scrollY),
                                height: Math.round(rect.height),
                                directText: directText.slice(0, 80),
                                semanticPaint,
                                display: style.display,
                                visibility: style.visibility,
                                opacity: style.opacity,
                                minHeight: style.minHeight,
                                position: style.position,
                            };
                        })
                        .filter(item => item.display !== 'none'
                            && item.visibility !== 'hidden'
                            && item.opacity !== '0'
                            && item.height > 0);
                    const byBottom = [...visibleElements]
                        .sort((left, right) => right.bottom - left.bottom)
                        .slice(0, 20);
                    const paintedBottom = visibleElements
                        .filter(item => item.semanticPaint)
                        .reduce((maximum, item) => Math.max(maximum, item.bottom), 0);
                    return {
                        title: document.title,
                        slideCount: details.length,
                        syntheticCount: details.filter(item => item.synthetic).length,
                        loadedImageCount: details.filter(item => item.loadedImage).length,
                        backgroundCount: details.filter(item => item.hasBackground).length,
                        blankIndexes: details
                            .filter(item => !item.loadedImage && !item.hasBackground)
                            .map(item => item.index),
                        slider: rectInfo(slider),
                        box: rectInfo(box),
                        wrapper: rectInfo(wrapper),
                        slides: details,
                        paintedBottom,
                        bottomElements: byBottom,
                        documentHeight: Math.ceil(Math.max(
                            document.body?.scrollHeight || 0,
                            document.documentElement?.scrollHeight || 0,
                        )),
                    };
                }"""
            )
            print(f"DOM {json.dumps(diagnostics, ensure_ascii=False)}", flush=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(image_bytes)
            with Image.open(output) as image:
                print(
                    f"SAMPLE {output} {image.width}x{image.height} "
                    f"{image.format} DPR={module.DEVICE_SCALE_FACTOR} "
                    f"{output.stat().st_size} bytes"
                )
        finally:
            await context.close()
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--chrome",
        type=Path,
        default=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    )
    args = parser.parse_args()
    asyncio.run(capture(args.url, args.output.resolve(), args.chrome))


if __name__ == "__main__":
    main()
