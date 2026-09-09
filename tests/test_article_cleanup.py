"""Regression tests for shared-post text expansion and chrome cleanup.

These tests intentionally do not launch Chromium.  The page doubles record the
embedded JavaScript sent to Playwright, while the orchestration test replaces
the expensive page operations with traceable coroutines.  This keeps the
contract covered in the ordinary lightweight unit-test environment.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    # ``unittest discover -s tests`` puts the test directory on sys.path.
    from test_screenshot_segments import XiaoheihePlugin
except ModuleNotFoundError:
    # Also support invoking this file as ``python -m unittest tests/...``.
    from tests.test_screenshot_segments import XiaoheihePlugin


class _EvaluateProbePage:
    def __init__(self):
        self.evaluate_calls = []

    async def evaluate(self, script, *args):
        self.evaluate_calls.append((script, args))
        if "expandLabels" in script:
            return {
                "matched": 1,
                "clicked": 1,
                "remaining": 0,
                "skippedNavigation": 0,
                "expanded": True,
            }
        return 120


class ArticleTextScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_expand_script_supports_both_full_text_labels_and_clicks_safe_control(self):
        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
        page = _EvaluateProbePage()

        result = await plugin._expand_article_text(page)

        self.assertEqual(result["clicked"], 1)
        script = page.evaluate_calls[0][0]
        self.assertIn("展开全文", script)
        self.assertIn("展开全部", script)
        self.assertIn("显示全文", script)
        self.assertIn("target.click()", script)
        self.assertIn("closest('button, a, [role=\"button\"]')", script)
        self.assertIn("!element.closest(excludedSelector)", script)
        self.assertIn("skippedNavigation", script)
        self.assertIn("setTimeout(resolve, 350)", script)

    async def test_cleanup_script_hides_tags_time_comments_and_cta(self):
        plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
        page = _EvaluateProbePage()

        measured_height = await plugin._clean_and_measure_article_height(page)

        self.assertEqual(measured_height, 120)
        script = page.evaluate_calls[0][0]
        # These are the stable page sections seen in the shared-post layout;
        # the exact generated class names are otherwise free to change.
        self.assertIn(".link-section-tags", script)
        self.assertIn(".link-section-link-data", script)
        self.assertIn(".bbs-link-section-tags", script)
        self.assertIn(".bbs-link-section-bottom-info", script)
        self.assertIn(".article-expand-button", script)
        self.assertIn(".has-fixed-share-header", script)
        self.assertIn("'padding-top', '0'", script)
        self.assertIn('[class*="comment" i]', script)
        self.assertIn('[class*="reply" i]', script)
        self.assertIn("打开小黑盒查看全部精彩评论", script)
        self.assertIn("查看全部精彩评论", script)
        self.assertIn("打开小黑盒查看评论", script)
        self.assertIn("style.setProperty('display', 'none', 'important')", script)
        self.assertNotIn("'展开全文'", script)
        self.assertNotIn("'展开全部'", script)

        # Expansion is performed before measurement; a collapsed-control
        # branch must never truncate the article at the control's top edge.
        self.assertNotIn("element.getBoundingClientRect().top + window.scrollY", script)


class _PreparePage:
    async def set_viewport_size(self, viewport):
        self.trace.append(("viewport", viewport))

    def __init__(self, trace):
        self.trace = trace


class _PrepareProbePlugin(XiaoheihePlugin):
    def __init__(self, expand_results, trace):
        # Avoid Star/AstrBot construction; only the fields used by
        # _prepare_and_screenshot are needed for this orchestration test.
        self.render_delay = 0
        self.image_quality = 92
        self.debug = False
        self.expand_results = list(expand_results)
        self.trace = trace

    async def _expand_article_text(self, _page):
        self.trace.append("expand")
        return self.expand_results.pop(0)

    async def _scroll_for_lazy_loading(self, _page):
        self.trace.append("scroll")

    async def _finish_article_image_capture(self, _state):
        self.trace.append("finish-images")
        return []

    async def _extract_article_img_urls_from_page(self, _page):
        self.trace.append("extract-page-images")
        return []

    async def _expand_article_image_sliders(self, _page, _urls):
        self.trace.append("expand-image-sliders")
        return {}

    async def _clean_and_measure_article_height(self, _page):
        self.trace.append("clean-and-measure")
        return 123

    async def _capture_segmented_screenshot(self, _page, _content_height, **_kwargs):
        self.trace.append("capture")
        return b"test-image"

    def _log(self, _message):
        pass


class PrepareOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, expand_results):
        trace = []
        plugin = _PrepareProbePlugin(expand_results, trace)
        page = _PreparePage(trace)
        with patch("asyncio.sleep", autospec=True) as sleep:
            sleep.return_value = None
            image = await plugin._prepare_and_screenshot(page)
        return image, trace

    async def test_expands_before_scroll_and_cleans_before_capture(self):
        image, trace = await self._run([
            {"matched": 1, "clicked": 1, "remaining": 0}
        ])

        self.assertEqual(image, b"test-image")
        self.assertEqual(
            [name for name in trace if isinstance(name, str)],
            [
                "expand",
                "scroll",
                "finish-images",
                "extract-page-images",
                "expand-image-sliders",
                "clean-and-measure",
                "capture",
            ],
        )
        self.assertEqual(trace[0][0], "viewport")

    async def test_late_expand_triggers_a_second_lazy_loading_pass(self):
        _image, trace = await self._run([
            {"matched": 0, "clicked": 0, "remaining": 0},
            {"matched": 1, "clicked": 1, "remaining": 0},
        ])

        self.assertEqual(
            [name for name in trace if isinstance(name, str)],
            [
                "expand",
                "scroll",
                "expand",
                "scroll",
                "finish-images",
                "extract-page-images",
                "expand-image-sliders",
                "clean-and-measure",
                "capture",
            ],
        )

    async def test_unresolved_expander_refuses_to_capture_collapsed_article(self):
        with self.assertRaisesRegex(RuntimeError, "Article expansion did not complete"):
            await self._run([
                {"matched": 1, "clicked": 1, "remaining": 1},
                {"matched": 1, "clicked": 1, "remaining": 1},
            ])


if __name__ == "__main__":
    unittest.main()
