#!/usr/bin/env python3
# Copyright (C) 2026 ForgeRSS Contributors
# Licensed under AGPL-3.0

"""
Xueqiu (雪球) User Feed Generator.

No login required. Uses DrissionPage headless to bypass Aliyun WAF,
then parses the timeline HTML directly.

Configuration:
1. XUEQIU_USER_ID env var (comma-separated for multiple users). Accepts:
     - pure uid     "8353550788"
     - full URL     "https://xueqiu.com/u/8353550788"

2. Optional: XUEQIU_MAX_POSTS (default 20)

Example:
    XUEQIU_USER_ID="8353550788" python scripts/run_single.py xueqiu_user
"""

import logging
import os
import random
import re
import tempfile
import shutil
import time
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import pytz
from bs4 import BeautifulSoup, Tag

from generators.base import Article, BaseFeedGenerator

logger = logging.getLogger(__name__)

CN_TZ = pytz.timezone("Asia/Shanghai")
BASE = "https://xueqiu.com"

# Detail fetch pacing (1.5-3.0s random jitter between detail page requests)
DETAIL_DELAY_RANGE = (1.5, 3.0)


def _parse_user_input(raw: str) -> tuple[str, str]:
    """Resolve user input into (uid, profile_url)."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    if raw.startswith(("http://", "https://")):
        m = re.search(r"/u/(\d+)", raw)
        if m:
            uid = m.group(1)
            return uid, f"{BASE}/u/{uid}"
        return raw, raw
    if raw.isdigit():
        return raw, f"{BASE}/u/{raw}"
    return raw, f"{BASE}/{raw}"


def _parse_relative_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse Xueqiu timestamp formats into aware UTC datetime.

    Examples:
      "12分钟前", "3小时前", "刚刚"
      "昨天 12:37"
      "05-16 12:36"            (this year, MM-DD HH:MM)
      "2024-12-15 09:30"       (full)
    """
    text = (text or "").strip()
    if not text:
        return None

    now = now or datetime.now(CN_TZ)

    if "刚刚" in text:
        return now.astimezone(pytz.UTC)

    m = re.match(r"(\d+)\s*分钟前", text)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).astimezone(pytz.UTC)

    m = re.match(r"(\d+)\s*小时前", text)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).astimezone(pytz.UTC)

    m = re.match(r"昨天\s+(\d{1,2}):(\d{2})", text)
    if m:
        y = now - timedelta(days=1)
        return CN_TZ.localize(
            datetime(y.year, y.month, y.day, int(m.group(1)), int(m.group(2)))
        ).astimezone(pytz.UTC)

    m = re.match(r"前天\s+(\d{1,2}):(\d{2})", text)
    if m:
        y = now - timedelta(days=2)
        return CN_TZ.localize(
            datetime(y.year, y.month, y.day, int(m.group(1)), int(m.group(2)))
        ).astimezone(pytz.UTC)

    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        return CN_TZ.localize(
            datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                     int(m.group(4)), int(m.group(5)))
        ).astimezone(pytz.UTC)

    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        return CN_TZ.localize(
            datetime(now.year, int(m.group(1)), int(m.group(2)),
                     int(m.group(3)), int(m.group(4)))
        ).astimezone(pytz.UTC)

    return None


def _is_avatar(img: Tag) -> bool:
    """Avatars sit inside .avatar containers or use the xavatar.imedao.com host."""
    parent_classes = " ".join(img.parent.get("class", []) if img.parent else [])
    if "avatar" in parent_classes.lower():
        return True
    src = (img.get("src") or img.get("data-src") or "").lower()
    if "xavatar.imedao.com" in src:
        return True
    if "/profiles/" in src and "identity_icon" in src:
        return True
    return False


def _normalize_img_src(src: str) -> str:
    """Xueqiu often serves protocol-relative URLs (//xqimg.imedao.com/...)."""
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    return src


def _extract_images(node: Tag) -> list[str]:
    out = []
    seen = set()
    for img in node.find_all("img"):
        if _is_avatar(img):
            continue
        src = _normalize_img_src(img.get("src") or img.get("data-src") or "")
        if src and src not in seen:
            seen.add(src)
            out.append(src)
    return out


