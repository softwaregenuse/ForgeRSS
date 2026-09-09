#!/usr/bin/env python3

import html
import json
import re
import sys
from datetime import datetime
from email.utils import format_datetime
from urllib.parse import urljoin

import requests


PAGE_URL = (
    "https://topic17z2k407.jin10.com/"
    "topic/jin10_important_news.html?from=web_homepage"
)

API_PATH = "/top/flashsByTime?time_type=time&sort=priority"

# 当前观察到的 API Host，自动发现失败时作为备用
FALLBACK_API_HOST = (
    "1b8d6028d99849668a6d8755c79e650f.z3c.jin10.com"
)

X_APP_ID = "EzF2s2HxxU0U5bYa"
X_VERSION = "1.0.0"

OUTPUT_FILE = "feeds/jin10_important.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def discover_api_host(session: requests.Session) -> str:
    """
    尝试从页面和页面引用的 JS 中自动发现 *.z3c.jin10.com API 域名。
    失败时使用当前已知域名。
    """

    try:
        response = session.get(
            PAGE_URL,
            timeout=20,
            headers={
                "User-Agent": USER_AGENT,
            },
        )
        response.raise_for_status()

        page = response.text

        # 先直接从 HTML 搜
        match = re.search(
            r'([a-zA-Z0-9]+\.z3c\.jin10\.com)',
            page,
        )

        if match:
            return match.group(1)

        # 找页面 JS
        scripts = re.findall(
            r'<script[^>]+src=["\']([^"\']+)["\']',
            page,
            flags=re.I,
        )

        for src in scripts:
            js_url = urljoin(PAGE_URL, src)

            try:
                js_response = session.get(
                    js_url,
                    timeout=20,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": PAGE_URL,
                    },
                )

                if not js_response.ok:
                    continue

                match = re.search(
                    r'([a-zA-Z0-9]+\.z3c\.jin10\.com)',
                    js_response.text,
                )

                if match:
                    return match.group(1)

            except Exception:
                continue

    except Exception as exc:
        print(
            f"API host discovery warning: {exc}",
            file=sys.stderr,
        )

    return FALLBACK_API_HOST


def fetch_items():
    session = requests.Session()

    # 先访问页面，模拟正常浏览器会话
    try:
        session.get(
            PAGE_URL,
            timeout=20,
            headers={
                "User-Agent": USER_AGENT,
            },
        )
    except Exception:
        pass

    api_host = discover_api_host(session)

    api_url = f"https://{api_host}{API_PATH}"

    print(f"Using Jin10 API: {api_url}")

    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://topic17z2k407.jin10.com",
        "Referer": "https://topic17z2k407.jin10.com/",
        "User-Agent": USER_AGENT,
        "x-app-id": X_APP_ID,
        "x-version": X_VERSION,
    }

    response = session.get(
        api_url,
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    if payload.get("status") != 200:
        raise RuntimeError(
            f"Jin10 API status: {payload.get('status')}"
        )

    data = payload.get("data", [])

    if not isinstance(data, list):
        raise RuntimeError("Unexpected Jin10 response format")

    return data


def extract_title(inner: dict, content: str) -> str:
    title = (inner.get("title") or "").strip()

    if title:
        return title

    # 普通快讯通常正文以 【标题】 开头
    match = re.match(r"【([^】]+)】", content)

    if match:
        return match.group(1).strip()

    clean = re.sub(r"\s+", " ", content).strip()

    if len(clean) > 60:
        return clean[:60] + "…"

    return clean or "金十重要事件"


def choose_link(item_id: str, inner: dict) -> str:
    link = (inner.get("link") or "").strip()

    if link:
        return link

    source_link = (inner.get("source_link") or "").strip()

    if source_link:
        return source_link

    return f"https://flash.jin10.com/detail/{item_id}"


def make_description(inner: dict, outer: dict) -> str:
    parts = []

    pic = (inner.get("pic") or "").strip()

    if pic:
        parts.append(
            f'<p><img src="{html.escape(pic)}" '
            f'style="max-width:100%;height:auto;" /></p>'
        )

    content = (inner.get("content") or "").strip()

    if content:
        parts.append(
            f"<p>{html.escape(content)}</p>"
        )

    source = (inner.get("source") or "").strip()

    if source:
        parts.append(
            f"<p>来源：{html.escape(source)}</p>"
        )

    remarks = outer.get("remark") or []

    if isinstance(remarks, list):
        remark_parts = []

        for remark in remarks:
            if not isinstance(remark, dict):
                continue

            title = (remark.get("title") or "").strip()
            content = (remark.get("content") or "").strip()
            link = (
                remark.get("link")
                or remark.get("url")
                or ""
            ).strip()

            text = ""

            if title:
                text += html.escape(title)

            if content:
                if text:
                    text += "："
                text += html.escape(content)

            if link and title:
                text = (
                    f'<a href="{html.escape(link)}">'
                    f"{html.escape(title)}</a>"
                )

                if content:
                    text += "：" + html.escape(content)

            if text:
                remark_parts.append(f"<li>{text}</li>")

        if remark_parts:
            parts.append(
                "<p><strong>相关信息</strong></p>"
                "<ul>"
                + "".join(remark_parts)
                + "</ul>"
            )

    return "".join(parts)


def parse_pubdate(value: str) -> str:
    try:
        dt = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        )

        # 金十返回的 time 按北京时间理解
        from datetime import timezone, timedelta

        cn_tz = timezone(timedelta(hours=8))
        dt = dt.replace(tzinfo=cn_tz)

        return format_datetime(dt)

    except Exception:
        return ""


