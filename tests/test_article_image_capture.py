"""Async tests for article API response capture timing."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from test_article_image_extraction import XiaoheihePlugin


_REAL_ASYNCIO_SLEEP = asyncio.sleep


class FakePage:
    def __init__(self):
        self.listeners = {}

    def on(self, event, listener):
        self.listeners[event] = listener

    def remove_listener(self, event, listener):
        if self.listeners.get(event) is listener:
            del self.listeners[event]

    def emit_response(self, response):
        listener = self.listeners.get("response")
        if listener:
            listener(response)


class FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.payload = payload

    async def json(self):
        return self.payload


class OpenLinkFakePage:
    """Page double that retains every listener so leaked retries are visible."""

    def __init__(self, goto_outcomes, snapshots):
        self.goto_outcomes = list(goto_outcomes)
        self.snapshots = list(snapshots)
        self.goto_calls = []
        self.evaluate_calls = 0
        self.listeners = {}

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        event_listeners = self.listeners.get(event, [])
        if listener in event_listeners:
            event_listeners.remove(listener)
        if not event_listeners:
            self.listeners.pop(event, None)

    def emit_response(self, response):
        for listener in list(self.listeners.get("response", [])):
            listener(response)

    async def goto(self, url, **kwargs):
        attempt = len(self.goto_calls)
        self.goto_calls.append((url, kwargs))
        outcome = self.goto_outcomes[attempt]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, FakeResponse):
            self.emit_response(outcome)

    async def evaluate(self, _script):
        snapshot = self.snapshots[self.evaluate_calls]
        self.evaluate_calls += 1
        return snapshot


class FastFinishPlugin(XiaoheihePlugin):
    """Keep production listener mechanics while removing capture quiet waits."""

    def _start_article_image_capture(self, page):
        state = super()._start_article_image_capture(page)
        self.capture_states.append(state)
        return state

    async def _finish_article_image_capture(self, state):
        if not state:
            return []
        state["closed"] = True
        state["page"].remove_listener("response", state["listener"])
        pending = list(state["tasks"])
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return list(state["urls"])


async def _yield_without_render_delay(_delay):
    """Let scheduled response inspection run without a 1.5 second page delay."""
    await _REAL_ASYNCIO_SLEEP(0)


class ArticleImageCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = XiaoheihePlugin.__new__(XiaoheihePlugin)
        self.plugin.debug = False

    async def test_finish_waits_for_response_arriving_after_finish_starts(self):
        urls = [f"https://img.example/body-{index}.jpg" for index in range(15)]
        page = FakePage()
        state = self.plugin._start_article_image_capture(page)

        async def emit_later():
            await asyncio.sleep(0.1)
            page.emit_response(FakeResponse(
                "https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=post",
                {"result": {"link": {"link_id": "post", "img_list": urls}}},
            ))

        emitter = asyncio.create_task(emit_later())
        captured = await self.plugin._finish_article_image_capture(state)
        await emitter

        self.assertEqual(captured, urls)
        self.assertNotIn("response", page.listeners)

    async def test_unrelated_responses_are_ignored(self):
        page = FakePage()
        state = self.plugin._start_article_image_capture(page)
        page.emit_response(FakeResponse(
            "https://imgheybox.max-c.com/bbs/example.jpg",
            {"result": {"link": {"link_id": "post", "img_list": ["https://bad"]}}},
        ))

        captured = await self.plugin._finish_article_image_capture(state)

        self.assertEqual(captured, [])
        self.assertEqual(state["responses"], 0)


class OpenLinkPageWithCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = FastFinishPlugin.__new__(FastFinishPlugin)
        self.plugin.debug = False
        self.plugin.wait_timeout = 1000
        self.plugin.render_delay = 0
        self.plugin.capture_states = []
        self.target_url = "https://www.xiaoheihe.cn/app/bbs/link/test-post"
        self.empty_snapshot = {
            "sliderCount": 0,
            "imageCount": 0,
            "textLength": 0,
            "documentHeight": 932,
            "viewportHeight": 932,
        }
        self.open_snapshot = {
            "sliderCount": 1,
            "imageCount": 12,
            "textLength": 80,
            "documentHeight": 6000,
            "viewportHeight": 932,
        }

    async def _open(self, page):
        with patch.object(asyncio, "sleep", new=_yield_without_render_delay):
            return await self.plugin._open_link_page_with_capture(page, self.target_url)

    async def test_retries_empty_dom_even_when_first_attempt_captures_api_urls(self):
        urls = [f"https://img.example/body-{index}.jpg" for index in range(15)]
        response = FakeResponse(
            "https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=test-post",
            {"result": {"link": {"link_id": "test-post", "img_list": urls}}},
        )
        page = OpenLinkFakePage(
            goto_outcomes=[response, None],
            snapshots=[self.empty_snapshot, self.open_snapshot],
        )

        open_state = await self._open(page)

        self.assertEqual(len(page.goto_calls), 2)
        self.assertEqual(self.plugin.capture_states[0]["urls"], urls)
        self.assertTrue(self.plugin.capture_states[0]["closed"])
        self.assertIs(open_state, self.plugin.capture_states[1])
        self.assertFalse(open_state["closed"])
        self.assertEqual(page.listeners.get("response"), [open_state["listener"]])

        await self.plugin._finish_article_image_capture(open_state)
        self.assertNotIn("response", page.listeners)

    async def test_two_empty_attempts_raise_and_remove_every_response_listener(self):
        page = OpenLinkFakePage(
            goto_outcomes=[None, None],
            snapshots=[self.empty_snapshot, self.empty_snapshot],
        )

        with self.assertRaises(RuntimeError):
            await self._open(page)

        self.assertEqual(len(page.goto_calls), 2)
        self.assertEqual(len(self.plugin.capture_states), 2)
        self.assertTrue(all(state["closed"] for state in self.plugin.capture_states))
        self.assertNotIn("response", page.listeners)

    async def test_first_goto_exception_cleans_listener_before_retrying(self):
        page = OpenLinkFakePage(
            goto_outcomes=[RuntimeError("navigation failed"), None],
            snapshots=[self.open_snapshot],
        )

        open_state = await self._open(page)

        self.assertEqual(len(page.goto_calls), 2)
        first_state, second_state = self.plugin.capture_states
        self.assertTrue(first_state["closed"])
        self.assertNotIn(first_state["listener"], page.listeners.get("response", []))
        self.assertIs(open_state, second_state)
        self.assertEqual(page.listeners.get("response"), [second_state["listener"]])

        await self.plugin._finish_article_image_capture(open_state)
        self.assertNotIn("response", page.listeners)


if __name__ == "__main__":
    unittest.main()