def _clean_for_rss(node: Tag) -> str:
    """Strip non-content elements and return cleaned inner HTML.

    Removes:
      - Card-wide click-capture anchors (fake-anchor)
      - Unfold/expand controls (hidden in DOM but kept "收起" text)
      - Edit/delete/forward control rows
    Normalizes img src for protocol-relative URLs.
    """
    clone = BeautifulSoup(str(node), "html.parser")

    drop_selectors = [
        ".fake-anchor",
        ".timeline__unfold__control",
        ".timeline__expand__control",
        ".timeline__forward__unfold__control",
        ".timeline__item__control",
        ".timeline__item__forward__editor",
        ".timeline__item__info",       # author + time row (already in header)
        ".timeline__item__ft",         # forward/reply/like counters
        ".timeline__item__top__right", # "more" / "follow" buttons
        "script",
        "style",
        # Hidden source-attribution paragraph on detail pages
        # (e.g. "来源：雪球App，作者：XXX，（URL）")
        '[style*="display:none"]',
        '[style*="display: none"]',
    ]
    for sel in drop_selectors:
        for el in clone.select(sel):
            el.decompose()

    # Unwrap <h-char> / <h-inner> wrappers Xueqiu uses around CJK punctuation.
    # They render the literal character via inner text; the wrapper is pure
    # CSS-styling noise that bloats the RSS payload ~10x.
    for tag_name in ("h-char", "h-inner"):
        for el in clone.find_all(tag_name):
            el.unwrap()

    for img in clone.find_all("img"):
        if _is_avatar(img):
            img.decompose()
            continue
        src = _normalize_img_src(img.get("src") or img.get("data-src") or "")
        if src:
            img["src"] = src
        img["style"] = "max-width:100%;height:auto;border-radius:6px;margin:6px 0"

    for a in clone.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            a["href"] = urljoin(BASE + "/", href)

    return clone.decode_contents().strip()


