#!/usr/bin/env python3
# Copyright (C) 2026 ForgeRSS Contributors
# Licensed under AGPL-3.0

"""
Xueqiu (雪球) User Feed Generator.

This version does NOT use Selenium.

Flow:

    CookieCloud
        ↓
    decrypt Xueqiu cookies
        ↓
    api.xueqiu.com
        ↓
    isolated RSS for one Xueqiu UID

Environment variables:

    XUEQIU_USER_ID
    XUEQIU_MAX_POSTS

    COOKIECLOUD_URL
    COOKIECLOUD_UUID
    COOKIECLOUD_PASSWORD

Each process handles exactly one Xueqiu UID.

Each user gets an independent feed identity:

    xueqiu_<UID>

Therefore BaseFeedGenerator automatically creates:

    cache/xueqiu_<UID>.json
    feeds/feed_xueqiu_<UID>.xml

and uses an independent SQLite feed_name.
"""

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from html import escape as html_escape
from typing import Optional

import pytz
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from generators.base import Article, BaseFeedGenerator


logger = logging.getLogger(__name__)

BASE_URL = "https://xueqiu.com"
API_URL = "https://api.xueqiu.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def _parse_user_input(raw: str) -> tuple[str, str]:
    """
    Resolve user input into:

        (uid, profile_url)
    """

    raw = (raw or "").strip()

    if not raw:
        return "", ""

    if raw.startswith(("http://", "https://")):
        match = re.search(r"/u/(\d+)", raw)

        if match:
            uid = match.group(1)

            return (
                uid,
                f"{BASE_URL}/u/{uid}",
            )

        return "", ""

    if raw.isdigit():
        return (
            raw,
            f"{BASE_URL}/u/{raw}",
        )

    return "", ""


def _evp_bytes_to_key(
    password: bytes,
    salt: bytes,
) -> tuple[bytes, bytes]:
    """
    OpenSSL-compatible EVP_BytesToKey.

    CryptoJS AES passphrase mode derives:

        32-byte AES key
        16-byte IV

    using MD5.
    """

    derived = b""
    block = b""

    while len(derived) < 48:
        block = hashlib.md5(
            block + password + salt
        ).digest()

        derived += block

    key = derived[:32]
    iv = derived[32:48]

    return key, iv


def _decrypt_cookiecloud(
    uuid: str,
    password: str,
    encrypted: str,
) -> dict:
    """
    Decrypt CookieCloud encrypted payload.
    """

    passphrase = hashlib.md5(
        f"{uuid}-{password}".encode("utf-8")
    ).hexdigest()[:16]

    raw = base64.b64decode(encrypted)

    if len(raw) < 16:
        raise ValueError(
            "CookieCloud encrypted data is too short"
        )

    if raw[:8] != b"Salted__":
        raise ValueError(
            "CookieCloud encrypted data has invalid header"
        )

    salt = raw[8:16]
    ciphertext = raw[16:]

    key, iv = _evp_bytes_to_key(
        passphrase.encode("utf-8"),
        salt,
    )

    cipher = AES.new(
        key,
        AES.MODE_CBC,
        iv,
    )

    decrypted = unpad(
        cipher.decrypt(ciphertext),
        AES.block_size,
    )

    return json.loads(
        decrypted.decode("utf-8")
    )


def _load_cookiecloud_payload() -> dict:
    """
    Download and decrypt CookieCloud payload.

    Secrets are read only from environment variables.
    They are never printed.
    """

    server = (
        os.environ.get(
            "COOKIECLOUD_URL",
            "",
        )
        .strip()
        .rstrip("/")
    )

    uuid = os.environ.get(
        "COOKIECLOUD_UUID",
        "",
    ).strip()

    password = os.environ.get(
        "COOKIECLOUD_PASSWORD",
        "",
    )

    if not server:
        raise RuntimeError(
            "COOKIECLOUD_URL is missing"
        )

    if not uuid:
        raise RuntimeError(
            "COOKIECLOUD_UUID is missing"
        )

    if not password:
        raise RuntimeError(
            "COOKIECLOUD_PASSWORD is missing"
        )

    url = f"{server}/get/{uuid}"

    logger.info(
        "Downloading encrypted cookies from CookieCloud"
    )

    response = requests.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    encrypted = data.get("encrypted")

    if not encrypted:
        raise RuntimeError(
            "CookieCloud response has no encrypted data"
        )

    payload = _decrypt_cookiecloud(
        uuid,
        password,
        encrypted,
    )

    logger.info(
        "CookieCloud data decrypted successfully"
    )

    return payload


