"""Unit tests for Xiaoheihe article-body image URL extraction.

The production module normally runs inside AstrBot with Playwright and Pillow
installed.  These tests only need the pure static parser, so minimal dependency
stubs are installed before importing ``main.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


def _identity_decorator(*_args, **_kwargs):
    def decorate(function):
        return function

    return decorate


def _install_dependency_stubs() -> None:
    """Provide just enough external API surface to import the plugin module."""
    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        pil.Image = types.SimpleNamespace()
        sys.modules["PIL"] = pil

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


def _load_plugin_class():
    _install_dependency_stubs()
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("xiaoheihe_plugin_for_tests", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load plugin module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.XiaoheihePlugin


XiaoheihePlugin = _load_plugin_class()


class ExtractArticleImageUrlsTests(unittest.TestCase):
    def test_does_not_merge_thumbnail_list_with_ordered_body_variants(self):
        payload = {
            "result": {
                "link": {
                    "img_list": [
                        "https://img.example/a.jpg?imageMogr2/thumbnail/400x",
                        "https://img.example/c.jpg?imageMogr2/thumbnail/400x",
                    ],
                    "content": [
                        {"type": "img", "url": "https://img.example/a.jpg"},
                        {"type": "img", "url": "https://img.example/b.jpg"},
                        {"type": "img", "url": "https://img.example/c.jpg"},
                    ],
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            [
                "https://img.example/a.jpg",
                "https://img.example/b.jpg",
                "https://img.example/c.jpg",
            ],
        )

    def test_ignores_versioned_comment_and_reply_subtrees(self):
        payload = {
            "result": {
                "link": {
                    "content": {
                        "children": [
                            {"type": "img", "url": "https://img.example/body.jpg"},
                            {
                                "comment_info_v2": {
                                    "images": ["https://img.example/comment.jpg"]
                                }
                            },
                            {
                                "reply_info_v2": {
                                    "images": ["https://img.example/reply.jpg"]
                                }
                            },
                        ]
                    }
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            ["https://img.example/body.jpg"],
        )

    def test_extracts_nested_content_text_json_and_preserves_more_than_twelve(self):
        urls = [f"https://img.example/body-{index:02d}.jpg" for index in range(1, 16)]
        payload = {
            "result": {
                "link": {
                    "link_id": "post-1",
                    "content": {
                        "text": json.dumps(
                            [
                                {"type": "text", "text": "opening"},
                                *[
                                    {"type": "img", "original_url": url}
                                    for url in urls
                                ],
                                {"type": "text", "text": "ending"},
                            ]
                        )
                    },
                }
            }
        }

        self.assertEqual(XiaoheihePlugin._extract_article_img_urls(payload), urls)

    def test_extracts_top_level_image_list_with_mixed_item_shapes(self):
        payload = {
            "result": {
                "article": {
                    "image_url_list": [
                        "https://img.example/one.jpg",
                        {"image_url": "https://img.example/two.jpg"},
                        {"originalUrl": "https://img.example/three.jpg"},
                    ]
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            [
                "https://img.example/one.jpg",
                "https://img.example/two.jpg",
                "https://img.example/three.jpg",
            ],
        )

    def test_prefers_complete_body_order_over_partial_named_list(self):
        payload = {
            "result": {
                "post": {
                    "img_list": [
                        {"url": "https://img.example/first.jpg"},
                        {"url": "https://img.example/shared.jpg"},
                    ],
                    "content": {
                        "text": [
                            {"type": "img", "url": "https://img.example/shared.jpg"},
                            {"type": "img", "url": "https://img.example/last.jpg"},
                            {"type": "img", "url": "https://img.example/first.jpg"},
                        ]
                    },
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            [
                "https://img.example/shared.jpg",
                "https://img.example/last.jpg",
                "https://img.example/first.jpg",
            ],
        )

    def test_ignores_images_nested_under_comments_and_replies(self):
        payload = {
            "result": {
                "link": {
                    "content": {
                        "text": [
                            {"type": "img", "url": "https://img.example/body.jpg"},
                        ]
                    },
                    "comments": [
                        {
                            "content": {
                                "text": [
                                    {
                                        "type": "img",
                                        "url": "https://img.example/comment.jpg",
                                    }
                                ]
                            },
                            "replies": [
                                {
                                    "images": [
                                        "https://img.example/reply.jpg",
                                    ]
                                }
                            ],
                        }
                    ],
                },
                "comment_list": [
                    {"images": ["https://img.example/result-comment.jpg"]}
                ],
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            ["https://img.example/body.jpg"],
        )

    def test_handles_json_encoded_article_text_at_top_level(self):
        payload = {
            "result": {
                "link": {
                    "text": json.dumps(
                        {
                            "content": [
                                {
                                    "type": "img",
                                    "src": "https://img.example/nested-object.jpg",
                                }
                            ]
                        }
                    )
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            ["https://img.example/nested-object.jpg"],
        )

    def test_extracts_url_from_nested_data_and_attrs_wrappers(self):
        payload = {
            "result": {
                "link": {
                    "content": [
                        {
                            "type": "image",
                            "data": {
                                "attrs": {
                                    "original_url": "https://img.example/wrapped.jpg"
                                }
                            },
                        }
                    ]
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            ["https://img.example/wrapped.jpg"],
        )

    def test_accepts_block_type_as_the_image_kind_discriminator(self):
        payload = {
            "result": {
                "post": {
                    "content": {
                        "blocks": [
                            {
                                "block_type": "picture",
                                "attrs": {"src": "https://img.example/block-type.jpg"},
                            }
                        ]
                    }
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            ["https://img.example/block-type.jpg"],
        )

    def test_extracts_images_list_nested_inside_article_body(self):
        payload = {
            "result": {
                "article": {
                    "content": {
                        "sections": [
                            {
                                "type": "paragraph",
                                "images": [
                                    {
                                        "data": {
                                            "url": "https://img.example/section-one.jpg"
                                        }
                                    },
                                    "https://img.example/section-two.jpg",
                                ],
                            }
                        ]
                    }
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            [
                "https://img.example/section-one.jpg",
                "https://img.example/section-two.jpg",
            ],
        )

    def test_excludes_author_and_comment_subtrees_from_body_walk(self):
        payload = {
            "result": {
                "link": {
                    "content": {
                        "blocks": [
                            {
                                "type": "img",
                                "url": "https://img.example/body-only.jpg",
                            }
                        ],
                        "author": {
                            "type": "img",
                            "url": "https://img.example/author-avatar.jpg",
                            "images": ["https://img.example/author-gallery.jpg"],
                        },
                        "comment_list": [
                            {
                                "type": "image",
                                "data": {"url": "https://img.example/comment.jpg"},
                                "replyInfo": {
                                    "images": [
                                        "https://img.example/comment-reply.jpg"
                                    ]
                                },
                            }
                        ],
                    }
                }
            }
        }

        self.assertEqual(
            XiaoheihePlugin._extract_article_img_urls(payload),
            ["https://img.example/body-only.jpg"],
        )

    def test_returns_empty_when_payload_has_no_article_body_images(self):
        payload = {
            "result": {
                "link": {
                    "link_id": "text-only-post",
                    "content": {
                        "blocks": [
                            {"type": "text", "text": "No image in this body."}
                        ],
                        "author": {
                            "avatar": {
                                "type": "image",
                                "url": "https://img.example/not-body-avatar.jpg",
                            }
                        },
                        "comments": [
                            {
                                "images": [
                                    "https://img.example/not-body-comment.jpg"
                                ]
                            }
                        ],
                    },
                }
            }
        }

        self.assertEqual(XiaoheihePlugin._extract_article_img_urls(payload), [])


if __name__ == "__main__":
    unittest.main()