class XueqiuUserGenerator(BaseFeedGenerator):
    """RSS generator for Xueqiu user posts (no login required)."""

    FEED_NAME = "xueqiu_user"
    FEED_TITLE = "Xueqiu User Posts"
    FEED_URL = "https://xueqiu.com/"
    FEED_DESCRIPTION = "Latest posts from Xueqiu users"
    FEED_LANGUAGE = "zh-CN"
    FEED_LOGO = "https://xueqiu.com/favicon.ico"

    USER_INPUTS = [
        u.strip()
        for u in os.environ.get("XUEQIU_USER_ID", "").split(",")
        if u.strip()
    ]

    MAX_POSTS = int(os.environ.get("XUEQIU_MAX_POSTS", "20"))

    def __init__(self):
        super().__init__()
        if not self.USER_INPUTS:
            self.logger.warning("No users configured. Set XUEQIU_USER_ID.")
            self.logger.warning(
                "Example: XUEQIU_USER_ID='8353550788' "
                "or full URL 'https://xueqiu.com/u/8353550788'"
            )

    def fetch_articles(self) -> list[Article]:
        try:
            from DrissionPage import ChromiumPage, ChromiumOptions
        except ImportError:
            self.logger.error("DrissionPage not installed. Run: pip install DrissionPage")
            return []

        per_user_cap = self.MAX_POSTS
        run_cap = getattr(self, "_run_max_articles", None)
        if run_cap is not None and run_cap < per_user_cap:
            self.logger.info(
                f"Run cap (--max {run_cap}) overrides XUEQIU_MAX_POSTS={per_user_cap}"
            )
            per_user_cap = run_cap

        all_articles: list[Article] = []
        self._first_user_name: Optional[str] = None

        for raw in self.USER_INPUTS:
            uid, url = _parse_user_input(raw)
            if not url:
                continue
            self.logger.info(f"Fetching xueqiu user {uid} ({url})")
            try:
                items = self._fetch_user(uid, url, per_user_cap, ChromiumPage, ChromiumOptions)
                all_articles.extend(items)
                self.logger.info(f"Got {len(items)} posts for {uid}")
            except Exception as e:
                self.logger.error(f"Failed to fetch user {uid}: {e}", exc_info=True)

        # Personalize feed channel when a single user is configured
        if len(self.USER_INPUTS) == 1 and self._first_user_name:
            self.FEED_TITLE = f"{self._first_user_name} (雪球)"
            self.FEED_DESCRIPTION = f"{self._first_user_name} 在雪球的最新动态"

        return all_articles

    def _fetch_user(
        self,
        uid: str,
        url: str,
        max_posts: int,
        ChromiumPage,
        ChromiumOptions,
    ) -> list[Article]:
        co = ChromiumOptions()
        co.set_address("127.0.0.1:9222")

        page = None
        try:
            page = ChromiumPage(co)
            try:
                page.run_js(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
            except Exception:
                pass

            page.get(url)
            time.sleep(6)

            html = page.html
            if not html or "aliyun_waf" in html:
                self.logger.error(f"WAF challenge not resolved for {url}")
                return []

            soup = BeautifulSoup(html, "html.parser")

            user_name = self._extract_user_name(soup)
            if user_name and self._first_user_name is None:
                self._first_user_name = user_name

            arts = soup.find_all("article")
            self.logger.info(f"Found {len(arts)} timeline cards on list page")

            parsed: list[Article] = []
            now = datetime.now(CN_TZ)

            for art in arts[:max_posts]:
                try:
                    info = self._parse_card(art, uid, user_name, now)
                except Exception as e:
                    self.logger.debug(f"Failed to parse a card: {e}")
                    continue
                if not info:
                    continue

                # Long-form posts: list contains only ~150-char summary. Fetch detail.
                if info["is_longtext"]:
                    time.sleep(random.uniform(*DETAIL_DELAY_RANGE))
                    detail_html = self._fetch_detail(page, info["url"])
                    if detail_html:
                        info = self._enrich_with_detail(info, detail_html)

                parsed.append(self._build_article(info))

            return parsed

        finally:
            if page is not None:
                try:
                    page.quit()
                except Exception:
                    pass

    def _extract_user_name(self, soup: BeautifulSoup) -> Optional[str]:
        t = soup.find("title")
        if t and t.text:
            # Normalize non-breaking spaces that appear in xueqiu page titles
            name = t.text.replace("\xa0", " ").strip()
            name = re.sub(r"\s*-\s*雪球\s*$", "", name).strip()
            if name:
                return name
        return None

    def _parse_card(
        self,
        art: Tag,
        feed_uid: str,
        feed_user_name: Optional[str],
        now: datetime,
    ) -> Optional[dict]:
        """Parse a single timeline article into a dict of fields.

        Returns dict with keys:
          url, status_id, author, time_text, published_at, is_longtext,
          title (optional), content_html, images, has_quote
        """
        # Main post identity: the .date-and-source anchor inside .timeline__item__info
        # carries data-id=<status_id> for the post itself (not the quoted one).
        # We pair it with feed_uid so URL stays correct even when the card also
        # contains a quoted post pointing to another user.
        date_el = art.select_one(".timeline__item__info .date-and-source")
        status_id = (date_el.get("data-id") if date_el else None) or ""
        if not status_id:
            # Fallback: first /<feed_uid>/<sid> link
            for a in art.find_all("a", href=True):
                m = re.match(rf"^/{feed_uid}/(\d+)$", a["href"])
                if m:
                    status_id = m.group(1)
                    break
        if not status_id:
            return None
        post_url = f"{BASE}/{feed_uid}/{status_id}"

        # Author name. The feed user is canonical for cards on their own page;
        # the card-level .user-name often resides inside the quote block and
        # would mis-attribute to the quoted user.
        author_name = feed_user_name or feed_uid

        # Timestamp — .date-and-source reads e.g. "12分钟前· 来自雪球".
        time_text = date_el.get_text(" ", strip=True) if date_el else ""
        time_text_clean = re.split(r"·|\s来自", time_text, maxsplit=1)[0].strip()
        published_at = _parse_relative_time(time_text_clean, now) or now.astimezone(pytz.UTC)

        # Type detection
        title_el = art.select_one(".timeline__item__title")
        is_longtext = bool(title_el and title_el.get_text(strip=True))
        forward_el = art.select_one(".timeline__item__forward")
        has_quote = bool(forward_el)

        # Main body content
        body_el = (
            art.select_one(".timeline__item__bd")
            or art.select_one(".timeline__item__main")
            or art
        )

        # Main post text (excluding any forward/quote block) — used for title
        # fallback on reply-type posts so we don't pull the quoted text in.
        main_text_el = art.select_one(".timeline__item__bd > .timeline__item__content")
        main_text = main_text_el.get_text(" ", strip=True) if main_text_el else ""

        title = title_el.get_text(strip=True) if title_el else None
        content_html = _clean_for_rss(body_el)
        images = _extract_images(body_el)

        return {
            "url": post_url,
            "status_id": status_id,
            "feed_uid": feed_uid,
            "author": author_name,
            "time_text": time_text_clean,
            "published_at": published_at,
            "is_longtext": is_longtext,
            "title": title,
            "main_text": main_text,
            "content_html": content_html,
            "images": images,
            "has_quote": has_quote,
        }

    def _fetch_detail(self, page, detail_url: str) -> Optional[str]:
        """Navigate to detail page in the same browser, return HTML."""
        try:
            page.get(detail_url)
            time.sleep(4)
            html = page.html
            if not html or "aliyun_waf" in html:
                self.logger.warning(f"WAF challenge on detail page {detail_url}")
                return None
            return html
        except Exception as e:
            self.logger.warning(f"Detail fetch failed for {detail_url}: {e}")
            return None

    def _enrich_with_detail(self, info: dict, detail_html: str) -> dict:
        """Replace summary content with full detail body."""
        soup = BeautifulSoup(detail_html, "html.parser")
        body = soup.select_one(".article__bd__detail")
        if not body:
            return info

        title_el = soup.select_one(".article__bd__title")
        if title_el and not info.get("title"):
            info["title"] = title_el.get_text(strip=True)

        info["content_html"] = _clean_for_rss(body)
        info["images"] = _extract_images(body) or info["images"]
        return info

    def _build_article(self, info: dict) -> Article:
        title = info.get("title")
        if not title:
            # Reply-type: prefer just the main post text (excluding the quote
            # block) so the title isn't padded with the quoted user's question.
            text = info.get("main_text") or BeautifulSoup(
                info["content_html"], "html.parser"
            ).get_text(" ", strip=True)
            title = text[:60] + ("…" if len(text) > 60 else "")
        if not title:
            title = f"雪球动态 {info['status_id']}"

        content_html = self._wrap_content(info)

        # No summary on purpose: rss_streaming prepends summary above the
        # full body, which would duplicate the visible text. The body in
        # `content` already carries the same information.
        return Article(
            url=info["url"],
            title=title,
            published_at=info["published_at"],
            content=content_html,
            summary=None,
            author=info["author"],
            images=info["images"],
            category="雪球",
        )

    def _wrap_content(self, info: dict) -> str:
        """Wrap parsed inner HTML with header (author + time) and footer (link)."""
        parts = ['<div style="font-size:16px;line-height:1.8;color:#333">']

        header_bits = []
        if info.get("author"):
            header_bits.append(
                f'<span style="font-weight:600">{html_escape(info["author"])}</span>'
            )
        if info.get("time_text"):
            header_bits.append(
                f'<span style="color:#888;font-size:13px">'
                f'{html_escape(info["time_text"])}</span>'
            )
        if header_bits:
            parts.append(
                '<div style="margin-bottom:12px;padding-bottom:8px;'
                'border-bottom:1px solid #eee">'
                + " · ".join(header_bits)
                + '</div>'
            )

        if info.get("is_longtext") and info.get("title"):
            parts.append(
                f'<h2 style="font-size:20px;margin:12px 0">'
                f'{html_escape(info["title"])}</h2>'
            )

        parts.append(info["content_html"])

        parts.append(
            f'<p style="margin-top:16px"><a href="{html_escape(info["url"])}" '
            f'style="color:#ff7d00">在雪球查看 &rarr;</a></p>'
        )
        parts.append("</div>")
        return "\n".join(parts)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Xueqiu User RSS")
    parser.add_argument("--max", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    gen = XueqiuUserGenerator()
    gen.run(full_refresh=args.full, max_articles=args.max)
