#!/usr/bin/env python3

import html
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup


PAGE_URLS = [
    "https://xnews.jin10.com/53",
    "https://xnews.jin10.com/31",
]

OUTPUT_FILE = "feeds/jin10_hot.xml"


def clean_text(value):
    return re.sub(
        r"\s+",
        " ",
        value or "",
    ).strip()


def create_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    profile_dir = tempfile.mkdtemp(
        prefix="jin10-hot-"
    )

    options = Options()

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
        "--lang=zh-CN"
    )
    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )
    options.add_argument(
        f"--user-data-dir={profile_dir}"
    )

    driver = webdriver.Chrome(
        options=options
    )

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(
                    navigator,
                    'webdriver',
                    {
                        get: () => undefined
                    }
                );
                """
            },
        )
    except Exception:
        pass

    return driver, profile_dir


def fetch_rendered_html(
    driver,
    page_url,
):
    print(
        f"Opening Jin10 page: {page_url}"
    )

    driver.get(
        page_url
    )

    time.sleep(8)

    # 向下滚动，让懒加载内容显示出来
    for _ in range(4):
        driver.execute_script(
            "window.scrollTo("
            "0, document.body.scrollHeight"
            ");"
        )
        time.sleep(2)

    page_source = (
        driver.page_source
    )

    print(
        f"Rendered HTML length "
        f"for {page_url}: "
        f"{len(page_source)}"
    )

    return page_source


def extract_articles(
    page_html,
    page_url,
    category_name,
):
    soup = BeautifulSoup(
        page_html,
        "html.parser",
    )

    articles = []
    seen = set()

    for a in soup.find_all(
        "a",
        href=True,
    ):
        href = (
            a.get("href")
            or ""
        ).strip()

        if not href:
            continue

        absolute = urljoin(
            page_url,
            href,
        )

        if (
            "xnews.jin10.com/details/"
            not in absolute
        ):
            continue

        match = re.search(
            r"/details/(\d+)",
            absolute,
        )

        if not match:
            continue

        article_id = match.group(1)

        if article_id in seen:
            continue

        container = a
        best_text = ""

        # 向上寻找文章卡片
        for _ in range(6):
            if (
                container is None
                or container.parent is None
            ):
                break

            text = clean_text(
                container.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) > len(best_text):
                best_text = text

            container = container.parent

        link_text = clean_text(
            a.get_text(
                " ",
                strip=True,
            )
        )

        title = ""

        # 优先寻找标题节点
        for selector in [
            "h1",
            "h2",
            "h3",
            "h4",
            ".title",
            "[class*=title]",
        ]:
            node = a.select_one(
                selector
            )

            if node:
                candidate = clean_text(
                    node.get_text(
                        " ",
                        strip=True,
                    )
                )

                if (
                    candidate
                    and len(candidate) >= 6
                ):
                    title = candidate
                    break

        if not title:
            title = link_text

        # 清理常见 UI 标签
        title = re.sub(
            r"^(NEW|HOT|精选)\s*",
            "",
            title,
            flags=re.I,
        ).strip()

        title = re.sub(
            r"\s+(NEW|HOT|精选).*$",
            "",
            title,
            flags=re.I,
        ).strip()

        if len(title) > 120:
            title = re.split(
                r"\s+(?:HOT|NEW|精选|"
                r"\d+分钟前|\d+小时前|"
                r"\d+天前|\d{2}-\d{2})",
                title,
                maxsplit=1,
                flags=re.I,
            )[0].strip()

        if (
            not title
            or len(title) < 4
        ):
            continue

        summary = best_text

        if summary:
            summary = summary.replace(
                title,
                "",
                1,
            ).strip()

            summary = re.sub(
                r"\b(?:NEW|HOT|精选)\b",
                "",
                summary,
                flags=re.I,
            )

            summary = re.sub(
                r"\b\d+分钟前\b",
                "",
                summary,
            )

            summary = re.sub(
                r"\b\d+小时前\b",
                "",
                summary,
            )

            summary = re.sub(
                r"\b\d+天前\b",
                "",
                summary,
            )

            summary = re.sub(
                r"\b\d{2}-\d{2}\s+\d{2}:\d{2}\b",
                "",
                summary,
            )

            summary = clean_text(
                summary
            )

            if len(summary) > 500:
                summary = (
                    summary[:500]
                    + "…"
                )

        seen.add(
            article_id
        )

        articles.append(
            {
                "id": article_id,
                "title": title,
                "link": absolute,
                "summary": summary,
                "category": category_name,
            }
        )

    return articles


def merge_articles(
    article_groups,
):
    merged = {}
    order = []

    for articles in article_groups:
        for article in articles:
            article_id = article["id"]

            if article_id not in merged:
                merged[article_id] = article
                order.append(article_id)
                continue

            # 同一篇文章同时出现在两个栏目时，
            # 保留原文章，只合并栏目名称
            old = merged[article_id]

            old_categories = set(
                old["category"].split(" / ")
            )

            new_categories = set(
                article["category"].split(" / ")
            )

            categories = sorted(
                old_categories
                | new_categories
            )

            old["category"] = (
                " / ".join(categories)
            )

            # 如果之前没有摘要，
            # 用新的摘要补上
            if (
                not old.get("summary")
                and article.get("summary")
            ):
                old["summary"] = (
                    article["summary"]
                )

    return [
        merged[article_id]
        for article_id in order
    ]


def generate_rss(
    articles,
):
    rss_items = []

    for article in articles:
        title = article["title"]
        link = article["link"]
        guid = article["id"]
        summary = article["summary"]
        category = article["category"]

        parts = []

        if category:
            parts.append(
                "<p><strong>栏目：</strong>"
                + html.escape(category)
                + "</p>"
            )

        if summary:
            parts.append(
                "<p>"
                + html.escape(summary)
                + "</p>"
            )

        parts.append(
            '<p><a href="'
            + html.escape(link)
            + '">阅读金十原文</a></p>'
        )

        description = "".join(
            parts
        )

        rss_items.append(
            f"""
        <item>
            <title>{html.escape(title)}</title>
            <link>{html.escape(link)}</link>
            <guid isPermaLink="false">{html.escape(guid)}</guid>
            <category>{html.escape(category)}</category>
            <description><![CDATA[{description}]]></description>
        </item>
