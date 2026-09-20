"""Tests for the QQ/NapCat image safety locks and final encoding path.

These tests reuse the import stubs and optional Pillow backend from
``test_screenshot_segments``.  They never contact Xiaoheihe or launch a browser.
"""

from __future__ import annotations

import base64
import io
import random
import struct
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from test_screenshot_segments import IMAGE_BACKEND, _MODULE, XiaoheihePlugin
except ModuleNotFoundError:
    from tests.test_screenshot_segments import IMAGE_BACKEND, _MODULE, XiaoheihePlugin


MAX_IMAGE_DIMENSION = _MODULE.MAX_IMAGE_DIMENSION
MAX_IMAGE_PIXELS = _MODULE.MAX_IMAGE_PIXELS
MAX_IMAGE_BYTES = _MODULE.MAX_IMAGE_BYTES
DEVICE_SCALE_FACTOR = _MODULE.DEVICE_SCALE_FACTOR
MIN_JPEG_QUALITY = _MODULE.MIN_JPEG_QUALITY
PILLOW_AVAILABLE = (
    isinstance(IMAGE_BACKEND, types.ModuleType)
    and getattr(IMAGE_BACKEND, "__name__", "").startswith("PIL.Image")
)


class _SizedPayload(bytes):
    """A tiny bytes value reporting a simulated encoded size via ``len``."""

    def __new__(cls, payload: bytes, reported_length: int):
        value = super().__new__(cls, payload)
        value.reported_length = int(reported_length)
        return value

    def __len__(self):
        return self.reported_length


def _png_image_bytes(width: int = 32, height: int = 24) -> bytes:
    image = IMAGE_BACKEND.new("RGB", (width, height), (80, 130, 190))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _plugin() -> XiaoheihePlugin:
    plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
    plugin.debug = False
    return plugin