def _get_xueqiu_cookies() -> list[dict]:
    """
    Extract only Xueqiu cookies from CookieCloud.

    Supports CookieCloud payload format:

        {
            "cookie_data": {
                ".xueqiu.com": [...],
                "xueqiu.com": [...]
            }
        }
    """

    payload = _load_cookiecloud_payload()

    cookie_data = payload.get(
        "cookie_data",
        {},
    )

    if not isinstance(cookie_data, dict):
        raise RuntimeError(
            "CookieCloud cookie_data is invalid"
        )

    result: list[dict] = []

    for domain_key, cookies in cookie_data.items():
        domain_text = str(
            domain_key or ""
        ).lower()

        if "xueqiu.com" not in domain_text:
            continue

        if not isinstance(cookies, list):
            continue

        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue

            name = cookie.get("name")
            value = cookie.get("value")

            if not name:
                continue

            if value is None:
                continue

            cookie_domain = str(
                cookie.get("domain")
                or domain_key
                or ""
            ).lower()

            if "xueqiu.com" not in cookie_domain:
                continue

            result.append(cookie)

    if not result:
        raise RuntimeError(
            "No Xueqiu cookies found in CookieCloud"
        )

    logger.info(
        f"Loaded {len(result)} Xueqiu cookies "
        "from CookieCloud"
    )

    return result


def _build_cookie_header(
    cookies: list[dict],
) -> str:
    """
    Build Cookie header manually.

    We deliberately build the header ourselves instead
    of relying on browser cookie-domain matching because
    the API host is api.xueqiu.com.
    """

    cookie_map: dict[str, str] = {}

    for cookie in cookies:
        name = str(
            cookie.get("name")
            or ""
        ).strip()

        value = str(
            cookie.get("value")
            or ""
        )

        if not name:
            continue

        cookie_map[name] = value

    if not cookie_map:
        raise RuntimeError(
            "Xueqiu cookie header is empty"
        )

    return "; ".join(
        f"{name}={value}"
        for name, value in cookie_map.items()
    )


def _strip_html(value: str) -> str:
    """
    Convert HTML into plain text.
    """

    if not value:
        return ""

    return BeautifulSoup(
        value,
        "html.parser",
    ).get_text(
        " ",
        strip=True,
    )


def _normalize_target(
    target: str,
    uid: str,
    status_id: str,
) -> str:
    """
    Convert Xueqiu target to absolute URL.
    """

    target = (
        target
        or ""
    ).strip()

    if target.startswith(
        ("http://", "https://")
    ):
        return target

    if target.startswith("/"):
        return BASE_URL + target

    if status_id:
        return (
            f"{BASE_URL}/"
            f"{uid}/"
            f"{status_id}"
        )

    return (
        f"{BASE_URL}/u/{uid}"
    )