"""
        )

    if not rss_items:
        raise RuntimeError(
            "No valid Jin10 articles generated"
        )

    now = format_datetime(
        datetime.now(
            timezone.utc
        )
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
    <channel>
        <title>金十数据 - 热点头条合集</title>
        <link>https://xnews.jin10.com/</link>
        <description>金十数据热点头条与相关栏目合并 RSS</description>
        <language>zh-cn</language>
        <generator>ForgeRSS Jin10 Hot Combined</generator>
        <lastBuildDate>{html.escape(now)}</lastBuildDate>
        {''.join(rss_items)}
    </channel>
</rss>
"""


def main():
    driver = None
    profile_dir = None

    try:
        driver, profile_dir = (
            create_driver()
        )

        article_groups = []

        page_configs = [
            (
                "https://xnews.jin10.com/53",
                "热点头条",
            ),
            (
                "https://xnews.jin10.com/31",
                "栏目31",
            ),
        ]

        for (
            page_url,
            category_name,
        ) in page_configs:

            page_html = (
                fetch_rendered_html(
                    driver,
                    page_url,
                )
            )

            articles = (
                extract_articles(
                    page_html,
                    page_url,
                    category_name,
                )
            )

            print(
                f"{category_name}: "
                f"{len(articles)} articles"
            )

            article_groups.append(
                articles
            )

        articles = merge_articles(
            article_groups
        )

        print(
            "Merged unique articles:",
            len(articles),
        )

        if not articles:
            raise RuntimeError(
                "No articles found; "
                "refusing to overwrite RSS"
            )

        for article in articles[:10]:
            print(
                "-",
                article["id"],
                f"[{article['category']}]",
                article["title"],
            )

        rss = generate_rss(
            articles
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
            f"{len(articles)} unique items"
        )

    finally:
        if driver is not None:
            driver.quit()

        if profile_dir:
            shutil.rmtree(
                profile_dir,
                ignore_errors=True,
            )


if __name__ == "__main__":
    main()
