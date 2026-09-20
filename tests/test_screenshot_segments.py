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

    def resize(self, size, _resample=None, box=None):
        width, height = map(int, size)
        if height <= 0:
            raise ValueError("Image height must be positive")
        source_top, source_bottom = (box[1], box[3]) if box else (0, self.height)
        rows = [
            self._rows[min(self.height - 1, int(source_top + row * (source_bottom - source_top) / height))]
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


def _row_colour(y):
    return (y // 256 % 256, y % 256, 73)


def _row_pattern_bytes(width, height, start=0):
    image = IMAGE_BACKEND.new("RGB", (width, height))
    for y in range(height):
        image.paste(IMAGE_BACKEND.new("RGB", (width, 1), _row_colour(start + y)), (0, y))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _assert_colour_dominates(test: unittest.TestCase, actual, expected):
    """Allow resampling at a seam while detecting the intended colour band."""
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
        if _kwargs.get("scale") != "device" or _kwargs.get("type") != "png":
            raise AssertionError("Capture must retain device pixels in lossless PNG")
        self.screenshot_count += 1
        return _solid_image_bytes(
            (20 * self.screenshot_count, 80, 160),
            self.viewport["height"] * 2,
            self.viewport["width"] * 2,
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
    def test_crop_uses_css_coordinates_and_keeps_every_device_row(self):
        cropped = XiaoheihePlugin._crop_screenshot_segment(
            _row_pattern_bytes(8, 40), viewport_height=20, crop_top=7.5, crop_height=9,
        )
        with IMAGE_BACKEND.open(io.BytesIO(cropped)) as image:
            self.assertEqual(image.size, (8, 18))
            self.assertEqual([image.getpixel((0, y)) for y in range(18)],
                             [_row_colour(y) for y in range(15, 33)])

    def test_crop_refuses_missing_rows(self):
        with self.assertRaisesRegex(ValueError, "outside the viewport"):
            XiaoheihePlugin._crop_screenshot_segment(
                _solid_image_bytes((255, 0, 0), 40, 8), 20, 15, 6,
            )

    def test_final_excess_rows_are_cropped_instead_of_squeezed(self):
        parts = [(0, _row_pattern_bytes(8, 40))]
        stitched = XiaoheihePlugin._stitch_screenshot_segments(parts, 13, css_width=4)
        with IMAGE_BACKEND.open(io.BytesIO(stitched)) as image:
            self.assertEqual(image.size, (8, 26))
            self.assertEqual([image.getpixel((0, y)) for y in range(26)],
                             [_row_colour(y) for y in range(26)])

    @unittest.skipUnless(isinstance(IMAGE_BACKEND, types.ModuleType), "Requires Pillow resampling")
    def test_scaled_absolute_boundaries_match_whole_image_without_seams(self):
        total = 10003
        plans = XiaoheihePlugin._plan_screenshot_segments(total, 997)
        colours = [(10 + index * 10, 80, 160) for index in range(len(plans))]
        parts = [(top, _solid_image_bytes(colour, height * 2, 20))
                 for (top, height), colour in zip(plans, colours)]
        # Only the test builds an unbounded source canvas as a reference.
        reference = IMAGE_BACKEND.new("RGB", (20, total * 2))
        for (top, height), colour in zip(plans, colours):
            reference.paste(IMAGE_BACKEND.new("RGB", (20, height * 2), colour), (0, top * 2))
        reference = reference.resize((16, 16384), IMAGE_BACKEND.Resampling.LANCZOS)
        stitched = XiaoheihePlugin._stitch_screenshot_segments(parts, total, css_width=10)
        with IMAGE_BACKEND.open(io.BytesIO(stitched)) as image:
            self.assertEqual(image.size, (16, 16384))
            for y in range(image.height):
                self.assertLessEqual(
                    max(abs(a - b) for a, b in zip(image.getpixel((0, y)), reference.getpixel((0, y)))),
                    1, f"row {y} differs from whole-image resampling",
                )

    @unittest.skipUnless(isinstance(IMAGE_BACKEND, types.ModuleType), "Requires Pillow resampling")
    def test_scaled_fine_detail_across_segment_boundaries_matches_reference(self):
        # A continuous row pattern exposes filtering discontinuities that solid
        # colour bands alone would miss. Include an odd, very short last slice.
        total = 8201
        plans = XiaoheihePlugin._plan_screenshot_segments(total, 2000)
        original_bytes = _row_pattern_bytes(8, total * 2)
        parts = []
        with IMAGE_BACKEND.open(io.BytesIO(original_bytes)) as original:
            for top, height in plans:
                output = io.BytesIO()
                original.crop((0, top * 2, 8, (top + height) * 2)).save(output, format="PNG")
                parts.append((top, output.getvalue()))
            reference = original.resize((7, 16384), IMAGE_BACKEND.Resampling.LANCZOS)
        result = XiaoheihePlugin._stitch_screenshot_segments(parts, total, css_width=4)
        with IMAGE_BACKEND.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, reference.size)
            for y in range(image.height):
                self.assertLessEqual(
                    max(abs(a - b) for a, b in zip(image.getpixel((0, y)), reference.getpixel((0, y)))),
                    1, f"row {y} differs from whole-image resampling",
                )

    def test_missing_duplicate_or_low_resolution_segments_are_rejected(self):
        valid = _solid_image_bytes((255, 0, 0), 20, 8)
        invalid_parts = [
            [(1, valid)],
            [(0, valid), (0, valid)],
            [(0, _solid_image_bytes((255, 0, 0), 18, 8))],
            [(0, _solid_image_bytes((255, 0, 0), 20, 4))],
        ]
        for parts in invalid_parts:
            with self.subTest(parts=[top for top, _ in parts]), self.assertRaises(ValueError):
                XiaoheihePlugin._stitch_screenshot_segments(parts, 10, css_width=4)

    def test_stitches_in_y_order_and_crops_the_last_part_to_total_height(self):
        red = (255, 0, 0)
        green = (0, 255, 0)
        blue = (0, 0, 255)
        # Deliberately pass DPR2 parts out of order with an overlong last part.
        # Only its first 1002 physical rows belong in the final image.
        parts = [
            (4000, _solid_image_bytes(blue, 4000, 860)),
            (0, _solid_image_bytes(red, 4000, 860)),
            (2000, _solid_image_bytes(green, 4000, 860)),
        ]

        stitched_bytes = XiaoheihePlugin._stitch_screenshot_segments(
            parts,
            total_height=4501,
            css_width=430,
        )

        with IMAGE_BACKEND.open(io.BytesIO(stitched_bytes)) as stitched:
            self.assertEqual(stitched.size, (860, 9002))
            samples = [
                (0, red),
                (3999, red),
                (4000, green),
                (7999, green),
                (8000, blue),
                (9001, blue),
            ]
            for y, expected in samples:
                with self.subTest(y=y):
                    _assert_colour_dominates(self, stitched.getpixel((215, y)), expected)


class ScreenshotSegmentCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_keeps_dpr2_rows_without_repeating_the_last_viewport(self):
        class RowPage(_ConstrainedSegmentPage):
            async def screenshot(self, **kwargs):
                if kwargs.get("scale") != "device":
                    raise AssertionError("DPR must be preserved")
                return _row_pattern_bytes(
                    self.viewport["width"] * 2, self.viewport["height"] * 2,
                    self.scroll_requests[-1] * 2,
                )

        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
        plugin._log = lambda _message: None
        page = RowPage(2005)
        result = await plugin._capture_segmented_screenshot(
            page, content_height=2005, css_width=4, segment_height=777,
        )
        self.assertEqual(page.scroll_requests, [0, 777, 1228])
        with IMAGE_BACKEND.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (8, 4010))
            self.assertEqual([image.getpixel((0, y)) for y in range(image.height)],
                             [_row_colour(y) for y in range(4010)])

    async def _capture(self, geometry_height: int):
        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
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
            self.assertEqual(image.size, (645, 16_384))

    async def test_unexpandable_layout_refuses_to_crop_article(self):
        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
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