class ImageSizeSafetyTests(unittest.TestCase):
    def test_internal_locks_and_device_scale_are_fixed(self):
        self.assertEqual(DEVICE_SCALE_FACTOR, 2)
        self.assertEqual(MAX_IMAGE_DIMENSION, 16384)
        self.assertEqual(MAX_IMAGE_PIXELS, 20_000_000)
        self.assertEqual(MAX_IMAGE_BYTES, 10 * 1024 * 1024)
        self.assertEqual(MIN_JPEG_QUALITY, 50)

    def test_dpr2_physical_dimensions_are_not_reduced_when_under_both_locks(self):
        # 430 CSS px at DPR 2 is 860 physical px; the page stays in CSS space.
        physical_size = (430 * DEVICE_SCALE_FACTOR, 5000 * DEVICE_SCALE_FACTOR)
        self.assertEqual(XiaoheihePlugin._safe_image_size(*physical_size), physical_size)

    def test_longest_edge_lock_scales_only_as_far_as_needed(self):
        actual = XiaoheihePlugin._safe_image_size(20_000, 500)

        self.assertEqual(actual, (16_384, 409))
        self.assertLessEqual(max(actual), MAX_IMAGE_DIMENSION)
        self.assertLessEqual(actual[0] * actual[1], MAX_IMAGE_PIXELS)

    def test_pixel_lock_scales_only_as_far_as_needed(self):
        actual = XiaoheihePlugin._safe_image_size(5000, 5000)

        self.assertEqual(actual, (4472, 4472))
        self.assertLessEqual(max(actual), MAX_IMAGE_DIMENSION)
        self.assertLessEqual(actual[0] * actual[1], MAX_IMAGE_PIXELS)

    def test_stricter_limit_wins_when_both_dimensions_and_pixels_exceed(self):
        # For 20000x1200 the longest-edge limit is tighter; for 20000x2000
        # the 20 MP limit is tighter.  Each result satisfies both locks.
        edge_limited = XiaoheihePlugin._safe_image_size(20_000, 1200)
        pixel_limited = XiaoheihePlugin._safe_image_size(20_000, 2000)

        self.assertEqual(edge_limited, (16_384, 983))
        self.assertEqual(pixel_limited, (14_142, 1414))
        for size in (edge_limited, pixel_limited):
            self.assertLessEqual(max(size), MAX_IMAGE_DIMENSION)
            self.assertLessEqual(size[0] * size[1], MAX_IMAGE_PIXELS)

    def test_boundary_and_deterministic_random_sizes_always_fit(self):
        rng = random.Random(20260920)
        sizes = [
            (1, 1),
            (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
            (MAX_IMAGE_DIMENSION + 1, 1),
            (1, MAX_IMAGE_DIMENSION + 1),
            (5000, 5000),
            (20_000, 2000),
        ]
        sizes.extend(
            (rng.randint(1, 50_000), rng.randint(1, 50_000))
            for _ in range(200)
        )

        for width, height in sizes:
            with self.subTest(width=width, height=height):
                actual = XiaoheihePlugin._safe_image_size(width, height)
                self.assertGreaterEqual(actual[0], 1)
                self.assertGreaterEqual(actual[1], 1)
                self.assertLessEqual(actual[0], width)
                self.assertLessEqual(actual[1], height)
                self.assertLessEqual(max(actual), MAX_IMAGE_DIMENSION)
                self.assertLessEqual(actual[0] * actual[1], MAX_IMAGE_PIXELS)

    def test_non_positive_image_dimensions_are_rejected(self):
        for size in ((0, 1), (1, 0), (-1, 3)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                XiaoheihePlugin._safe_image_size(*size)


class ImageEncodingTests(unittest.TestCase):
    def test_quality_100_is_kept_below_and_at_the_byte_lock(self):
        source = _png_image_bytes()
        for payload_size in (MAX_IMAGE_BYTES - 1, MAX_IMAGE_BYTES):
            with self.subTest(payload_size=payload_size):
                calls = []

                def encode(_image, quality):
                    calls.append(quality)
                    return _SizedPayload(bytes([quality]), payload_size)

                with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
                    result = _plugin()._normalize_for_qq(source)

                self.assertEqual(bytes(result), bytes([100]))
                self.assertEqual(len(result), payload_size)
                self.assertEqual(calls, [100])

    def test_over_limit_jpeg_search_returns_highest_acceptable_quality(self):
        source = _png_image_bytes()
        # Quality 84 is the highest integer whose simulated file fits under
        # 10 MiB; all higher qualities are larger.
        def encoded_size(quality):
            return 7_000_000 + (quality - 50) * 100_000

        calls = []

        def encode(_image, quality):
            calls.append(quality)
            return _SizedPayload(bytes([quality]), encoded_size(quality))

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            result = _plugin()._normalize_for_qq(source)

        self.assertEqual(bytes(result), bytes([84]))
        self.assertEqual(len(result), encoded_size(84))
        self.assertLessEqual(len(result), MAX_IMAGE_BYTES)
        self.assertIn(100, calls)
        self.assertIn(MIN_JPEG_QUALITY, calls)

    def test_minimum_quality_over_limit_triggers_size_reduction(self):
        source = _png_image_bytes(400, 300)
        calls = []

        def encode(image, quality):
            width, height = image.size
            reported_size = width * height * quality * 10
            calls.append((width, height, quality, reported_size))
            marker = struct.pack(">HHB", width, height, quality)
            return _SizedPayload(marker, reported_size)

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            result = _plugin()._normalize_for_qq(source)

        final_width, final_height, final_quality = struct.unpack(">HHB", bytes(result))
        final_size = final_width * final_height * final_quality * 10
        self.assertEqual((final_width, final_height), (167, 125))
        self.assertGreater(400 * 300 * 100 * 10, MAX_IMAGE_BYTES)
        self.assertGreater(400 * 300 * MIN_JPEG_QUALITY * 10, MAX_IMAGE_BYTES)
        self.assertGreater(168 * 126 * MIN_JPEG_QUALITY * 10, MAX_IMAGE_BYTES)
        # A smaller image could fit at quality 100, but the policy first keeps
        # the largest dimensions that pass the minimum quality byte lock.
        self.assertLessEqual(118 * 88 * 100 * 10, MAX_IMAGE_BYTES)
        self.assertGreaterEqual(final_quality, MIN_JPEG_QUALITY)
        self.assertEqual(final_quality, MIN_JPEG_QUALITY)
        self.assertLessEqual(final_size, MAX_IMAGE_BYTES)
        self.assertEqual(len(result), final_size)
        self.assertTrue(any((width, height) != (400, 300) for width, height, *_ in calls))

    def test_local_size_reversal_does_not_hide_a_higher_quality(self):
        calls = []

        def encode(_image, quality):
            calls.append(quality)
            fits = quality <= 84 or quality == 97
            return _SizedPayload(bytes([quality]), MAX_IMAGE_BYTES if fits else MAX_IMAGE_BYTES + 1)

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            result = _plugin()._normalize_for_qq(_png_image_bytes())
        self.assertEqual(bytes(result), bytes([97]))
        self.assertEqual(len(calls), len(set(calls)))
        self.assertLessEqual(len(calls), 51)

    def test_minimum_quality_failure_checks_higher_qualities_before_resizing(self):
        sizes = []

        def encode(image, quality):
            sizes.append(image.size)
            return _SizedPayload(bytes([quality]), MAX_IMAGE_BYTES if quality == 70 else MAX_IMAGE_BYTES + 1)

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            result = _plugin()._normalize_for_qq(_png_image_bytes(32, 24))
        self.assertEqual(bytes(result), bytes([70]))
        self.assertEqual(set(sizes), {(32, 24)})

    def test_unattainable_byte_lock_raises_instead_of_returning_an_oversized_image(self):
        source = _png_image_bytes(16, 12)
        attempted_sizes = []

        def encode(image, quality):
            attempted_sizes.append((image.size, quality))
            return _SizedPayload(bytes([quality]), MAX_IMAGE_BYTES + 1)

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            with self.assertRaisesRegex(RuntimeError, "within the QQ byte safety lock"):
                _plugin()._normalize_for_qq(source)

        self.assertTrue(attempted_sizes)
        self.assertTrue(any(size != (16, 12) for size, _quality in attempted_sizes))
        self.assertLessEqual(
            sum(1 for size, quality in attempted_sizes if quality == MIN_JPEG_QUALITY),
            16,
        )

    def test_normalization_applies_dimension_pixel_and_byte_locks_together(self):
        encoded_sizes = []

        class SyntheticImage:
            def __init__(self, width, height):
                self.width = width
                self.height = height
                self.size = (width, height)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def convert(self, _mode):
                return self

            def resize(self, size, _resample=None):
                return SyntheticImage(*size)

        def encode(image, quality):
            encoded_sizes.append(image.size)
            return _SizedPayload(bytes([quality]), 100)

        with (
            patch.object(
                _MODULE.Image,
                "open",
                side_effect=lambda *_args, **_kwargs: SyntheticImage(20_000, 2000),
            ),
            patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)),
        ):
            result = _plugin()._normalize_for_qq(b"synthetic input")

        self.assertEqual(encoded_sizes, [(14_142, 1414)])
        self.assertLessEqual(max(encoded_sizes[-1]), MAX_IMAGE_DIMENSION)
        self.assertLessEqual(encoded_sizes[-1][0] * encoded_sizes[-1][1], MAX_IMAGE_PIXELS)
        self.assertLessEqual(len(result), MAX_IMAGE_BYTES)

    @unittest.skipUnless(PILLOW_AVAILABLE, "Pillow is optional in the lightweight test runtime")
    def test_real_pillow_normalization_outputs_one_high_quality_jpeg(self):
        source = _png_image_bytes(860, 240)
        original_encoder = XiaoheihePlugin._encode_jpeg
        calls = []

        def encode(image, quality):
            calls.append(quality)
            return original_encoder(image, quality)

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            result = _plugin()._normalize_for_qq(source)

        with IMAGE_BACKEND.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (860, 240))
        self.assertEqual(calls, [100])
        self.assertLessEqual(len(result), MAX_IMAGE_BYTES)

    @unittest.skipUnless(PILLOW_AVAILABLE, "Pillow is optional in the lightweight test runtime")
    def test_jpeg_encoder_uses_real_pillow_jpeg_bytes(self):
        image = IMAGE_BACKEND.new("RGB", (64, 48), (20, 90, 180))

        result = XiaoheihePlugin._encode_jpeg(image, 100)

        with IMAGE_BACKEND.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (64, 48))

    @unittest.skipUnless(PILLOW_AVAILABLE, "Pillow is optional in the lightweight test runtime")
    def test_real_512_pixel_noise_keeps_quality_100_and_dimensions(self):
        raw_pixels = random.Random(712).randbytes(512 * 512 * 3)
        source_image = IMAGE_BACKEND.frombytes("RGB", (512, 512), raw_pixels)
        source_output = io.BytesIO()
        source_image.save(source_output, format="PNG")
        source = source_output.getvalue()

        original_encoder = XiaoheihePlugin._encode_jpeg
        calls = []

        def encode(image, quality):
            calls.append((image.size, quality))
            return original_encoder(image, quality)

        with patch.object(XiaoheihePlugin, "_encode_jpeg", staticmethod(encode)):
            result = _plugin()._normalize_for_qq(source)

        with IMAGE_BACKEND.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (512, 512))
            self.assertTrue(decoded.quantization)
            self.assertTrue(
                all(value == 1 for table in decoded.quantization.values() for value in table)
            )
        self.assertEqual(calls, [((512, 512), 100)])
        self.assertLessEqual(len(result), MAX_IMAGE_BYTES)


