import re
import json
import asyncio
import base64
import io
import math
from urllib.parse import quote
from PIL import Image

from playwright.async_api import async_playwright, Browser, BrowserContext

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig


# 经过实际测试的 QQ/NapCat 发送安全锁，不属于用户配置，禁止增大。
MAX_IMAGE_DIMENSION = 16384
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024

DEVICE_SCALE_FACTOR = 2
MIN_JPEG_QUALITY = 50
PAGE_LOAD_TIMEOUT_MS = 60_000
SEARCH_SELECTOR_TIMEOUT_MS = 5_000
GAME_TAB_TIMEOUT_MS = 10_000
# 分享页首次检查前等待应用挂载；滚动后的等待供懒加载/动态正文完成渲染。
SHARE_APP_SETTLE_SECONDS = 4.0
LAZY_RENDER_SETTLE_SECONDS = 5.0


class XiaoheihePlugin(Star):
    """小黑盒游戏截图插件 - 移动端正文完整展开与裁切版"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cookies: str = config.get("cookies", "")
        self.enable_link_preview: bool = config.get("enable_link_preview", True)
        self.debug: bool = config.get("debug", False)

        self._playwright_manager = None
        self._playwright = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(2)

    def _log(self, message: str):
        if self.debug:
            logger.info(f"[小黑盒] {message}")

    async def _get_browser(self) -> Browser:
        async with self._browser_lock:
            if self._playwright_manager is None:
                self._playwright_manager = async_playwright()
                self._playwright = await self._playwright_manager.start()
            if self._browser is None or not self._browser.is_connected():
                self._browser = await self._playwright.chromium.launch(headless=True)
            return self._browser

    async def _create_context(self) -> BrowserContext:
        browser = await self._get_browser()
        # 伪装成 iPhone 14 Pro Max 手机，初始高度给 932
        context = await browser.new_context(
            viewport={"width": 430, "height": 932},
            device_scale_factor=DEVICE_SCALE_FACTOR,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            is_mobile=True,
            has_touch=True
        )
        if self.cookies:
            try:
                cookie_list =[{"name": p.split("=")[0].strip(), "value": p.split("=")[1].strip(), "domain": ".xiaoheihe.cn", "path": "/"} for p in self.cookies.split(";") if "=" in p]
                if cookie_list: await context.add_cookies(cookie_list)
            except Exception as e:
                self._log(f"Cookie 注入异常: {e}")
                await context.close()
                raise
        return context

    async def _open_link_page_with_capture(self, page, target_url: str) -> dict:
        """Open a shared post, retrying once when the web app renders no content."""
        last_state = None
        captcha_seen = False
        for attempt in range(2):
            state = self._start_article_image_capture(page)
            last_state = state
            try:
                await page.goto(target_url, wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)
                await asyncio.sleep(SHARE_APP_SETTLE_SECONDS)
                snapshot = await page.evaluate("""() => ({
                    sliderCount: document.querySelectorAll(
                        '.bbs-link-img-slider, .bbs-link-img-slider-box'
                    ).length,
                    imageCount: document.images.length,
                    textLength: (document.body?.innerText || '').trim().length,
                    documentHeight: Math.ceil(Math.max(
                        document.body?.scrollHeight || 0,
                        document.documentElement?.scrollHeight || 0,
                    )),
                    viewportHeight: window.innerHeight,
                })""")
            except Exception:
                await self._finish_article_image_capture(state)
                if attempt == 1:
                    raise
                self._log("分享页打开异常，正在进行一次有界重载")
                continue
            looks_empty = (
                snapshot["sliderCount"] == 0
                and snapshot["textLength"] < 50
                and snapshot["documentHeight"] <= snapshot["viewportHeight"] + 100
            )
            if not looks_empty:
                return state

            await self._finish_article_image_capture(state)
            captcha_seen = captcha_seen or state.get("captcha", False)
            if attempt == 1:
                if captcha_seen:
                    raise RuntimeError("小黑盒要求完成验证码或提供有效登录 Cookie")
                raise RuntimeError("小黑盒分享页连续两次未渲染正文")
            self._log("分享页未渲染正文，正在进行一次有界重载")

        return last_state

    # ==================== 核心：手机端量体裁衣截图 ====================

    @staticmethod
    def _extract_article_img_urls(payload) -> list[str]:
        """Extract only article-body images; comment images are intentionally ignored."""
        candidate_keys = {
            "imgurls", "imageurls", "imgurllist", "imageurllist",
            "images", "imagelist", "imglist", "imgs",
            "pictures", "picturelist", "piclist", "picurls", "pictureurls",
        }
        url_keys = (
            "originalurl", "originurl", "imageurl", "imgurl",
            "largeurl", "url", "src", "thumburl",
        )
        image_kinds = {"img", "image", "picture"}
        image_wrappers = {
            "data", "attrs", "attributes", "value", "image", "picture",
        }
        def normalize_key(value) -> str:
            return re.sub(r"[^a-z]", "", str(value).lower())

        def item_url(item) -> str | None:
            if isinstance(item, str):
                value = item.strip()
                return value if value.startswith(("https://", "http://")) else None
            if isinstance(item, dict):
                normalized = {normalize_key(key): value for key, value in item.items()}
                for key in url_keys:
                    value = normalized.get(key)
                    if isinstance(value, str) and value.strip().startswith(("https://", "http://")):
                        return value.strip()
            return None

        def nested_item_url(item, depth: int = 0) -> str | None:
            """Find an image URL in the small wrapper objects used by body blocks."""
            direct = item_url(item)
            if direct or depth >= 5 or not isinstance(item, dict):
                return direct
            for key, value in item.items():
                if normalize_key(key) in image_wrappers:
                    nested = nested_item_url(value, depth + 1)
                    if nested:
                        return nested
            return None

        def collect_named_list(value) -> list[str]:
            if not isinstance(value, list):
                return []
            urls: list[str] = []
            seen: set[str] = set()
            for item in value:
                url = nested_item_url(item)
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
            return urls

        def collect_content_blocks(value) -> list[str]:
            """Walk the article body JSON without descending into comments/replies."""
            ignored_prefixes = (
                "comment", "reply", "user", "author", "avatar", "profile",
            )
            urls: list[str] = []
            seen: set[str] = set()

            def append_url(url: str | None):
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)

            def walk(node, depth: int = 0):
                if depth > 30:
                    return
                if isinstance(node, str):
                    stripped = node.strip()
                    if stripped[:1] not in {"{", "["} or len(stripped) > 2_000_000:
                        return
                    try:
                        walk(json.loads(stripped), depth + 1)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return
                    return
                if isinstance(node, list):
                    for item in node:
                        walk(item, depth + 1)
                    return
                if not isinstance(node, dict):
                    return

                normalized = {normalize_key(key): child for key, child in node.items()}
                kind = normalize_key(normalized.get("type") or normalized.get("blocktype") or "")
                if kind in image_kinds:
                    append_url(nested_item_url(node))
                for key, child in node.items():
                    normalized_key = normalize_key(key)
                    if normalized_key.startswith(ignored_prefixes):
                        continue
                    if normalized_key in candidate_keys and isinstance(child, list):
                        for item in child:
                            append_url(nested_item_url(item))
                    walk(child, depth + 1)

            walk(value)
            return urls

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if not isinstance(payload, dict):
            return []

        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return []

        article = result.get("link") or result.get("article") or result.get("post")
        if not isinstance(article, dict):
            # Some share APIs return the article itself as result.
            article = result if any(key in result for key in ("linkid", "link_id", "share_url")) else None
        if not isinstance(article, dict):
            return []

        named_candidates: list[list[str]] = []
        for key, value in article.items():
            if normalize_key(key) in candidate_keys:
                urls = collect_named_list(value)
                if urls:
                    named_candidates.append(urls)

        body_urls: list[str] = []
        body_seen: set[str] = set()
        for body_key in ("content", "text"):
            content_urls = collect_content_blocks(article.get(body_key))
            for url in content_urls:
                if url not in body_seen:
                    body_seen.add(url)
                    body_urls.append(url)

        # A named list and the body tree often contain the same images using
        # different CDN variants. Do not concatenate those sources: prefer the
        # ordered body tree when equally complete, otherwise use the longest
        # single list exposed by the API.
        candidates = [*named_candidates]
        if body_urls:
            candidates.append(body_urls)
        return max(
            candidates,
            key=lambda urls: (len(urls), 1 if urls is body_urls else 0),
            default=[],
        )

    def _start_article_image_capture(self, page) -> dict:
        """Capture full image lists from Xiaoheihe article API responses."""
        loop = asyncio.get_running_loop()
        state = {
            "urls": [], "tasks": set(), "responses": 0,
            "last_activity": loop.time(), "closed": False,
            "page": page, "listener": None, "captcha": False,
        }

        async def inspect_response(response):
            try:
                payload = await response.json()
                if isinstance(payload, dict) and payload.get("status") == "show_captcha":
                    state["captcha"] = True
                urls = self._extract_article_img_urls(payload)
                if len(urls) > len(state["urls"]):
                    state["urls"] = urls
                    self._log(f"从文章接口捕获到 {len(urls)} 张图片")
                elif not urls:
                    response_status = payload.get("status") if isinstance(payload, dict) else None
                    response_message = payload.get("msg") if isinstance(payload, dict) else None
                    self._log(
                        "文章接口未解析出正文图片: "
                        f"status={response_status!r}, msg={response_message!r}, url={response.url}"
                    )
            except Exception as e:
                self._log(f"解析文章接口响应失败: {response.url} ({type(e).__name__})")

        def schedule_inspection(response):
            if state["closed"]:
                return
            response_url = response.url.lower()
            article_api_markers = (
                "/bbs/app/link/tree",
                "/bbs/app/api/web/share",
                "/v3/bbs/app/api/web/share",
                "/bbs/app/api/share",
            )
            if (
                "api.xiaoheihe.cn" not in response_url
                or not any(marker in response_url for marker in article_api_markers)
            ):
                return
            state["responses"] += 1
            state["last_activity"] = asyncio.get_running_loop().time()
            task = asyncio.create_task(inspect_response(response))
            state["tasks"].add(task)
            def finish_task(done_task):
                state["tasks"].discard(done_task)
                state["last_activity"] = asyncio.get_running_loop().time()
            task.add_done_callback(finish_task)

        state["listener"] = schedule_inspection
        page.on("response", schedule_inspection)
        return state

    async def _finish_article_image_capture(self, state: dict | None) -> list[str]:
        if not state:
            return []
        loop = asyncio.get_running_loop()
        finish_started = loop.time()
        deadline = finish_started + 5
        quiet_window = 0.6
        # Wait for response handlers that are created slightly after the page
        # appears, and require a short quiet period before declaring capture done.
        while loop.time() < deadline:
            pending = list(state["tasks"])
            if pending:
                remaining = max(0.1, deadline - loop.time())
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break
            quiet_anchor = max(finish_started, state.get("last_activity", finish_started))
            if not state["tasks"] and loop.time() - quiet_anchor >= quiet_window:
                break
            await asyncio.sleep(0.05)
        state["closed"] = True
        try:
            state["page"].remove_listener("response", state["listener"])
        except Exception:
            pass
        pending = list(state["tasks"])
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not state["urls"]:
            self._log(
                f"文章接口补图不可用（匹配响应 {state['responses']} 个），将使用页面现有图片"
            )
        return list(state["urls"])

    async def _extract_article_img_urls_from_page(self, page) -> list[str]:
        """Read the complete body image list retained by the current web app."""
        raw_items = await page.evaluate(r"""() => {
            const result = [];
            const seenObjects = new WeakSet();
            const visitedNodes = new WeakSet();
            const normalizeKey = value => String(value || '').toLowerCase().replace(/[^a-z]/g, '');
            const ignoredKeys = new Set([
                'comment', 'comments', 'commentlist', 'commentinfo',
                'reply', 'replies', 'replylist', 'replyinfo',
                'user', 'users', 'author', 'authors', 'avatar', 'profile',
            ]);
            const candidateKeys = new Set([
                'imgurls', 'imageurls', 'imgurllist', 'imageurllist',
                'images', 'imagelist', 'imglist', 'imgs',
                'pictures', 'picturelist', 'piclist', 'picurls', 'pictureurls',
            ]);
            const roots = [];
            const addRoot = value => {
                if (value && typeof value === 'object' && !roots.includes(value)) roots.push(value);
            };
            addRoot(window.__INITIAL_STATE__);
            addRoot(window.__INITIAL_DATA__);
            addRoot(window.__NUXT__);
            addRoot(window.__HEYBOX_STATE__);

            const app = document.querySelector('#app') || document.querySelector('#root');
            const findVueRoot = element => {
                if (!element || visitedNodes.has(element)) return;
                visitedNodes.add(element);
                addRoot(element.__vue_app__ && element.__vue_app__._instance);
                addRoot(element.__vueParentComponent);
                for (const key of Object.getOwnPropertyNames(element)) {
                    if (key.startsWith('__vue')) addRoot(element[key]);
                }
            };
            findVueRoot(app);

            const isArticle = object => {
                if (!object || typeof object !== 'object' || Array.isArray(object)) return false;
                const keys = Object.keys(object).map(normalizeKey);
                return keys.some(key => ['linkid', 'shareurl'].includes(key))
                    && keys.some(key => ['content', 'text', ...candidateKeys].includes(key));
            };
            const scan = (value, depth = 0, path = '') => {
                if (!value || typeof value !== 'object' || depth > 18) return;
                if (seenObjects.has(value)) return;
                seenObjects.add(value);
                if (isArticle(value)) {
                    try {
                        const json = JSON.stringify(value, (key, child) => {
                            if (ignoredKeys.has(normalizeKey(key))) return undefined;
                            return child;
                        });
                        if (json && json.length <= 2000000) result.push(JSON.parse(json));
                    } catch (_) {}
                }
                for (const [key, child] of Object.entries(value)) {
                    const normalized = normalizeKey(key);
                    if (ignoredKeys.has(normalized)) continue;
                    scan(child, depth + 1, path ? `${path}.${key}` : key);
                }
            };
            roots.forEach(root => scan(root));
            return result;
        }""")
        best: list[str] = []
        for article in raw_items or []:
            urls = self._extract_article_img_urls({"result": {"link": article}})
            if len(urls) > len(best):
                best = urls
        if best:
            self._log(f"从页面应用状态获取到 {len(best)} 张正文图片")
        return best

    async def _expand_article_image_sliders(self, page, article_image_urls: list[str] | None = None) -> dict:
        """优先使用正文原图并按比例展开；多图纵排，单图自适应高度。"""
        return await page.evaluate(r"""async (articleImageUrls) => {
            let expandedSliders = 0;
            let renderedCount = 0;
            let removedSlides = 0;
            const failedUrls = [];
            const suppliedUrls = Array.isArray(articleImageUrls)
                ? articleImageUrls.filter(url => typeof url === 'string' && /^https?:\/\//.test(url))
                : [];

            const extractBackgroundUrl = element => {
                if (!element) return null;
                const backgroundImage = window.getComputedStyle(element).backgroundImage;
                if (!backgroundImage || backgroundImage === 'none') return null;
                const match = backgroundImage.match(/^url\((['"]?)(.*)\1\)$/);
                return match ? match[2] : null;
            };

            const getOriginalImageUrl = imageUrl => {
                try {
                    const url = new URL(imageUrl, window.location.href);
                    const isHeyboxImage = /(^|\.)max-c\.com$/i.test(url.hostname);
                    const isArticleImage = /\/bbs\//i.test(url.pathname);
                    const isProcessed = url.search.includes('imageMogr2');
                    if (isHeyboxImage && isArticleImage && isProcessed) {
                        // “查看原图”的等效地址：保留源文件路径，移除 CDN
                        // 的缩略、格式转换和质量压缩参数。
                        url.search = '';
                        url.hash = '';
                        return url.href;
                    }
                } catch (_) {
                    // Invalid/relative URLs keep using the page-provided source.
                }
                return imageUrl;
            };

            const waitForImage = image => new Promise(resolve => {
                if (image.complete) {
                    resolve(image.naturalWidth > 0 && image.naturalHeight > 0);
                    return;
                }

                let settled = false;
                let timer = null;
                const finish = loaded => {
                    if (settled) return;
                    settled = true;
                    if (timer !== null) clearTimeout(timer);
                    image.onload = null;
                    image.onerror = null;
                    resolve(loaded);
                };
                image.onload = () => finish(true);
                image.onerror = () => finish(false);
                timer = setTimeout(() => finish(false), 10000);
            });

            const isCommentSlider = slider => Boolean(slider.closest(
                '[class*="comment" i], [class*="reply" i]'
            ));
            const sliders = Array.from(document.querySelectorAll('.bbs-link-img-slider'))
                .filter(slider => !isCommentSlider(slider));
            const articleSlider = sliders[0] || null;
            await Promise.all(sliders.map(async slider => {
                const box = slider.querySelector('.bbs-link-img-slider-box');
                const wrapper = box && box.querySelector('.swiper-wrapper');
                if (!box || !wrapper) return;

                let slides = Array.from(wrapper.children).filter(el =>
                    el.classList.contains('swiper-slide')
                );

                const fallbackHeight = Math.max(1, Math.round(box.getBoundingClientRect().height));

                // 停止 Swiper，避免调整截图视口高度时重新写入横向位移。
                const swiper = box.swiper;
                if (swiper && typeof swiper.destroy === 'function' && !swiper.destroyed) {
                    swiper.destroy(true, false);
                }

                // The mobile share component renders at most 12 slides. Preserve
                // those already-loaded DOM nodes and append only the missing ones.
                if (slider === articleSlider && suppliedUrls.length > 0) {
                    slides.forEach((slide, index) => {
                        const imageLayer = slide.querySelector('.bbs-link-img-slider-slide');
                        const existingImage = imageLayer && imageLayer.querySelector('img');
                        const existingUrl = (existingImage && (existingImage.currentSrc || existingImage.src))
                            || extractBackgroundUrl(imageLayer);
                        const suppliedUrl = suppliedUrls[index];
                        // Keep the page-provided URL as the first fallback. This
                        // also avoids assuming that two API lists always align.
                        if (suppliedUrl && (!existingUrl || suppliedUrl === existingUrl)) {
                            slide.dataset.astrbotImageUrl = suppliedUrl;
                        }
                        if (existingUrl) {
                            slide.dataset.astrbotExistingUrl = existingUrl;
                        }
                    });
                    suppliedUrls.slice(slides.length).forEach(url => {
                        const slide = document.createElement('div');
                        slide.className = 'swiper-slide';
                        slide.dataset.astrbotImageUrl = url;
                        slide.dataset.astrbotSynthetic = 'true';

                        const imageLayer = document.createElement('div');
                        imageLayer.className = 'bbs-link-img-slider-slide';
                        slide.appendChild(imageLayer);
                        wrapper.appendChild(slide);
                        slides.push(slide);
                    });
                }

                if (slides.length === 0) return;

                box.style.setProperty('height', 'auto', 'important');
                box.style.setProperty('overflow', 'visible', 'important');

                wrapper.style.setProperty('display', 'flex', 'important');
                wrapper.style.setProperty('flex-direction', 'column', 'important');
                wrapper.style.setProperty('width', '100%', 'important');
                wrapper.style.setProperty('height', 'auto', 'important');
                wrapper.style.setProperty('transform', 'none', 'important');
                wrapper.style.setProperty('transition', 'none', 'important');

                const processSlide = async slide => {
                    const imageLayer = slide.querySelector('.bbs-link-img-slider-slide');
                    const existingImage = imageLayer && imageLayer.querySelector('img');
                    const existingUrl = slide.dataset.astrbotExistingUrl
                        || (existingImage && (existingImage.currentSrc || existingImage.src))
                        || extractBackgroundUrl(imageLayer);
                    const imageUrl = slide.dataset.astrbotImageUrl || existingUrl;

                    slide.style.setProperty('display', 'block', 'important');
                    slide.style.setProperty('width', '100%', 'important');
                    slide.style.setProperty('flex-shrink', '0', 'important');
                    slide.style.setProperty('transform', 'none', 'important');
                    slide.style.setProperty('margin', '0', 'important');

                    if (!imageLayer || !imageUrl) {
                        slide.remove();
                        removedSlides += 1;
                        return;
                    }

                    const originalImageUrl = getOriginalImageUrl(imageUrl);
                    const candidateUrls = Array.from(new Set([
                        originalImageUrl,
                        imageUrl,
                        existingUrl && getOriginalImageUrl(existingUrl),
                        existingUrl,
                    ].filter(Boolean)));
                    let loadedImage = null;
                    for (const candidateUrl of candidateUrls) {
                        const image = document.createElement('img');
                        image.alt = '';
                        image.decoding = 'async';
                        image.loading = 'eager';
                        image.src = candidateUrl;
                        if (await waitForImage(image)) {
                            loadedImage = image;
                            break;
                        }
                    }

                    if (!loadedImage) {
                        failedUrls.push(imageUrl);
                        if (existingUrl && !slide.dataset.astrbotSynthetic) {
                            slide.style.setProperty('height', `${fallbackHeight}px`, 'important');
                            imageLayer.style.setProperty('background-size', 'contain', 'important');
                            return;
                        }
                        slide.remove();
                        removedSlides += 1;
                        return;
                    }

                    loadedImage.style.setProperty('display', 'block', 'important');
                    loadedImage.style.setProperty('width', '100%', 'important');
                    loadedImage.style.setProperty('height', 'auto', 'important');
                    loadedImage.style.setProperty('object-fit', 'contain', 'important');

                    slide.style.setProperty('height', 'auto', 'important');
                    imageLayer.style.setProperty('height', 'auto', 'important');
                    imageLayer.style.setProperty('background', 'none', 'important');
                    imageLayer.replaceChildren(loadedImage);
                    renderedCount += 1;
                };

                let cursor = 0;
                const workers = Array.from(
                    {length: Math.min(4, slides.length)},
                    async () => {
                        while (cursor < slides.length) {
                            const index = cursor++;
                            await processSlide(slides[index]);
                        }
                    }
                );
                await Promise.all(workers);

                const dots = slider.querySelector('.bbs-link-img-slider-dots');
                if (dots) dots.style.setProperty('display', 'none', 'important');

                slider.dataset.astrbotExpanded = 'true';
                expandedSliders += 1;
            }));

            return {
                expandedSliders,
                suppliedCount: suppliedUrls.length,
                renderedCount,
                removedSlides,
                failedUrls: failedUrls.slice(0, 5),
            };
        }""", article_image_urls or [])

    @staticmethod
    def _plan_screenshot_segments(
        total_height: int,
        segment_height: int = 2000,
        max_total_height: int | None = None,
    ) -> list[tuple[int, int]]:
        """Return non-overlapping ``(top, height)`` slices in CSS pixels."""
        total = max(0, int(total_height))
        if max_total_height is not None and total > max(0, int(max_total_height)):
            raise ValueError(
                f"Page content height {total} exceeds safety limit {max_total_height}"
            )
        step = max(1, int(segment_height))
        return [
            (top, min(step, total - top))
            for top in range(0, total, step)
        ]

    @staticmethod
    def _safe_image_size(width: int, height: int) -> tuple[int, int]:
        """Keep physical pixels intact unless a tested dimension lock is hit."""
        if width <= 0 or height <= 0:
            raise ValueError("Image dimensions must be positive")
        scale = min(
            1.0,
            MAX_IMAGE_DIMENSION / max(width, height),
            math.sqrt(MAX_IMAGE_PIXELS / (width * height)),
        )
        # Floor both axes: rounding up can cross the pixel or longest-edge lock.
        return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))

    @staticmethod
    def _crop_screenshot_segment(
        image_bytes: bytes,
        viewport_height: int,
        crop_top: float,
        crop_height: int,
    ) -> bytes:
        """Crop a viewport PNG using CSS coordinates while preserving DPR."""
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
            scale_y = image.height / max(1, viewport_height)
            top = round(crop_top * scale_y)
            bottom = round((crop_top + crop_height) * scale_y)
            if top < 0 or bottom > image.height or bottom <= top:
                raise ValueError("Screenshot segment crop is outside the viewport")
            cropped = image.crop((0, top, image.width, bottom))
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            return output.getvalue()

    @classmethod
    def _stitch_screenshot_segments(
        cls,
        parts: list[tuple[int, bytes]],
        total_height: int,
        css_width: int = 430,
    ) -> bytes:
        """Map CSS boundaries onto a safe DPR2 canvas; keep the result lossless."""
        if not parts or total_height <= 0:
            raise ValueError("No screenshot segments to stitch")

        ordered_parts = sorted(parts, key=lambda item: item[0])
        if ordered_parts[0][0] != 0:
            raise ValueError("Screenshot segments must start at CSS y=0")
        physical_width = css_width * DEVICE_SCALE_FACTOR
        output_width, output_height = cls._safe_image_size(
            physical_width, total_height * DEVICE_SCALE_FACTOR,
        )
        regions = []
        for index, (css_y, image_bytes) in enumerate(ordered_parts):
            next_css_y = ordered_parts[index + 1][0] if index + 1 < len(parts) else total_height
            if not 0 <= css_y < next_css_y <= total_height:
                raise ValueError("Invalid screenshot segment boundaries")
            with Image.open(io.BytesIO(image_bytes)) as source:
                source_height = (next_css_y - css_y) * DEVICE_SCALE_FACTOR
                if source.width != physical_width or source.height < source_height:
                    raise ValueError("Screenshot segment does not cover its DPR2 region")
                if source.height > source_height and index != len(parts) - 1:
                    raise ValueError("Screenshot segments overlap")
            regions.append((css_y * DEVICE_SCALE_FACTOR, next_css_y * DEVICE_SCALE_FACTOR, image_bytes))

        # Decide the bounded canvas size before allocating it, even for a
        # 100000 CSS px article. Scroll/crop coordinates always remain in CSS px.
        stitched = Image.new("RGB", (output_width, output_height), "white")
        physical_height = total_height * DEVICE_SCALE_FACTOR
        scale_y = output_height / physical_height
        needs_resize = (output_width, output_height) != (physical_width, physical_height)
        # Lanczos needs neighbouring rows on BOTH sides of a segment boundary.
        # Include its filter radius plus a rounding margin in source pixels.
        padding = math.ceil(3 / scale_y) + 1 if needs_resize else 0
        for start, end, _ in regions:
            # Shared absolute boundaries prevent accumulated rounding error.
            top = round(start * output_height / physical_height)
            bottom = round(end * output_height / physical_height)
            if bottom <= top:
                continue
            source_top = max(0, math.floor(top / scale_y) - padding)
            source_bottom = min(physical_height, math.ceil(bottom / scale_y) + padding)
            strip = Image.new("RGB", (physical_width, source_bottom - source_top), "white")
            for region_top, region_bottom, image_bytes in regions:
                overlap_top = max(source_top, region_top)
                overlap_bottom = min(source_bottom, region_bottom)
                if overlap_top >= overlap_bottom:
                    continue
                with Image.open(io.BytesIO(image_bytes)) as source:
                    crop = source.crop((0, overlap_top - region_top, physical_width,
                                        overlap_bottom - region_top)).convert("RGB")
                    strip.paste(crop, (0, overlap_top - source_top))
            if needs_resize:
                # Use the same sampling grid as one whole-image resize without
                # ever allocating that potentially enormous DPR2 source canvas.
                strip = strip.resize(
                    (output_width, bottom - top), Image.Resampling.LANCZOS,
                    box=(0, top / scale_y - source_top,
                         physical_width, bottom / scale_y - source_top),
                )
            stitched.paste(strip, (0, top))

        output = io.BytesIO()
        stitched.save(output, format="PNG")
        return output.getvalue()

    async def _capture_segmented_screenshot(
        self,
        page,
        content_height: int,
        css_width: int = 430,
        segment_height: int = 2000,
    ) -> bytes:
        """Capture a tall page in short viewports to avoid Chromium paint holes."""
        plans = self._plan_screenshot_segments(
            content_height,
            segment_height,
            max_total_height=100000,
        )
        if not plans:
            raise ValueError("Page content height is empty")

        total_height = plans[-1][0] + plans[-1][1]
        viewport_height = min(segment_height, total_height)
        await page.set_viewport_size({"width": css_width, "height": viewport_height})
        geometry = await page.evaluate(
            """async targetHeight => {
                const nextPaint = () => new Promise(resolve => requestAnimationFrame(
                    () => requestAnimationFrame(resolve)
                ));
                const pageHeight = () => Math.ceil(Math.max(
                    document.body?.scrollHeight || 0,
                    document.documentElement?.scrollHeight || 0,
                ));
                const setImportant = (element, property, value) => {
                    if (element) element.style.setProperty(property, value, 'important');
                };

                // A normal-flow spacer works for most pages, but it does not
                // extend the root scroll range when the app root is absolute,
                // constrained by a flex layout, or otherwise outside normal
                // document flow.  Keep it robust for flex layouts first.
                let spacer = document.querySelector('[data-astrbot-screenshot-spacer]');
                if (!spacer) {
                    spacer = document.createElement('div');
                    spacer.dataset.astrbotScreenshotSpacer = 'true';
                    spacer.setAttribute('aria-hidden', 'true');
                    document.body.appendChild(spacer);
                }
                setImportant(spacer, 'display', 'block');
                setImportant(spacer, 'position', 'static');
                setImportant(spacer, 'box-sizing', 'content-box');
                setImportant(spacer, 'width', '1px');
                setImportant(spacer, 'margin', '0');
                setImportant(spacer, 'padding', '0');
                setImportant(spacer, 'border', '0');
                setImportant(spacer, 'float', 'none');
                setImportant(spacer, 'clear', 'both');
                setImportant(spacer, 'align-self', 'stretch');
                setImportant(spacer, 'order', '2147483647');
                setImportant(spacer, 'pointer-events', 'none');

                const setSpacerHeight = height => {
                    const value = `${Math.max(0, Math.ceil(height))}px`;
                    setImportant(spacer, 'height', value);
                    setImportant(spacer, 'min-height', value);
                    setImportant(spacer, 'max-height', 'none');
                    setImportant(spacer, 'flex', `0 0 ${value}`);
                };

                setSpacerHeight(0);
                await nextPaint();
                const before = pageHeight();
                const missing = Math.max(0, targetHeight - before);
                setSpacerHeight(missing > 0 ? missing + 2 : 0);
                await nextPaint();

                let height = pageHeight();
                let rootFloorApplied = false;
                if (height < targetHeight) {
                    // Pin a one-pixel rail at the intended bottom and raise the
                    // root floor.  Unlike a body child, this also works when the
                    // visible app root is absolutely positioned.
                    let rail = document.querySelector('[data-astrbot-screenshot-rail]');
                    if (!rail) {
                        rail = document.createElement('div');
                        rail.dataset.astrbotScreenshotRail = 'true';
                        rail.setAttribute('aria-hidden', 'true');
                        document.documentElement.appendChild(rail);
                    }
                    setImportant(rail, 'display', 'block');
                    setImportant(rail, 'position', 'absolute');
                    setImportant(rail, 'top', `${Math.max(0, Math.ceil(targetHeight) - 1)}px`);
                    setImportant(rail, 'left', '0');
                    setImportant(rail, 'width', '1px');
                    setImportant(rail, 'height', '1px');
                    setImportant(rail, 'margin', '0');
                    setImportant(rail, 'padding', '0');
                    setImportant(rail, 'border', '0');
                    setImportant(rail, 'visibility', 'hidden');
                    setImportant(rail, 'pointer-events', 'none');

                    const floor = `${Math.ceil(targetHeight) + 2}px`;
                    [document.documentElement, document.body,
                        document.scrollingElement].filter(Boolean).forEach(root => {
                        setImportant(root, 'min-height', floor);
                        setImportant(root, 'max-height', 'none');
                    });
                    setImportant(document.documentElement, 'overflow-y', 'auto');
                    setImportant(document.body, 'overflow-y', 'visible');
                    rootFloorApplied = true;
                    await nextPaint();
                    height = pageHeight();
                }

                return {height, before, rootFloorApplied};
            }""",
            total_height,
        )
        document_height = max(0, int((geometry or {}).get("height", 0)))
        if document_height < total_height:
            # The root can still be non-scrollable on a highly constrained
            # third-party layout.  Do not silently crop the article or stitch
            # a blank tail that window.scrollTo() can never reach.
            self._log(
                "页面布局未能扩展到测量高度："
                f"目标 {total_height} px，实际可滚动 {document_height} px；"
                "已停止截图以避免裁掉正文"
            )
            raise RuntimeError(
                "Page layout prevents a complete screenshot "
                f"(scrollable {document_height}px, required {total_height}px)"
            )

        parts: list[tuple[int, bytes]] = []
        for index, (css_y, css_height) in enumerate(plans, start=1):
            # The last logical slice can be very short. Keep a stable viewport,
            # scroll to the lowest valid position, then crop just that slice.
            requested_scroll = min(css_y, max(0, total_height - viewport_height))
            actual_scroll = await page.evaluate(
                """async y => {
                    window.scrollTo(0, y);
                    await new Promise(resolve => requestAnimationFrame(
                        () => requestAnimationFrame(resolve)
                    ));
                    return window.scrollY;
                }""",
                requested_scroll,
            )
            crop_top = css_y - float(actual_scroll)
            if crop_top < -1 or crop_top + css_height > viewport_height + 1:
                raise RuntimeError(
                    "Page could not scroll to screenshot segment "
                    f"{index}/{len(plans)} (wanted y={css_y}, actual y={actual_scroll})"
                )

            viewport_png = await page.screenshot(
                type="png",
                scale="device",
                animations="disabled",
                caret="hide",
            )
            cropped_png = self._crop_screenshot_segment(
                viewport_png,
                viewport_height,
                crop_top,
                css_height,
            )
            parts.append((css_y, cropped_png))

        await page.evaluate("window.scrollTo(0, 0)")
        self._log(f"分段截图完成: {len(parts)} 段，总高度 {total_height} px")
        return self._stitch_screenshot_segments(
            parts,
            total_height,
            css_width,
        )

    async def _expand_article_text(self, page) -> dict:
        """Expand the article body before lazy loading, cleanup and measurement."""
        return await page.evaluate(r"""async () => {
            const expandLabels = new Set(['展开全文', '展开全部', '显示全文']);
            const excludedSelector = [
                '[class*="comment" i]', '[class*="reply" i]',
                '[class*="recommend" i]', '[class*="similar" i]'
            ].join(',');
            const normalizeText = value => (value || '').replace(/\s+/g, '').trim();
            const isVisible = element => {
                const style = window.getComputedStyle(element);
                if (
                    style.display === 'none'
                    || style.visibility === 'hidden'
                    || Number.parseFloat(style.opacity || '1') === 0
                ) return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const findCandidates = () => Array.from(
                document.querySelectorAll('button, a, [role="button"], div, span')
            )
                .filter(element => expandLabels.has(normalizeText(element.textContent)))
                .filter(element => isVisible(element) && !element.closest(excludedSelector));

            const targets = [];
            const seenTargets = new Set();
            findCandidates().forEach(candidate => {
                const target = candidate.closest('button, a, [role="button"]') || candidate;
                if (!seenTargets.has(target)) {
                    seenTargets.add(target);
                    targets.push(target);
                }
            });

            let clicked = 0;
            let skippedNavigation = 0;
            let remaining = targets.length;
            for (const target of targets) {
                if (target.tagName === 'A') {
                    const href = (target.getAttribute('href') || '').trim();
                    if (href && !href.startsWith('#') && !href.toLowerCase().startsWith('javascript:')) {
                        skippedNavigation += 1;
                        continue;
                    }
                }
                target.click();
                clicked += 1;
                await new Promise(resolve => setTimeout(resolve, 350));
                await new Promise(resolve => requestAnimationFrame(
                    () => requestAnimationFrame(resolve)
                ));
                remaining = findCandidates().length;
                if (remaining === 0) break;
            }

            remaining = findCandidates().length;
            return {
                matched: targets.length,
                clicked,
                remaining,
                skippedNavigation,
                expanded: targets.length > 0 && remaining === 0,
            };
        }""")

    async def _scroll_for_lazy_loading(self, page) -> None:
        """Scroll through the current article once so newly revealed media can load."""
        await page.evaluate("""async () => {
            await new Promise(resolve => {
                let totalHeight = 0;
                const distance = 400;
                const timer = setInterval(() => {
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    if (totalHeight >= document.body.scrollHeight - window.innerHeight) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, 150);
            });
        }""")

    async def _clean_and_measure_article_height(self, page) -> int:
        """Hide page chrome and measure the bottom-most actually painted content."""
        return await page.evaluate(r"""() => {
            const hideSelectors = [
                '[class*="comment" i]', '[class*="reply" i]', '[class*="recommend" i]',
                '[class*="similar" i]', '[class*="bottom-bar" i]', '[class*="download" i]',
                '[class*="open-app" i]', '[class*="footer" i]', '.publish-score-wrapper',
                '.link-section-tags', '.link-section-link-data',
                '.bbs-link-section-tags', '.bbs-link-section-bottom-info',
                '.article-expand-button'
            ];
            hideSelectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(element => {
                    element.style.setProperty('display', 'none', 'important');
                });
            });

            // The mobile share header is fixed and therefore hidden above, but
            // its app root keeps a 60px padding reservation unless reset.
            document.querySelectorAll('.has-fixed-share-header').forEach(element => {
                element.style.setProperty('padding-top', '0', 'important');
                element.style.setProperty('margin-top', '0', 'important');
            });

            const normalizeText = value => (value || '').replace(/\s+/g, '').trim();
            const removableLabels = new Set([
                '收起全文',
                '打开小黑盒查看全部精彩评论', '查看全部精彩评论',
                '打开小黑盒查看评论'
            ]);
            document.querySelectorAll('body *').forEach(element => {
                const text = normalizeText(element.textContent);
                const matched = removableLabels.has(text)
                    || Array.from(removableLabels).some(label => (
                        text.startsWith(label) && text.length <= label.length + 4
                    ));
                if (!matched) return;

                // Hide the widest wrapper whose only visible label is the same
                // text, so an icon/padding-only app prompt does not leave a gap.
                let target = element;
                while (
                    target.parentElement
                    && target.parentElement !== document.body
                    && normalizeText(target.parentElement.textContent) === text
                ) {
                    target = target.parentElement;
                }
                target.style.setProperty('display', 'none', 'important');
            });

            // A fixed/sticky element would be painted once per segment.
            document.querySelectorAll('*').forEach(element => {
                const style = window.getComputedStyle(element);
                if (style.position === 'fixed' || style.position === 'sticky') {
                    element.style.setProperty('display', 'none', 'important');
                }
            });

            document.documentElement.style.setProperty('scroll-behavior', 'auto', 'important');
            document.body.style.setProperty('scroll-behavior', 'auto', 'important');
            document.documentElement.style.setProperty('scroll-snap-type', 'none', 'important');
            document.body.style.setProperty('scroll-snap-type', 'none', 'important');

            // Remove framework-level viewport floors. Their boxes can extend
            // hundreds of pixels below the article even when they paint nothing.
            const layoutRoots = [
                document.documentElement,
                document.body,
                document.querySelector('#app'),
                document.querySelector('#root'),
                document.querySelector('.view-normal'),
            ].filter(Boolean);
            layoutRoots.forEach(element => {
                element.style.setProperty('min-height', '0', 'important');
                element.style.setProperty('height', 'auto', 'important');
            });

            const isVisible = element => {
                const style = window.getComputedStyle(element);
                if (
                    style.display === 'none'
                    || style.visibility === 'hidden'
                    || Number.parseFloat(style.opacity || '1') === 0
                ) return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const absoluteBottom = rect => rect.bottom + window.scrollY;

            let paintedBottom = 0;
            const includeRect = rect => {
                if (!rect || rect.width <= 0 || rect.height <= 0) return;
                paintedBottom = Math.max(paintedBottom, absoluteBottom(rect));
            };

            document.querySelectorAll('body *').forEach(element => {
                if (!isVisible(element)) return;
                const tag = element.tagName.toLowerCase();
                const style = window.getComputedStyle(element);

                // Text-node ranges measure glyph/line boxes without inheriting
                // the often oversized rectangle of their structural parent.
                for (const node of element.childNodes) {
                    if (node.nodeType !== Node.TEXT_NODE || !node.textContent?.trim()) continue;
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    for (const rect of range.getClientRects()) includeRect(rect);
                    range.detach?.();
                }

                if (tag === 'img') {
                    if (element.complete && element.naturalWidth > 0) {
                        includeRect(element.getBoundingClientRect());
                    }
                    return;
                }

                if ([
                    'svg', 'canvas', 'video', 'iframe', 'object', 'embed',
                    'input', 'textarea', 'select', 'button', 'hr'
                ].includes(tag)) {
                    includeRect(element.getBoundingClientRect());
                    return;
                }

                if (style.backgroundImage && style.backgroundImage !== 'none') {
                    includeRect(element.getBoundingClientRect());
                }
            });

            if (paintedBottom <= 0) {
                // Defensive fallback for an unusual page consisting only of
                // CSS boxes; never return an empty screenshot.
                const article = document.querySelector(
                    '.bbs-link, article, main, [class*="article" i]'
                );
                if (article && isVisible(article)) {
                    paintedBottom = absoluteBottom(article.getBoundingClientRect());
                } else {
                    paintedBottom = Math.min(
                        document.body?.scrollHeight || window.innerHeight,
                        window.innerHeight,
                    );
                }
            }

            return Math.max(1, Math.ceil(paintedBottom) + 20);
        }""")

    async def _prepare_and_screenshot(self, page, article_image_capture: dict | None = None) -> bytes:
        self._log("开始处理手机端页面排版...")

        # Use the same stable viewport for lazy loading, layout measurement and
        # segmented capture. Changing it only after measurement can reflow vh-
        # based content and make the measured height stale.
        await page.set_viewport_size({"width": 430, "height": 2000})

        # 1. 正文默认完整展开，再滚动触发展开后出现的懒加载图片。
        expand_result = await self._expand_article_text(page)
        if expand_result.get("clicked"):
            self._log(
                "正文已自动展开："
                f"匹配 {expand_result.get('matched', 0)} 个控件，"
                f"剩余 {expand_result.get('remaining', 0)} 个"
            )

        self._log("开始模拟滚动，加载图片...")
        await self._scroll_for_lazy_loading(page)

        await asyncio.sleep(LAZY_RENDER_SETTLE_SECONDS)
        final_expand_result = expand_result
        if (
            not expand_result.get("matched")
            or expand_result.get("remaining", 0) > 0
        ):
            late_expand_result = await self._expand_article_text(page)
            final_expand_result = late_expand_result
            if late_expand_result.get("clicked"):
                self._log("正文控件延迟出现，已完成第二次自动展开")
                await self._scroll_for_lazy_loading(page)
        if final_expand_result.get("remaining", 0) > 0:
            raise RuntimeError(
                "Article expansion did not complete "
                f"({final_expand_result['remaining']} control(s) still visible)"
            )

        article_image_urls = await self._finish_article_image_capture(article_image_capture)
        if len(article_image_urls) <= 12:
            page_state_urls = await self._extract_article_img_urls_from_page(page)
            if len(page_state_urls) > len(article_image_urls):
                article_image_urls = page_state_urls
        image_result = await self._expand_article_image_sliders(page, article_image_urls)
        if image_result.get("expandedSliders"):
            self._log(
                "文章图片展开完成: "
                f"API {image_result.get('suppliedCount', 0)} 张，"
                f"成功 {image_result.get('renderedCount', 0)} 张，"
                f"移除空节点 {image_result.get('removedSlides', 0)} 个"
            )
        if image_result.get("failedUrls"):
            self._log(f"加载失败的图片（最多5张）: {image_result['failedUrls']}")
        self._log("清理广告、评论，并测算真实高度...")

        # 2. 清理不需要的元素，并按实际绘制内容测量底边。
        content_height = await self._clean_and_measure_article_height(page)

        self._log(f"测算出的实际内容高度为: {content_height} px")

        # 3. 使用短视口分段截图。超长内容由分段截图内部的显式安全上限
        # 拒绝，不再静默裁掉正文尾部。
        # Chromium 对超高单视口可能只绘制前半段，DOM 中的后续图片虽然已加载，
        # 最终 JPEG 仍会出现大片纯白，因此不能再直接把 viewport 拉到正文总高度。
        # 4. 使用 2000px 的稳定视口逐段滚动、截图并拼接。
        image_bytes = await self._capture_segmented_screenshot(
            page,
            int(content_height),
            css_width=430,
            segment_height=2000,
        )

        return image_bytes

    # ==================== 指令与事件 ====================

    @filter.command("xiaoheihe", alias={"小黑盒"}, ignore_prefix=True)
    async def cmd_xiaoheihe(self, event: AstrMessageEvent, game: str = ""):
        if not game.strip():
            yield event.plain_result("请输入要搜索的游戏名称。\n用法：/小黑盒 <游戏名>")
            return
        async with self._semaphore:
            async for result in self._process_screenshot(event, game):
                yield result

    async def _process_screenshot(self, event: AstrMessageEvent, game: str):
        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()

            search_url = f"https://www.xiaoheihe.cn/app/search?q={quote(game)}"
            await page.goto(search_url, wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)

            success = False
            selectors =[
                'a[href*="/app/topic/game/"]',
                ".search-topic__topic-name",
                ".search-result__game .game-rank__game-card"
            ]
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=SEARCH_SELECTOR_TIMEOUT_MS)
                    if sel == 'a[href*="/app/topic/game/"]':
                        href = await page.get_attribute(sel, "href")
                        await page.goto(f"https://www.xiaoheihe.cn{href}", wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)
                    else:
                        async with page.expect_navigation(wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS):
                            await page.click(sel)
                        if sel == ".search-topic__topic-name":
                            await page.wait_for_selector(".slide-tab__tab-label", timeout=GAME_TAB_TIMEOUT_MS)
                            async with page.expect_navigation(wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS):
                                await page.click(".slide-tab__tab-label")
                    success = True
                    break
                except Exception:
                    continue

            if not success:
                yield self._base64_image_result(
                    event, await page.screenshot(type="png", full_page=True, scale="device")
                )
                return

            image_bytes = await self._prepare_and_screenshot(page)
            yield self._base64_image_result(event, image_bytes)

        except Exception as e:
            logger.error(f"[小黑盒] 截图失败: {e}")
            yield event.plain_result("截图失败，可能是页面加载超时。")
        finally:
            if context: await context.close()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.enable_link_preview: return
        target_url = self._extract_xiaoheihe_url(event)
        if not target_url: return

        async with self._semaphore:
            async for result in self._process_link_screenshot(event, target_url):
                yield result

    def _extract_xiaoheihe_url(self, event: AstrMessageEvent) -> str | None:
        url_pattern = re.compile(r"https?://(?:[a-z0-9.-]*\.)?xiaoheihe\.cn[^\s\"'<>]*", re.IGNORECASE)
        match = url_pattern.search(event.message_str or "")
        if match: return match.group(0)

        try:
            for seg in getattr(event.message_obj, "message", None) or[]:
                if getattr(seg, "type", "").lower() == "json":
                    raw_data = getattr(seg, "data", None)
                    if isinstance(raw_data, dict): raw_data = raw_data.get("data", raw_data)
                    json_text = json.dumps(raw_data, ensure_ascii=False) if isinstance(raw_data, (dict, list)) else str(raw_data)
                    m = url_pattern.search(json_text)
                    if m: return m.group(0)
        except Exception:
            pass
        return None

    async def _process_link_screenshot(self, event: AstrMessageEvent, target_url: str):
        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()
            article_image_capture = await self._open_link_page_with_capture(page, target_url)

            image_bytes = await self._prepare_and_screenshot(page, article_image_capture)
            yield self._base64_image_result(event, image_bytes)
        except Exception as e:
            logger.error(f"[小黑盒] 链接解析截图失败: {e}")
            if "验证码" in str(e) or "Cookie" in str(e):
                yield event.plain_result(
                    "小黑盒要求验证码；请稍后重试，或在插件配置中填写有效登录 Cookie。"
                )
            elif "Page layout prevents a complete screenshot" in str(e):
                yield event.plain_result(
                    "该小黑盒页面的布局暂不支持完整截图；已停止生成以避免漏掉正文。"
                )
            else:
                yield event.plain_result("小黑盒分享页暂时无法加载，请稍后重试。")
        finally:
            if context: await context.close()

    # ==================== 统一发送安全锁与生命周期 ====================

    @staticmethod
    def _encode_jpeg(image, quality: int) -> bytes:
        output = io.BytesIO()
        # Keep colour detail as well as luminance; all trials encode the same
        # lossless pixels, never a previously encoded JPEG.
        try:
            image.save(output, format="JPEG", quality=quality, subsampling=0, optimize=True)
        except OSError:
            # Pillow's optimized encoder guesses a whole-image buffer which
            # can be too small for high-entropy 4:4:4 JPEG at quality 100.
            # Retry its streaming encoder at the SAME quality and pixels.
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, subsampling=0, optimize=False)
        return output.getvalue()

    def _highest_quality_jpeg(self, image) -> tuple[bytes | None, int]:
        """Try 100 first, then search the integer quality range within the lock."""
        encoded = self._encode_jpeg(image, 100)
        if len(encoded) <= MAX_IMAGE_BYTES:
            return encoded, 100

        best = self._encode_jpeg(image, MIN_JPEG_QUALITY)
        if len(best) > MAX_IMAGE_BYTES:
            best = None
        quality = MIN_JPEG_QUALITY
        failed = {100}
        low, high = MIN_JPEG_QUALITY + 1, 99 if best is not None else MIN_JPEG_QUALITY
        while low <= high:
            middle = (low + high) // 2
            encoded = self._encode_jpeg(image, middle)
            if len(encoded) <= MAX_IMAGE_BYTES:
                best, quality = encoded, middle
                low = middle + 1
            else:
                failed.add(middle)
                high = middle - 1
        # Optimized JPEG sizes can have local reversals. Verify the untested
        # higher qualities before accepting a binary-search result (or reducing
        # resolution). The complete search still has only 51 integer qualities.
        for candidate_quality in range(99, quality, -1):
            if candidate_quality in failed:
                continue
            encoded = self._encode_jpeg(image, candidate_quality)
            if len(encoded) <= MAX_IMAGE_BYTES:
                return encoded, candidate_quality
        return best, quality

    def _normalize_for_qq(self, image_bytes: bytes) -> bytes:
        """Apply all three safety locks, then encode once from lossless pixels."""
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
        size = self._safe_image_size(*image.size)
        if size != image.size:
            self._log(f"发送安全锁调整尺寸: {image.size} -> {size}")
            image = image.resize(size, Image.Resampling.LANCZOS)

        normalized, quality = self._highest_quality_jpeg(image)
        if normalized is None:
            # Even quality 50 exceeds 10 MiB. Search integer longest-edge sizes
            # instead of taking arbitrary 10% steps. At most 14 trials with the
            # 16384 edge lock; every trial resizes the same lossless source.
            width, height = image.size
            longest = max(width, height)
            low, high = 1, longest - 1
            best_size = None
            while low <= high:
                edge = (low + high) // 2
                candidate_size = (
                    max(1, width * edge // longest),
                    max(1, height * edge // longest),
                )
                candidate = image.resize(candidate_size, Image.Resampling.LANCZOS)
                encoded = self._encode_jpeg(candidate, MIN_JPEG_QUALITY)
                if len(encoded) <= MAX_IMAGE_BYTES:
                    best_size = candidate_size
                    low = edge + 1
                else:
                    high = edge - 1
            if best_size is None:
                raise RuntimeError("Cannot encode an image within the QQ byte safety lock")
            self._log(f"文件体积安全锁调整尺寸: {image.size} -> {best_size}")
            image = image.resize(best_size, Image.Resampling.LANCZOS)
            normalized, quality = self._highest_quality_jpeg(image)

        # Fail closed: never let an encoder error or an oversized fallback
        # bypass the same locks used by normal and search-failure screenshots.
        if (
            normalized is None
            or len(normalized) > MAX_IMAGE_BYTES
            or max(image.size) > MAX_IMAGE_DIMENSION
            or image.width * image.height > MAX_IMAGE_PIXELS
        ):
            raise RuntimeError("Screenshot exceeds QQ image safety locks")
        self._log(
            f"最终 JPEG: {image.width}x{image.height}, quality={quality}, "
            f"{len(normalized)} bytes"
        )
        return normalized

    def _base64_image_result(self, event: AstrMessageEvent, image_bytes: bytes):
        """直接发送 Base64 图片，避免 OneBot/NapCat 依赖临时文件 URI。"""
        image_bytes = self._normalize_for_qq(image_bytes)
        return event.make_result().base64_image(
            base64.b64encode(image_bytes).decode("ascii")
        )

    async def terminate(self):
        async with self._browser_lock:
            if self._browser and self._browser.is_connected(): await self._browser.close()
            if self._playwright_manager: await self._playwright_manager.stop()
