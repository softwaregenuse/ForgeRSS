#!/usr/bin/env python3
# Copyright (C) 2026 ForgeRSS Contributors
# Licensed under AGPL-3.0

"""
Xueqiu (雪球) User Feed Generator.

Uses Selenium headless Chrome to fetch Xueqiu user timeline pages,
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
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from html import escape as html_escape
from typing import Optional
from urllib.parse import urljoin

import pytz
from bs4 import BeautifulSoup, Tag

from generators.base import Article, BaseFeedGenerator


logger = logging.getLogger(__name__)

CN_TZ = pytz.timezone("Asia/Shanghai")
BASE = "https://xueqiu.com"

# Detail fetch pacing
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


def _parse_relative_time(
    text: str,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Parse Xueqiu timestamp formats into aware UTC datetime.

    Examples:
      "12分钟前"
      "3小时前"
      "刚刚"
      "昨天 12:37"
      "05-16 12:36"
      "2024-12-15 09:30"
    """
    text = (text or "").strip()

    if not text:
        return None

    now = now or datetime.now(CN_TZ)

    if "刚刚" in text:
        return now.astimezone(pytz.UTC)

    m = re.match(r"(\d+)\s*分钟前", text)
    if m:
        return (
            now - timedelta(minutes=int(m.group(1)))
        ).astimezone(pytz.UTC)

    m = re.match(r"(\d+)\s*小时前", text)
    if m:
        return (
            now - timedelta(hours=int(m.group(1)))
        ).astimezone(pytz.UTC)

    m = re.match(r"昨天\s+(\d{1,2}):(\d{2})", text)
    if m:
        y = now - timedelta(days=1)

        return CN_TZ.localize(
            datetime(
                y.year,
                y.month,
                y.day,
                int(m.group(1)),
                int(m.group(2)),
            )
        ).astimezone(pytz.UTC)

    m = re.match(r"前天\s+(\d{1,2}):(\d{2})", text)
    if m:
        y = now - timedelta(days=2)

        return CN_TZ.localize(
            datetime(
                y.year,
                y.month,
                y.day,
                int(m.group(1)),
                int(m.group(2)),
            )
        ).astimezone(pytz.UTC)

    m = re.match(
        r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})",
        text,
    )

    if m:
        return CN_TZ.localize(
            datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
            )
        ).astimezone(pytz.UTC)

    m = re.match(
        r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})",
        text,
    )

    if m:
        return CN_TZ.localize(
            datetime(
                now.year,
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
            )
        ).astimezone(pytz.UTC)

    return None


def _is_avatar(img: Tag) -> bool:
    """Detect avatar images."""
    parent_classes = " ".join(
        img.parent.get("class", [])
        if img.parent
        else []
    )

    if "avatar" in parent_classes.lower():
        return True

    src = (
        img.get("src")
        or img.get("data-src")
        or ""
    ).lower()

    if "xavatar.imedao.com" in src:
        return True

    if "/profiles/" in src and "identity_icon" in src:
        return True

    return False


def _normalize_img_src(src: str) -> str:
    """Normalize protocol-relative image URLs."""
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

        src = _normalize_img_src(
            img.get("src")
            or img.get("data-src")
            or ""
        )

        if src and src not in seen:
            seen.add(src)
            out.append(src)

    return out


def _clean_for_rss(node: Tag) -> str:
    """
    Strip non-content elements and return cleaned inner HTML.
    """
    clone = BeautifulSoup(
        str(node),
        "html.parser",
    )

    drop_selectors = [
        ".fake-anchor",
        ".timeline__unfold__control",
        ".timeline__expand__control",
        ".timeline__forward__unfold__control",
        ".timeline__item__control",
        ".timeline__item__forward__editor",
        ".timeline__item__info",
        ".timeline__item__ft",
        ".timeline__item__top__right",
        "script",
        "style",
        '[style*="display:none"]',
        '[style*="display: none"]',
    ]

    for sel in drop_selectors:
        for el in clone.select(sel):
            el.decompose()

    # Remove wrapper tags used by Xueqiu
    for tag_name in ("h-char", "h-inner"):
        for el in clone.find_all(tag_name):
            el.unwrap()

    for img in clone.find_all("img"):
        if _is_avatar(img):
            img.decompose()
            continue

        src = _normalize_img_src(
            img.get("src")
            or img.get("data-src")
            or ""
        )

        if src:
            img["src"] = src

        img["style"] = (
            "max-width:100%;"
            "height:auto;"
            "border-radius:6px;"
            "margin:6px 0"
        )

    for a in clone.find_all("a", href=True):
        href = a["href"]

        if href.startswith("/"):
            a["href"] = urljoin(
                BASE + "/",
                href,
            )

    return clone.decode_contents().strip()


