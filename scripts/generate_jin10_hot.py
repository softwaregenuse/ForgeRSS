#!/usr/bin/env python3

import html
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


PAGE_URL = "https://xnews.jin10.com/53"
OUTPUT_FILE = "feeds/jin10_hot.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def fetch_page():
    response = requests.get(
        PAGE_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.text


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def extract_articles(page_html):
    soup = BeautifulSoup(page_html, "html.parser")

    articles = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()

        if not href:
            continue

        absolute = urljoin(PAGE_URL, href)

        # 金十文章详情通常在 xnews.jin10.com/details/
        if "/details/" not in absolute:
            continue

        if absolute in seen:
            continue

        seen.add(absolute)

        text = clean_text(
            a.get_text(" ", strip=True)
        )

        if not text:
            continue

        # 尝试从链接内部 / 周边结构提取标题和摘要
        title = ""
        summary = ""

        # 常见情况下链接文本里标题在前、摘要在后
        # 先尝试找标题节点
        for selector in [
            "h1",
            "h2",
            "h3",
            ".title",
            ".news-title",
            ".article-title",
        ]:
            node = a.select_one(selector)
            if node:
                title = clean_text(
                    node.get_text(" ", strip=True)
                )
                if title:
                    break

        if not title:
            # 退化方案：
            # 去掉 HOT / 精选 / 时间等常见尾巴
            title = re.sub(
                r"\s+(HOT\s*)?(精选\s*)?"
                r"(\d+小时前|\d+天前|\d{2}-\d{2}\s+\d{2}:\d{2})"
                r".*$",
                "",
                text,
            ).strip()

        if not title:
            title = text[:80]

        # 尝试取卡片容器里的摘要
        container = a

        for _ in range(4):
            if container.parent is None:
                break

            container = container.parent

            block_text = clean_text(
                container.get_text(" ", strip=True)
            )

            if (
                len(block_text) > len(title) + 20
                and title in block_text
            ):
                summary = block_text
                break

        if summary:
            summary = summary.replace(
                title,
                "",
                1,
            ).strip()

            summary = re.sub(
                r"\bHOT\b",
                "",
                summary,
                flags=re.I,
            )

            summary = re.sub(
                r"\b精选\b",
                "",
                summary,
            )

            summary = clean_text(summary)

        articles.append(
            {
                "title": title,
                "link": absolute,
                "summary": summary,
            }
        )

    return articles


def generate_rss(articles):
    items = []

    for article in articles:
        title = article["title"]
        link = article["link"]
        summary = article["summary"]

        guid = link

        description = ""

        if summary:
            description = (
                "<p>"
                + html.escape(summary)
                + "</p>"
            )

        description += (
            '<p><a href="'
            + html.escape(link)
            + '">阅读金十原文</a></p>'
        )

        items.append(
            f"""
        <item>
            <title>{html.escape(title)}</title>
            <link>{html.escape(link)}</link>
            <guid isPermaLink="true">{html.escape(guid)}</guid>
            <description><![CDATA[{description}]]></description>
        </item>
"""
        )

    if not items:
        raise RuntimeError(
            "No Jin10 hot-news articles found"
        )

    now = format_datetime(
        datetime.now(timezone.utc)
    )

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
    <channel>
        <title>金十数据 - 热点头条</title>
        <link>{html.escape(PAGE_URL)}</link>
        <description>金十数据热点头条 RSS</description>
        <language>zh-cn</language>
        <generator>ForgeRSS Jin10 Hot</generator>
        <lastBuildDate>{html.escape(now)}</lastBuildDate>
        {''.join(items)}
    </channel>
</rss>
"""

    return rss


def main():
    page_html = fetch_page()

    articles = extract_articles(
        page_html
    )

    print(
        "Found articles:",
        len(articles),
    )

    if not articles:
        raise RuntimeError(
            "No articles found; refusing to overwrite RSS"
        )

    rss = generate_rss(
        articles
    )

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
        f"{len(articles)} items"
    )


if __name__ == "__main__":
    main()