class XueqiuUserGenerator(
    BaseFeedGenerator
):
    """
    RSS generator for one isolated Xueqiu user.
    """

    # Keep the class-level name.
    #
    # scripts/run_single.py uses this value to locate
    # the generator by the name "xueqiu_user".
    #
    # __init__ replaces the instance FEED_NAME with
    # xueqiu_<UID>.
    FEED_NAME = "xueqiu_user"

    FEED_TITLE = "Xueqiu User Posts"
    FEED_URL = "https://xueqiu.com/"
    FEED_DESCRIPTION = (
        "Latest posts from Xueqiu user"
    )
    FEED_LANGUAGE = "zh-CN"
    FEED_LOGO = (
        "https://xueqiu.com/favicon.ico"
    )

    MAX_POSTS = int(
        os.environ.get(
            "XUEQIU_MAX_POSTS",
            "20",
        )
    )

    def __init__(self):
        raw_inputs = [
            value.strip()
            for value in os.environ.get(
                "XUEQIU_USER_ID",
                "",
            ).split(",")
            if value.strip()
        ]

        if not raw_inputs:
            raise ValueError(
                "XUEQIU_USER_ID is not configured"
            )

        if len(raw_inputs) != 1:
            raise ValueError(
                "Xueqiu isolated feed mode "
                "requires exactly one "
                "XUEQIU_USER_ID per process"
            )

        uid, profile_url = (
            _parse_user_input(
                raw_inputs[0]
            )
        )

        if not uid:
            raise ValueError(
                "Invalid XUEQIU_USER_ID: "
                f"{raw_inputs[0]}"
            )

        if not uid.isdigit():
            raise ValueError(
                "XUEQIU_USER_ID must be numeric"
            )

        # Critical isolation setting.
        #
        # BaseFeedGenerator will now use:
        #
        # cache/xueqiu_<UID>.json
        # feeds/feed_xueqiu_<UID>.xml
        # SQLite feed_name=xueqiu_<UID>
        self.FEED_NAME = (
            f"xueqiu_{uid}"
        )

        self.FEED_URL = profile_url

        self.FEED_TITLE = (
            f"雪球用户 {uid}"
        )

        self.FEED_DESCRIPTION = (
            f"雪球用户 {uid} 的最新动态"
        )

        self._uid = uid
        self._profile_url = profile_url

        super().__init__()

        self.logger.info(
            "Xueqiu isolated feed initialized: "
            f"{self.FEED_NAME}"
        )

    def _request_timeline(
        self,
        max_posts: int,
    ) -> list[dict]:
        """
        Fetch timeline directly from api.xueqiu.com.
        """

        cookies = (
            _get_xueqiu_cookies()
        )

        cookie_header = (
            _build_cookie_header(
                cookies
            )
        )

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/json, "
                "text/plain, */*"
            ),
            "Referer": (
                self._profile_url
            ),
            "Origin": BASE_URL,
            "Cookie": cookie_header,
            "Connection": "keep-alive",
        }

        params = {
            "user_id": self._uid,
            "type": 10,
            "source": "",
            "page": 1,
            "count": min(
                max(
                    max_posts,
                    1,
                ),
                20,
            ),
        }

        url = (
            f"{API_URL}"
            "/v4/statuses/"
            "user_timeline.json"
        )

        self.logger.info(
            "Requesting Xueqiu timeline API"
        )

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
        )

        self.logger.info(
            "Xueqiu API HTTP status: "
            f"{response.status_code}"
        )

        if response.status_code != 200:
            body_preview = (
                response.text[:200]
                .replace("\n", " ")
            )

            self.logger.error(
                "Xueqiu API request failed. "
                f"Response preview: "
                f"{body_preview}"
            )

            response.raise_for_status()

        try:
            data = response.json()

        except Exception as exc:
            raise RuntimeError(
                "Xueqiu API did not return JSON"
            ) from exc

        if isinstance(data, dict):
            error_code = data.get(
                "error_code"
            )

            error_description = data.get(
                "error_description"
            )

            if error_code:
                raise RuntimeError(
                    "Xueqiu API error "
                    f"{error_code}: "
                    f"{error_description}"
                )

        statuses = data.get(
            "statuses",
            [],
        )

        if not isinstance(
            statuses,
            list,
        ):
            raise RuntimeError(
                "Xueqiu API statuses "
                "is not a list"
            )

        # Remove pinned posts, same behaviour
        # as current RSSHub route.
        statuses = [
            status
            for status in statuses
            if status.get("mark") != 1
        ]

        self.logger.info(
            f"Xueqiu API returned "
            f"{len(statuses)} posts"
        )

        return statuses[
            :max_posts
        ]

    def _status_to_article(
        self,
        status: dict,
    ) -> Optional[Article]:
        """
        Convert one API status into ForgeRSS Article.
        """

        status_id = str(
            status.get("id")
            or ""
        )

        target = _normalize_target(
            str(
                status.get(
                    "target"
                )
                or ""
            ),
            self._uid,
            status_id,
        )

        if not target:
            return None

        user = (
            status.get("user")
            or {}
        )

        author = (
            user.get("screen_name")
            or self._uid
        )

        created_at = status.get(
            "created_at"
        )

        if created_at:
            try:
                timestamp = (
                    float(created_at)
                    / 1000
                )

                published_at = (
                    datetime.fromtimestamp(
                        timestamp,
                        tz=pytz.UTC,
                    )
                )

            except Exception:
                published_at = (
                    datetime.now(
                        pytz.UTC
                    )
                )

        else:
            published_at = (
                datetime.now(
                    pytz.UTC
                )
            )

        title = str(
            status.get("title")
            or ""
        ).strip()

        description = str(
            status.get(
                "description"
            )
            or ""
        )

        text = str(
            status.get("text")
            or ""
        )

        body_html = (
            text
            or description
        )

        if not title:
            plain = _strip_html(
                description
                or text
            )

            if len(plain) > 80:
                title = (
                    plain[:80]
                    + "…"
                )

            else:
                title = plain

        if not title:
            title = (
                f"雪球动态 "
                f"{status_id}"
            )

        images: list[str] = []

        image_info_list = (
            status.get(
                "image_info_list"
            )
            or []
        )

        if isinstance(
            image_info_list,
            list,
        ):
            for image in image_info_list:
                if not isinstance(
                    image,
                    dict,
                ):
                    continue

                filename = (
                    image.get(
                        "filename"
                    )
                )

                if not filename:
                    continue

                image_url = (
                    "https://xqimg.imedao.com/"
                    + str(filename)
                )

                if image_url in images:
                    continue

                images.append(
                    image_url
                )

        retweeted = status.get(
            "retweeted_status"
        )

        if isinstance(
            retweeted,
            dict,
        ):
            retweet_user = (
                retweeted.get(
                    "user"
                )
                or {}
            )

            retweet_author = (
                retweet_user.get(
                    "screen_name"
                )
                or ""
            )

            retweet_text = str(
                retweeted.get(
                    "text"
                )
                or retweeted.get(
                    "description"
                )
                or ""
            )

            if retweet_text:
                body_html += (
                    "<blockquote>"
                )

                if retweet_author:
                    body_html += (
                        "<strong>"
                        + html_escape(
                            retweet_author
                        )
                        + "：</strong>"
                    )

                body_html += (
                    retweet_text
                    + "</blockquote>"
                )

        if images:
            image_html = []

            for image_url in images:
                image_html.append(
                    '<p>'
                    f'<img src="{html_escape(image_url)}" '
                    'style="max-width:100%;'
                    'height:auto;'
                    'border-radius:6px">'
                    '</p>'
                )

            body_html += (
                "\n"
                + "\n".join(
                    image_html
                )
            )

        body_html = (
            '<div style="'
            'font-size:16px;'
            'line-height:1.8;'
            'color:#333">'
            + body_html
            + '<p style="margin-top:16px">'
            + f'<a href="{html_escape(target)}" '
            + 'style="color:#ff7d00">'
            + '在雪球查看 &rarr;'
            + '</a>'
            + '</p>'
            + '</div>'
        )

        return Article(
            url=target,
            title=title,
            published_at=(
                published_at
            ),
            content=body_html,
            summary=None,
            author=author,
            images=images,
            category="雪球",
        )

    def fetch_articles(
        self,
    ) -> list[Article]:
        """
        BaseFeedGenerator entry point.
        """

        max_posts = (
            self.MAX_POSTS
        )

        run_cap = getattr(
            self,
            "_run_max_articles",
            None,
        )

        if (
            run_cap is not None
            and run_cap > 0
        ):
            max_posts = min(
                max_posts,
                run_cap,
            )

        if max_posts <= 0:
            max_posts = 20

        statuses = (
            self._request_timeline(
                max_posts
            )
        )

        if not statuses:
            self.logger.warning(
                "Xueqiu API returned "
                "no statuses"
            )

            return []

        first_user = (
            statuses[0].get(
                "user"
            )
            or {}
        )

        user_name = (
            first_user.get(
                "screen_name"
            )
        )

        if user_name:
            self.FEED_TITLE = (
                f"{user_name} (雪球)"
            )

            self.FEED_DESCRIPTION = (
                f"{user_name}"
                " 在雪球的最新动态"
            )

        articles: list[Article] = []

        for status in statuses:
            try:
                article = (
                    self._status_to_article(
                        status
                    )
                )

                if article:
                    articles.append(
                        article
                    )

            except Exception as exc:
                self.logger.warning(
                    "Failed to parse one "
                    "Xueqiu status: "
                    f"{exc}"
                )

        self.logger.info(
            f"Built {len(articles)} "
            "RSS articles"
        )

        return articles


if __name__ == "__main__":
    import argparse

    parser = (
        argparse.ArgumentParser(
            description=(
                "Generate Xueqiu "
                "User RSS"
            )
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

    generator = (
        XueqiuUserGenerator()
    )

    generator.run(
        full_refresh=args.full,
        max_articles=args.max,
    )
