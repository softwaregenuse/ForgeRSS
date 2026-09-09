#!/usr/bin/env python3

import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import urljoin

import requests


PAGE_URL = (
    "https://topic17z2k407.jin10.com/"
    "topic/jin10_important_news.html?from=web_homepage"
)

PAGE_ORIGIN = "https://topic17z2k407.jin10.com"
PAGE_REFERER = "https://topic17z2k407.jin10.com/"

USERINFO_URL = "https://uc-api.jin10.com/userinfo?forceUpdate=true"

API_PATH = "/top/flashsByTime?time_type=time&sort=priority"

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


def base_headers():
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": USER_AGENT,
        "Origin": PAGE_ORIGIN,
        "Referer": PAGE_REFERER,
        "x-app-id": X_APP_ID,
        "x-version": X_VERSION,
    }


def bootstrap_session(session: requests.Session):
    """
    模拟金十页面初始化流程：
    1. 访问重要事件页面
    2. 请求 userinfo
    3. 让 requests.Session 自动保存 x-token 等 Cookie
    """

    print("Opening Jin10 important-news page...")

    response = session.get(
        PAGE_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        },
        timeout=30,
    )

    print(
        "Page status:",
        response.status_code,
    )

    response.raise_for_status()

    print("Initializing Jin10 session...")

    userinfo = session.get(
        USERINFO_URL,
        headers=base_headers(),
        timeout=30,
    )

    print(
        "Userinfo status:",
        userinfo.status_code,
    )

    # 不输出 Cookie 内容，只输出名称
    cookie_names = [
        cookie.name
        for cookie in session.cookies
    ]

    print(
        "Session cookies:",
        cookie_names,
    )

    return response.text


def discover_api_hosts(
    session: requests.Session,
    page_html: str,
) -> list[str]:

    hosts = []

    def add_hosts(text: str):
        matches = re.findall(
            r'([a-zA-Z0-9]+\.z3c\.jin10\.com)',
            text or "",
        )

        for host in matches:
            if host not in hosts:
                hosts.append(host)

    # 先检查 HTML
    add_hosts(page_html)

    # 再检查页面引用的 JS
    scripts = re.findall(
        r'<script[^>]+src=["\']([^"\']+)["\']',
        page_html,
        flags=re.I,
    )

    for src in scripts:
        js_url = urljoin(
            PAGE_URL,
            src,
        )

        try:
            response = session.get(
                js_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": PAGE_URL,
                },
                timeout=20,
            )

            if response.ok:
                add_hosts(response.text)

        except Exception as exc:
            print(
                f"JS discovery warning: {exc}",
                file=sys.stderr,
            )

    if (
        FALLBACK_API_HOST
        and FALLBACK_API_HOST not in hosts
    ):
        hosts.append(FALLBACK_API_HOST)

    return hosts


def fetch_items():

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
        }
    )

    page_html = bootstrap_session(
        session
    )

    hosts = discover_api_hosts(
        session,
        page_html,
    )

    if not hosts:
        raise RuntimeError(
            "Could not discover any Jin10 API host"
        )

    print(
        "Discovered API hosts:",
        hosts,
    )

    headers = base_headers()

    headers["Content-Type"] = (
        "application/json"
    )

    last_error = None

    for host in hosts:

        api_url = (
            f"https://{host}"
            f"{API_PATH}"
        )

        print(
            f"Trying Jin10 API: {api_url}"
        )

        try:
            response = session.get(
                api_url,
                headers=headers,
                timeout=30,
            )

            print(
                "API HTTP status:",
                response.status_code,
            )

            if response.status_code != 200:
                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                )
                continue

            payload = response.json()

            if payload.get("status") != 200:
                last_error = RuntimeError(
                    "Jin10 API returned "
                    f"status={payload.get('status')}"
                )
                continue

            data = payload.get(
                "data",
                [],
            )

            if not isinstance(
                data,
                list,
            ):
                last_error = RuntimeError(
                    "Unexpected Jin10 response format"
                )
                continue

            if not data:
                last_error = RuntimeError(
                    "Jin10 API returned empty data"
                )
                continue

            print(
                f"Jin10 API OK: {len(data)} items"
            )

            return data

        except Exception as exc:

            last_error = exc

            print(
                f"API host failed: "
                f"{host}: {exc}",
                file=sys.stderr,
            )

    raise RuntimeError(
        "All Jin10 API hosts failed: "
        f"{last_error}"
    )