class XueqiuUserGenerator(BaseFeedGenerator):
    """RSS generator for Xueqiu user posts."""

    FEED_NAME = "xueqiu_user"
    FEED_TITLE = "Xueqiu User Posts"
    FEED_URL = "https://xueqiu.com/"
    FEED_DESCRIPTION = "Latest posts from Xueqiu users"
    FEED_LANGUAGE = "zh-CN"
    FEED_LOGO = "https://xueqiu.com/favicon.ico"

    USER_INPUTS = [
        u.strip()
        for u in os.environ.get(
            "XUEQIU_USER_ID",
            "",
        ).split(",")
        if u.strip()
    ]

    MAX_POSTS = int(
        os.environ.get(
            "XUEQIU_MAX_POSTS",
            "20",
        )
    )

    def __init__(self):
        super().__init__()

        if not self.USER_INPUTS:
            self.logger.warning(
                "No users configured. "
                "Set XUEQIU_USER_ID."
            )

            self.logger.warning(
                "Example: "
                "XUEQIU_USER_ID='8353550788'"
            )

    def fetch_articles(self) -> list[Article]:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import (
                Options as ChromeOptions,
            )

        except ImportError:
            self.logger.error(
                "Selenium not installed. "
                "Run: pip install selenium"
            )
            return []

        per_user_cap = self.MAX_POSTS

        run_cap = getattr(
            self,
            "_run_max_articles",
            None,
        )

        if (
            run_cap is not None
            and run_cap < per_user_cap
        ):
            self.logger.info(
                f"Run cap (--max {run_cap}) overrides "
                f"XUEQIU_MAX_POSTS={per_user_cap}"
            )

            per_user_cap = run_cap

        all_articles: list[Article] = []

        self._first_user_name: Optional[str] = None

        for raw in self.USER_INPUTS:
            uid, url = _parse_user_input(raw)

            if not url:
                continue

            self.logger.info(
                f"Fetching xueqiu user "
                f"{uid} ({url})"
            )

            try:
                items = self._fetch_user(
                    uid,
                    url,
                    per_user_cap,
                    webdriver,
                    ChromeOptions,
                )

                all_articles.extend(items)

                self.logger.info(
                    f"Got {len(items)} posts "
                    f"for {uid}"
                )

            except Exception as e:
                self.logger.error(
                    f"Failed to fetch user {uid}: {e}",
                    exc_info=True,
                )

        # Personalize feed when single user
        if (
            len(self.USER_INPUTS) == 1
            and self._first_user_name
        ):
            self.FEED_TITLE = (
                f"{self._first_user_name} (雪球)"
            )

            self.FEED_DESCRIPTION = (
                f"{self._first_user_name}"
                " 在雪球的最新动态"
            )

        return all_articles

    def _fetch_user(
        self,
        uid: str,
        url: str,
        max_posts: int,
        webdriver,
        ChromeOptions,
    ) -> list[Article]:

        tmp_profile = tempfile.mkdtemp(
            prefix="selenium_xueqiu_"
        )

        driver = None

        try:
            options = ChromeOptions()

            options.binary_location = (
                "/usr/bin/google-chrome"
            )

            options.add_argument(
                "--headless=new"
            )

            options.add_argument(
                "--no-sandbox"
            )

            options.add_argument(
                "--disable-dev-shm-usage"
            )

            options.add_argument(
                "--disable-gpu"
            )

            options.add_argument(
                "--window-size=1920,1080"
            )

            options.add_argument(
                "--disable-blink-features="
                "AutomationControlled"
            )

            options.add_argument(
                "--lang=zh-CN"
            )

            options.add_argument(
                f"--user-data-dir={tmp_profile}"
            )

            self.logger.info(
                "Starting Selenium Chrome"
            )

            driver = webdriver.Chrome(
                options=options
            )

            # Hide webdriver marker
            try:
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": (
                            "Object.defineProperty("
                            "navigator,"
                            "'webdriver',"
                            "{get:()=>undefined}"
                            ");"
                        )
                    },
                )

            except Exception:
                pass

            self.logger.info(
                f"Opening Xueqiu user page: {url}"
            )

            driver.get(url)

            time.sleep(6)

            html = driver.page_source

            if not html:
                self.logger.error(
                    f"Empty HTML returned for {url}"
                )
                return []

            if "aliyun_waf" in html:
                self.logger.error(
                    f"WAF challenge not resolved "
                    f"for {url}"
                )
                return []

            soup = BeautifulSoup(
                html,
                "html.parser",
            )

            user_name = self._extract_user_name(
                soup
            )

            if (
                user_name
                and self._first_user_name is None
            ):
                self._first_user_name = user_name

            arts = soup.find_all("article")

            self.logger.info(
                f"Found {len(arts)} "
                "timeline cards on list page"
            )

            parsed: list[Article] = []

            now = datetime.now(CN_TZ)

            for art in arts[:max_posts]:
                try:
                    info = self._parse_card(
                        art,
                        uid,
                        user_name,
                        now,
                    )

                except Exception as e:
                    self.logger.debug(
                        f"Failed to parse a card: {e}"
                    )
                    continue

                if not info:
                    continue

                # Long-form posts:
                # fetch detail page
                if info["is_longtext"]:
                    time.sleep(
                        random.uniform(
                            *DETAIL_DELAY_RANGE
                        )
                    )

                    detail_html = (
                        self._fetch_detail(
                            driver,
                            info["url"],
                        )
                    )

                    if detail_html:
                        info = (
                            self._enrich_with_detail(
                                info,
                                detail_html,
                            )
                        )

                parsed.append(
                    self._build_article(info)
                )

            return parsed

        except Exception as e:
            self.logger.error(
                f"Selenium fetch failed "
                f"for {uid}: {e}",
                exc_info=True,
            )

            return []

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

            shutil.rmtree(
                tmp_profile,
                ignore_errors=True,
            )

    def _extract_user_name(
        self,
        soup: BeautifulSoup,
    ) -> Optional[str]:

        t = soup.find("title")

        if t and t.text:
            name = (
                t.text
                .replace("\xa0", " ")
                .strip()
            )

            name = re.sub(
                r"\s*-\s*雪球\s*$",
                "",
                name,
            ).strip()

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

        date_el = art.select_one(
            ".timeline__item__info "
            ".date-and-source"
        )

        status_id = (
            date_el.get("data-id")
            if date_el
            else None
        ) or ""

        if not status_id:
            for a in art.find_all(
                "a",
                href=True,
            ):
                m = re.match(
                    rf"^/{feed_uid}/(\d+)$",
                    a["href"],
                )

                if m:
                    status_id = m.group(1)
                    break

        if not status_id:
            return None

        post_url = (
            f"{BASE}/"
            f"{feed_uid}/"
            f"{status_id}"
        )

        author_name = (
            feed_user_name
            or feed_uid
        )

        time_text = (
            date_el.get_text(
                " ",
                strip=True,
            )
            if date_el
            else ""
        )

        time_text_clean = re.split(
            r"·|\s来自",
            time_text,
            maxsplit=1,
        )[0].strip()

        published_at = (
            _parse_relative_time(
                time_text_clean,
                now,
            )
            or now.astimezone(pytz.UTC)
        )

        title_el = art.select_one(
            ".timeline__item__title"
        )

        is_longtext = bool(
            title_el
            and title_el.get_text(strip=True)
        )

        forward_el = art.select_one(
            ".timeline__item__forward"
        )

        has_quote = bool(forward_el)

        body_el = (
            art.select_one(
                ".timeline__item__bd"
            )
            or art.select_one(
                ".timeline__item__main"
            )
            or art
        )

        main_text_el = art.select_one(
            ".timeline__item__bd "
            "> .timeline__item__content"
        )

        main_text = (
            main_text_el.get_text(
                " ",
                strip=True,
            )
            if main_text_el
            else ""
        )

        title = (
            title_el.get_text(strip=True)
            if title_el
            else None
        )

        content_html = _clean_for_rss(
            body_el
        )

        images = _extract_images(
            body_el
        )

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

    def _fetch_detail(
        self,
        driver,
        detail_url: str,
    ) -> Optional[str]:
        """
        Navigate to detail page using Selenium.
        """
        try:
            driver.get(detail_url)

            time.sleep(4)

            html = driver.page_source

            if (
                not html
                or "aliyun_waf" in html
            ):
                self.logger.warning(
                    f"WAF challenge on detail page "
                    f"{detail_url}"
                )

                return None

            return html

        except Exception as e:
            self.logger.warning(
                f"Detail fetch failed for "
                f"{detail_url}: {e}"
            )

            return None

    def _enrich_with_detail(
        self,
        info: dict,
        detail_html: str,
    ) -> dict:

        soup = BeautifulSoup(
            detail_html,
            "html.parser",
        )

        body = soup.select_one(
            ".article__bd__detail"
        )

        if not body:
            return info

        title_el = soup.select_one(
            ".article__bd__title"
        )

        if (
            title_el
            and not info.get("title")
        ):
            info["title"] = (
                title_el.get_text(
                    strip=True
                )
            )

        info["content_html"] = (
            _clean_for_rss(body)
        )

        info["images"] = (
            _extract_images(body)
            or info["images"]
        )

        return info

    def _build_article(
        self,
        info: dict,
    ) -> Article:

        title = info.get("title")

        if not title:
            text = (
                info.get("main_text")
                or BeautifulSoup(
                    info["content_html"],
                    "html.parser",
                ).get_text(
                    " ",
                    strip=True,
                )
            )

            title = (
                text[:60]
                + (
                    "…"
                    if len(text) > 60
                    else ""
                )
            )

        if not title:
            title = (
                f"雪球动态 "
                f"{info['status_id']}"
            )

        content_html = (
            self._wrap_content(info)
        )

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

    def _wrap_content(
        self,
        info: dict,
    ) -> str:

        parts = [
            '<div style="'
            'font-size:16px;'
            'line-height:1.8;'
            'color:#333">'
        ]

        header_bits = []

        if info.get("author"):
            header_bits.append(
                '<span style="'
                'font-weight:600">'
                f'{html_escape(info["author"])}'
                '</span>'
            )

        if info.get("time_text"):
            header_bits.append(
                '<span style="'
                'color:#888;'
                'font-size:13px">'
                f'{html_escape(info["time_text"])}'
                '</span>'
            )

        if header_bits:
            parts.append(
                '<div style="'
                'margin-bottom:12px;'
                'padding-bottom:8px;'
                'border-bottom:1px solid #eee">'
                + " · ".join(header_bits)
                + '</div>'
            )

        if (
            info.get("is_longtext")
            and info.get("title")
        ):
            parts.append(
                '<h2 style="'
                'font-size:20px;'
                'margin:12px 0">'
                f'{html_escape(info["title"])}'
                '</h2>'
            )

        parts.append(
            info["content_html"]
        )

        parts.append(
            '<p style="margin-top:16px">'
            f'<a href="{html_escape(info["url"])}" '
            'style="color:#ff7d00">'
            '在雪球查看 &rarr;'
            '</a>'
            '</p>'
        )

        parts.append("</div>")

        return "\n".join(parts)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Generate Xueqiu User RSS"
        )
    )

    parser.add_argument(
        "--max",
        type=int,
        default=20,
    )

    parser.add_argument(
        "--full",
        action="store_true",
    )

    args = parser.parse_args()

    gen = XueqiuUserGenerator()

    gen.run(
        full_refresh=args.full,
        max_articles=args.max,
    )
