"""Geometry tests for segmented tall-page screenshots.

Pillow is a production dependency, but it is not installed in every lightweight
unit-test environment.  A tiny row-colour image backend is used as a fallback;
the production planning and stitching helpers are still loaded from ``main.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import struct
import sys
import types
import unittest
from pathlib import Path


_FAKE_MAGIC = b"ASTRBOT-TEST-IMAGE\0"


class _RowColourImage:
    """Minimal Pillow-compatible image storing one RGB colour per image row."""

    def __init__(self, width: int, height: int, colour=(255, 255, 255), rows=None):
        self.width = int(width)
        self.height = int(height)
        self.size = (self.width, self.height)
        self._rows = list(rows) if rows is not None else [tuple(colour)] * self.height

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def convert(self, _mode: str):
        return _RowColourImage(self.width, self.height, rows=self._rows)

    def resize(self, size, _resample=None):
        width, height = map(int, size)
        if height <= 0:
            raise ValueError("Image height must be positive")
        rows = [
            self._rows[min(self.height - 1, row * self.height // height)]
            for row in range(height)
        ]
        return _RowColourImage(width, height, rows=rows)

    def crop(self, box):
        _left, top, _right, bottom = map(int, box)
        if bottom <= top:
            raise ValueError("Crop height must be positive")
        return _RowColourImage(self.width, bottom - top, rows=self._rows[top:bottom])

    def paste(self, image, position):
        _left, top = position
        for row, colour in enumerate(image._rows):
            target = top + row
            if 0 <= target < self.height:
                self._rows[target] = colour

    def getpixel(self, position):
        _x, y = position
        return self._rows[y]

    def save(self, output, format=None, **_kwargs):
        del format
        payload = bytearray(_FAKE_MAGIC)
        payload.extend(struct.pack(">II", self.width, self.height))
        for colour in self._rows:
            payload.extend(bytes(colour))
        output.write(payload)


class _RowColourImageModule:
    Image = _RowColourImage
    Resampling = types.SimpleNamespace(LANCZOS=1)

    @staticmethod
    def new(_mode, size, colour="white"):
        if colour == "white":
            colour = (255, 255, 255)
        return _RowColourImage(*size, colour=colour)

    @staticmethod
    def open(stream):
        raw = stream.read() if hasattr(stream, "read") else Path(stream).read_bytes()
        if not raw.startswith(_FAKE_MAGIC):
            raise ValueError("Not a test image")
        offset = len(_FAKE_MAGIC)
        width, height = struct.unpack(">II", raw[offset:offset + 8])
        row_bytes = raw[offset + 8:]
        rows = [tuple(row_bytes[index:index + 3]) for index in range(0, len(row_bytes), 3)]
        if len(rows) != height:
            raise ValueError("Invalid test image row count")
        return _RowColourImage(width, height, rows=rows)


def _load_image_backend():
    """Use real Pillow when installed; otherwise install the deterministic fake."""
    existing_pil = sys.modules.pop("PIL", None)
    existing_image = sys.modules.pop("PIL.Image", None)
    try:
        return importlib.import_module("PIL.Image")
    except ModuleNotFoundError:
        # Do not restore a parser-test SimpleNamespace: stitching needs image IO.
        del existing_pil, existing_image
        pil = types.ModuleType("PIL")
        pil.Image = _RowColourImageModule
        sys.modules["PIL"] = pil
        return pil.Image


def _identity_decorator(*_args, **_kwargs):
    def decorate(function):
        return function

    return decorate


def _install_non_image_stubs() -> None:
    if "playwright.async_api" not in sys.modules:
        playwright = types.ModuleType("playwright")
        async_api = types.ModuleType("playwright.async_api")
        async_api.async_playwright = lambda: None
        async_api.Browser = type("Browser", (), {})
        async_api.BrowserContext = type("BrowserContext", (), {})
        playwright.async_api = async_api
        sys.modules["playwright"] = playwright
        sys.modules["playwright.async_api"] = async_api

    if "astrbot.api" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        event = types.ModuleType("astrbot.api.event")
        star = types.ModuleType("astrbot.api.star")
        event.filter = types.SimpleNamespace(
            command=_identity_decorator,
            event_message_type=_identity_decorator,
            EventMessageType=types.SimpleNamespace(ALL="ALL"),
        )
        event.AstrMessageEvent = type("AstrMessageEvent", (), {})
        star.Context = type("Context", (), {})
        star.Star = type("Star", (), {"__init__": lambda self, *_args, **_kwargs: None})
        api.logger = types.SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        )
        api.AstrBotConfig = dict
        astrbot.api = api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api
        sys.modules["astrbot.api.event"] = event
        sys.modules["astrbot.api.star"] = star


IMAGE_BACKEND = _load_image_backend()
_install_non_image_stubs()
_MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
_SPEC = importlib.util.spec_from_file_location("xiaoheihe_segment_helpers_for_tests", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Cannot load plugin module from {_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
XiaoheihePlugin = _MODULE.XiaoheihePlugin


def _solid_image_bytes(colour, height: int, width: int = 430) -> bytes:
    image = IMAGE_BACKEND.new("RGB", (width, height), colour)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _assert_colour_dominates(test: unittest.TestCase, actual, expected):
    """Allow normal JPEG loss while still detecting the intended colour band."""
    expected_channel = expected.index(max(expected))
    test.assertGreater(actual[expected_channel], 180)
    for channel, value in enumerate(actual):
        if channel != expected_channel:
            test.assertLess(value, 90)


class _ConstrainedSegmentPage:
    """Small Page double for root-height handling during segmented capture."""

    def __init__(self, geometry_height: int):
        self.geometry_height = int(geometry_height)
        self.viewport = {"width": 0, "height": 0}
        self.geometry_script = ""
        self.scroll_requests = []
        self.screenshot_count = 0

    async def set_viewport_size(self, viewport):
        self.viewport = dict(viewport)

    async def evaluate(self, script, argument=None):
        if "async targetHeight =>" in script:
            self.geometry_script = script
            return {
                "height": self.geometry_height,
                "before": 9589,
                "rootFloorApplied": True,
            }
        if script == "window.scrollTo(0, 0)":
            return None
        if "window.scrollTo" in script:
            self.scroll_requests.append(argument)
            return argument
        raise AssertionError(f"Unexpected page.evaluate script: {script[:80]!r}")

    async def screenshot(self, **_kwargs):
        self.screenshot_count += 1
        return _solid_image_bytes(
            (20 * self.screenshot_count, 80, 160),
            self.viewport["height"],
            self.viewport["width"],
        )


class ScreenshotSegmentPlanningTests(unittest.TestCase):
    def test_total_height_smaller_than_one_segment(self):
        self.assertEqual(
            XiaoheihePlugin._plan_screenshot_segments(1234),
            [(0, 1234)],
        )

    def test_exact_multiple_has_no_empty_trailing_segment(self):
        self.assertEqual(
            XiaoheihePlugin._plan_screenshot_segments(6000),
            [(0, 2000), (2000, 2000), (4000, 2000)],
        )

    def test_non_multiple_crops_the_last_segment_to_the_remainder(self):
        self.assertEqual(
            XiaoheihePlugin._plan_screenshot_segments(4501),
            [(0, 2000), (2000, 2000), (4000, 501)],
        )

    def test_total_height_is_not_silently_capped(self):
        plans = XiaoheihePlugin._plan_screenshot_segments(50000)

        self.assertEqual(len(plans), 25)
        self.assertEqual(plans[0], (0, 2000))
        self.assertEqual(plans[-1], (48000, 2000))
        self.assertEqual(sum(height for _top, height in plans), 50000)

    def test_explicit_safety_limit_raises_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, "exceeds safety limit"):
            XiaoheihePlugin._plan_screenshot_segments(
                100001,
                max_total_height=100000,
            )


class ScreenshotSegmentStitchingTests(unittest.TestCase):
    def test_stitches_in_y_order_and_crops_the_last_part_to_total_height(self):
        red = (255, 0, 0)
        green = (0, 255, 0)
        blue = (0, 0, 255)
        # Deliberately pass parts out of order and make the last source 2000 px
        # tall.  Only the 501 px remainder belongs in the final image.
        parts = [
            (4000, _solid_image_bytes(blue, 2000)),
            (0, _solid_image_bytes(red, 2000)),
            (2000, _solid_image_bytes(green, 2000)),
        ]

        stitched_bytes = XiaoheihePlugin._stitch_screenshot_segments(
            parts,
            total_height=4501,
            css_width=430,
        )

        with IMAGE_BACKEND.open(io.BytesIO(stitched_bytes)) as stitched:
            self.assertEqual(stitched.size, (430, 4501))
            samples = [
                (0, red),
                (1999, red),
                (2000, green),
                (3999, green),
                (4000, blue),
                (4500, blue),
            ]
            for y, expected in samples:
                with self.subTest(y=y):
                    _assert_colour_dominates(self, stitched.getpixel((215, y)), expected)


class ScreenshotSegmentCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def _capture(self, geometry_height: int):
        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
        plugin.image_quality = 92
        logs = []
        plugin._log = logs.append
        page = _ConstrainedSegmentPage(geometry_height)
        image_bytes = await plugin._capture_segmented_screenshot(
            page,
            content_height=10_911,
            css_width=430,
            segment_height=2_000,
        )
        return page, image_bytes, logs

    async def test_root_scroll_rail_preserves_requested_height(self):
        page, image_bytes, logs = await self._capture(10_913)

        self.assertIn("data-astrbot-screenshot-rail", page.geometry_script)
        self.assertIn("min-height", page.geometry_script)
        self.assertFalse(any("实际可滚动" in message for message in logs))
        self.assertEqual(page.screenshot_count, 6)
        self.assertEqual(page.scroll_requests, [0, 2000, 4000, 6000, 8000, 8911])
        with IMAGE_BACKEND.open(io.BytesIO(image_bytes)) as image:
            self.assertEqual(image.size, (430, 10_911))

    async def test_unexpandable_layout_refuses_to_crop_article(self):
        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
        plugin.image_quality = 92
        logs = []
        plugin._log = logs.append
        page = _ConstrainedSegmentPage(9_589)

        with self.assertRaisesRegex(
            RuntimeError,
            r"Page layout prevents a complete screenshot .*scrollable 9589px",
        ):
            await plugin._capture_segmented_screenshot(
                page,
                content_height=10_911,
                css_width=430,
                segment_height=2_000,
            )

        self.assertEqual(page.screenshot_count, 0)
        self.assertEqual(page.scroll_requests, [])
        self.assertTrue(any("实际可滚动 9589 px" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