def extract_title(
    inner: dict,
    content: str,
) -> str:

    title = (
        inner.get("title")
        or ""
    ).strip()

    if title:
        return title

    # 普通快讯通常：
    # 【这里是标题】正文……
    match = re.match(
        r"【([^】]+)】",
        content,
    )

    if match:
        return (
            match.group(1)
            .strip()
        )

    clean = re.sub(
        r"\s+",
        " ",
        content,
    ).strip()

    if len(clean) > 60:
        return clean[:60] + "…"

    return (
        clean
        or "金十重要事件"
    )


def choose_link(
    item_id: str,
    inner: dict,
) -> str:

    # 金十自己的文章链接优先
    link = (
        inner.get("link")
        or ""
    ).strip()

    if link:
        return link

    # 然后使用原始来源
    source_link = (
        inner.get("source_link")
        or ""
    ).strip()

    if source_link:
        return source_link

    # 最后回退到金十快讯详情
    return (
        "https://flash.jin10.com/"
        f"detail/{item_id}"
    )


def make_description(
    inner: dict,
    outer: dict,
) -> str:

    parts = []

    pic = (
        inner.get("pic")
        or ""
    ).strip()

    if pic:
        parts.append(
            '<p>'
            f'<img src="{html.escape(pic)}" '
            'style="max-width:100%;'
            'height:auto;" />'
            '</p>'
        )

    content = (
        inner.get("content")
        or ""
    ).strip()

    if content:
        parts.append(
            "<p>"
            + html.escape(content)
            + "</p>"
        )

    source = (
        inner.get("source")
        or ""
    ).strip()

    if source:
        parts.append(
            "<p>"
            "来源："
            + html.escape(source)
            + "</p>"
        )

    tag = (
        inner.get("tag")
        or ""
    ).strip()

    if tag:
        parts.append(
            "<p>"
            "分类："
            + html.escape(tag)
            + "</p>"
        )

    remarks = (
        outer.get("remark")
        or []
    )

    if isinstance(
        remarks,
        list,
    ):

        remark_parts = []

        for remark in remarks:

            if not isinstance(
                remark,
                dict,
            ):
                continue

            title = (
                remark.get("title")
                or ""
            ).strip()

            content = (
                remark.get("content")
                or ""
            ).strip()

            link = (
                remark.get("link")
                or remark.get("url")
                or ""
            ).strip()

            if not (
                title
                or content
            ):
                continue

            text = ""

            if link and title:
                text = (
                    f'<a href="'
                    f'{html.escape(link)}">'
                    f'{html.escape(title)}'
                    "</a>"
                )

            elif title:
                text = html.escape(
                    title
                )

            if content:
                if text:
                    text += "："

                text += html.escape(
                    content
                )

            if text:
                remark_parts.append(
                    f"<li>{text}</li>"
                )

        if remark_parts:
            parts.append(
                "<p><strong>"
                "相关信息"
                "</strong></p>"
                "<ul>"
                + "".join(
                    remark_parts
                )
                + "</ul>"
            )

    return "".join(parts)


def parse_pubdate(
    value: str,
) -> str:

    try:
        dt = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        )

        cn_tz = timezone(
            timedelta(hours=8)
        )

        dt = dt.replace(
            tzinfo=cn_tz
        )

        return format_datetime(
            dt
        )

    except Exception:
        return ""


def generate_rss(
    items,
):

    rss_items = []

    for wrapper in items:

        if not isinstance(
            wrapper,
            dict,
        ):
            continue

        item_id = str(
            wrapper.get("item_id")
            or ""
        ).strip()

        outer = (
            wrapper.get("data")
            or {}
        )

        if not isinstance(
            outer,
            dict,
        ):
            continue

        inner = (
            outer.get("data")
            or {}
        )

        if not isinstance(
            inner,
            dict,
        ):
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
            outer.get("time")
            or ""
        )

        description = (
            make_description(
                inner,
                outer,
            )
        )

        guid = (
            item_id
            or link
        )

        rss_items.append(
            f"""
        <item>
            <title>{html.escape(title)}</title>
            <link>{html.escape(link)}</link>
            <guid isPermaLink="false">{html.escape(guid)}</guid>
            <pubDate>{html.escape(pubdate)}</pubDate>
            <description><![CDATA[{description}]]></description>
        </item>
"""
        )

    if not rss_items:
        raise RuntimeError(
            "No valid RSS items generated"
        )

    now = format_datetime(
        datetime.now(
            timezone.utc
        )
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
            "Jin10 returned no items; "
            "refusing to overwrite RSS"
        )

    rss = generate_rss(
        items
    )

    os.makedirs(
        "feeds",
        exist_ok=True,
    )

    tmp_file = (
        OUTPUT_FILE
        + ".tmp"
    )

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
        f"Generated "
        f"{OUTPUT_FILE}: "
        f"{len(items)} items"
    )


if __name__ == "__main__":
    main()