def generate_rss(items):
    rss_items = []

    for wrapper in items:
        if not isinstance(wrapper, dict):
            continue

        item_id = str(
            wrapper.get("item_id") or ""
        ).strip()

        outer = wrapper.get("data") or {}

        if not isinstance(outer, dict):
            continue

        inner = outer.get("data") or {}

        if not isinstance(inner, dict):
            continue

        content = (
            inner.get("content")
            or ""
        ).strip()

        title = extract_title(
            inner,
            content,
        )

        link = choose_link(
            item_id,
            inner,
        )

        pubdate = parse_pubdate(
            outer.get("time") or ""
        )

        description = make_description(
            inner,
            outer,
        )

        guid = item_id or link

        item_xml = f"""
        <item>
            <title>{html.escape(title)}</title>
            <link>{html.escape(link)}</link>
            <guid isPermaLink="false">{html.escape(guid)}</guid>
            <pubDate>{html.escape(pubdate)}</pubDate>
            <description><![CDATA[{description}]]></description>
        </item>
"""

        rss_items.append(item_xml)

    now = format_datetime(
        datetime.now().astimezone()
    )

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
    <channel>
        <title>金十数据 - 重要事件</title>
        <link>{html.escape(PAGE_URL)}</link>
        <description>金十数据重要事件 RSS</description>
        <language>zh-cn</language>
        <generator>ForgeRSS Jin10 Important</generator>
        <lastBuildDate>{html.escape(now)}</lastBuildDate>
        {''.join(rss_items)}
    </channel>
</rss>
"""

    return rss


def main():
    items = fetch_items()

    if not items:
        raise RuntimeError(
            "Jin10 returned no items; refusing to overwrite RSS"
        )

    rss = generate_rss(items)

    import os

    os.makedirs(
        "feeds",
        exist_ok=True,
    )

    tmp_file = OUTPUT_FILE + ".tmp"

    with open(
        tmp_file,
        "w",
        encoding="utf-8",
    ) as f:
        f.write(rss)

    os.replace(
        tmp_file,
        OUTPUT_FILE,
    )

    print(
        f"Generated {OUTPUT_FILE}: "
        f"{len(items)} items"
    )


if __name__ == "__main__":
    main()