class JpegEncoderFallbackTests(unittest.TestCase):
    def test_optimize_buffer_error_retries_same_quality_without_optimization(self):
        class SaveProbe:
            size = (32, 24)

            def __init__(self):
                self.calls = []

            def save(self, output, **kwargs):
                self.calls.append(kwargs)
                if kwargs["optimize"]:
                    raise OSError("optimized buffer too small")
                output.write(b"same-quality jpeg")

        image = SaveProbe()

        result = XiaoheihePlugin._encode_jpeg(image, 100)

        self.assertEqual(result, b"same-quality jpeg")
        self.assertEqual(len(image.calls), 2)
        self.assertEqual([call["quality"] for call in image.calls], [100, 100])
        self.assertEqual([call["subsampling"] for call in image.calls], [0, 0])
        self.assertEqual([call["optimize"] for call in image.calls], [True, False])

    def test_both_encoder_modes_failing_propagates_oserror(self):
        class SaveProbe:
            size = (32, 24)

            def __init__(self):
                self.calls = []

            def save(self, _output, **kwargs):
                self.calls.append(kwargs)
                raise OSError("both encoder passes failed")

        image = SaveProbe()

        with self.assertRaisesRegex(OSError, "both encoder passes failed"):
            XiaoheihePlugin._encode_jpeg(image, 100)

        self.assertEqual(len(image.calls), 2)
        self.assertEqual([call["quality"] for call in image.calls], [100, 100])
        self.assertEqual([call["optimize"] for call in image.calls], [True, False])

    def test_normalization_keeps_quality_100_after_optimize_buffer_error(self):
        class SaveProbe:
            width = 32
            height = 24
            size = (32, 24)

            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def convert(self, _mode):
                return self

            def save(self, output, **kwargs):
                self.calls.append(kwargs)
                if kwargs["optimize"]:
                    raise OSError("optimized buffer too small")
                output.write(b"quality 100 jpeg")

        image = SaveProbe()
        with patch.object(_MODULE.Image, "open", return_value=image):
            result = _plugin()._normalize_for_qq(b"synthetic png")

        self.assertEqual(result, b"quality 100 jpeg")
        self.assertEqual([call["quality"] for call in image.calls], [100, 100])
        self.assertEqual([call["optimize"] for call in image.calls], [True, False])

    def test_normalization_propagates_when_both_encoder_modes_fail(self):
        class SaveProbe:
            size = (32, 24)

            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def convert(self, _mode):
                return self

            def save(self, _output, **kwargs):
                self.calls.append(kwargs)
                raise OSError("both encoder passes failed")

        image = SaveProbe()
        with patch.object(_MODULE.Image, "open", return_value=image):
            with self.assertRaisesRegex(OSError, "both encoder passes failed"):
                _plugin()._normalize_for_qq(b"synthetic png")

        self.assertEqual([call["quality"] for call in image.calls], [100, 100])
        self.assertEqual([call["optimize"] for call in image.calls], [True, False])


class _ResultDouble:
    def __init__(self):
        self.base64_payloads = []

    def base64_image(self, payload):
        self.base64_payloads.append(payload)
        return ("image", payload)


class _EventDouble:
    def __init__(self):
        self.result = _ResultDouble()
        self.make_result_calls = 0
        self.plain_results = []

    def make_result(self):
        self.make_result_calls += 1
        return self.result

    def plain_result(self, message):
        self.plain_results.append(message)
        return ("plain", message)


class _SearchFailurePage:
    def __init__(self, image_bytes):
        self.image_bytes = image_bytes
        self.screenshot_options = None

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_selector(self, *_args, **_kwargs):
        raise TimeoutError("search result not found")

    async def screenshot(self, **kwargs):
        self.screenshot_options = kwargs
        return self.image_bytes


class _ContextDouble:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class ImageSendPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_failure_full_page_png_is_normalized_before_base64_send(self):
        source_png = _png_image_bytes(80, 40)
        page = _SearchFailurePage(source_png)
        context = _ContextDouble(page)
        event = _EventDouble()
        plugin = _plugin()
        plugin._create_context = AsyncMock(return_value=context)
        plugin._normalize_for_qq = Mock(return_value=b"safe-jpeg")

        results = [result async for result in plugin._process_screenshot(event, "missing game")]

        self.assertEqual(
            page.screenshot_options,
            {"type": "png", "full_page": True, "scale": "device"},
        )
        plugin._normalize_for_qq.assert_called_once_with(source_png)
        self.assertEqual(results, [("image", base64.b64encode(b"safe-jpeg").decode("ascii"))])
        self.assertEqual(event.result.base64_payloads, [base64.b64encode(b"safe-jpeg").decode("ascii")])
        self.assertTrue(context.closed)

    async def test_search_failure_normalization_error_sends_no_original_image(self):
        source_png = _png_image_bytes(80, 40)
        page = _SearchFailurePage(source_png)
        context = _ContextDouble(page)
        event = _EventDouble()
        plugin = _plugin()
        plugin._create_context = AsyncMock(return_value=context)
        plugin._normalize_for_qq = Mock(side_effect=RuntimeError("encoder failed"))

        results = [result async for result in plugin._process_screenshot(event, "missing game")]

        plugin._normalize_for_qq.assert_called_once_with(source_png)
        self.assertEqual(event.result.base64_payloads, [])
        self.assertEqual(event.make_result_calls, 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "plain")
        self.assertTrue(context.closed)

    def test_base64_result_propagates_normalization_failure_without_sending_input(self):
        event = _EventDouble()
        plugin = _plugin()
        plugin._normalize_for_qq = Mock(side_effect=ValueError("invalid image"))

        with self.assertRaisesRegex(ValueError, "invalid image"):
            plugin._base64_image_result(event, b"raw image")

        self.assertEqual(event.result.base64_payloads, [])
        self.assertEqual(event.make_result_calls, 0)

    def test_legacy_screenshot_config_fields_are_ignored_during_initialization(self):
        legacy_config = {
            "wait_timeout": "invalid legacy value",
            "render_delay": -1,
            "device_scale_factor": 99,
            "image_quality": 1,
        }

        plugin = XiaoheihePlugin(object(), legacy_config)

        for attribute in ("wait_timeout", "render_delay", "device_scale_factor", "image_quality"):
            self.assertFalse(hasattr(plugin, attribute), attribute)

    async def test_browser_context_keeps_css_viewport_and_fixed_dpr2(self):
        browser = types.SimpleNamespace(new_context=AsyncMock(return_value=object()))
        plugin = _plugin()
        plugin.cookies = ""
        plugin._get_browser = AsyncMock(return_value=browser)

        await plugin._create_context()

        kwargs = browser.new_context.await_args.kwargs
        self.assertEqual(kwargs["viewport"], {"width": 430, "height": 932})
        self.assertEqual(kwargs["device_scale_factor"], 2)


if __name__ == "__main__":
    unittest.main()
