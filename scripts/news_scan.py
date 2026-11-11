#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小源的新闻扫描：拉最新全球财经与政治新闻 + 主要指数走势，输出 JSON 摘要。

新闻源（2026-11-11 核实，全部免费、无需 key；单个源失败自动跳过，不中断）：
  - Google News RSS 搜索（主源，按主题查）：news.google.com/rss/search
  - BBC 商业 / 国际 RSS：feeds.bbci.co.uk/news/business/rss.xml、
    feeds.bbci.co.uk/news/world/rss.xml（2026 年 10 月第三方实测仍有新鲜内容）
  - Yahoo News 头条：news.yahoo.com/rss
  - Yahoo Finance 通用财经 feed 已下线（finance.yahoo.com/news/rssindex
    返回 404），不用；单股 headline feed 仍可用，见 README。
  - 路透没有公开稳定的免费 RSS，不用。

指数（yfinance，免费）：标普 500 ^GSPC、纳斯达克 ^IXIC、恒生 ^HSI、上证 000001.SS。

用法：
  python scripts/news_scan.py [--session morning|evening] [--hours 24] [--out out/news_scan.json]

退出码：0 = 至少拿到新闻或指数；1 = 新闻和指数全失败。
"""

import argparse
import html
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("xiaoyuan")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# (名称, RSS 地址)；单个源挂了只记日志、跳过，不中断整批。
FEEDS = [
    ("GoogleNews-全球股市", "https://news.google.com/rss/search?q=stock%20market&hl=en-US&gl=US&ceid=US:en"),
    ("GoogleNews-美联储利率", "https://news.google.com/rss/search?q=Federal%20Reserve%20interest%20rates&hl=en-US&gl=US&ceid=US:en"),
    ("GoogleNews-关税贸易", "https://news.google.com/rss/search?q=tariffs%20trade%20war&hl=en-US&gl=US&ceid=US:en"),
    ("GoogleNews-中国股市", "https://news.google.com/rss/search?q=%E4%B8%AD%E5%9B%BD%E8%82%A1%E5%B8%82%20A%E8%82%A1&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    ("GoogleNews-地缘政治", "https://news.google.com/rss/search?q=geopolitics%20oil%20Middle%20East&hl=en-US&gl=US&ceid=US:en"),
    ("BBC-商业", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC-国际", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("YahooNews-头条", "https://news.yahoo.com/rss"),
]

INDICES = [
    ("SP500", "^GSPC", "标普 500"),
    ("NASDAQ", "^IXIC", "纳斯达克"),
    ("HSI", "^HSI", "恒生指数"),
    ("SSE", "000001.SS", "上证指数"),
]

MAX_NEWS_ITEMS = 60      # 喂给模型前最多保留的条数
MAX_SUMMARY_LEN = 300    # 每条摘要最多保留的字数（控 token）


def clean_text(text):
    """去 HTML 标签、解实体、压空白、截断。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_SUMMARY_LEN]


def fetch_feed(name, url, since_ts):
    """抓一个 RSS 源，返回 (items, ok)。items 只要窗口内的新闻。"""
    import feedparser

    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": UA})
    except Exception as exc:  # noqa: BLE001 - 单个源失败不中断
        logger.warning("%s 抓取失败: %s", name, exc)
        return [], False
    items = []
    for entry in parsed.entries or []:
        title = clean_text(getattr(entry, "title", ""))
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue
        published = None
        pp = getattr(entry, "published_parsed", None)
        if pp:
            try:
                published = int(time.mktime(pp))
            except Exception:  # noqa: BLE001
                published = None
        if published is None:
            published = int(time.time())  # 没给时间就当刚发的，别漏掉
        if published < since_ts:
            continue
        summary = clean_text(
            getattr(entry, "summary", "") or getattr(entry, "description", "")
        )
        items.append(
            {
                "title": title,
                "summary": summary,
                "link": link,
                "source": name,
                "published": datetime.fromtimestamp(
                    published, tz=timezone.utc
                ).isoformat(),
                "_ts": published,
            }
        )
    logger.info("%s 拿到 %d 条", name, len(items))
    return items, True


def fetch_indices():
    """拉四个主要指数最近 5 个交易日，算最新收盘、涨跌幅、5 日涨跌幅。"""
    import yfinance as yf

    out = {}
    for key, symbol, name in INDICES:
        try:
            hist = yf.Ticker(symbol).history(
                period="5d", interval="1d", auto_adjust=False
            )
            closes = [float(c) for c in hist["Close"].dropna()] if len(hist) else []
            if len(closes) < 2:
                logger.warning("指数 %s(%s) 数据不足", name, symbol)
                continue
            last, prev = closes[-1], closes[-2]
            out[key] = {
                "name": name,
                "symbol": symbol,
                "close": round(last, 2),
                "prev_close": round(prev, 2),
                "change_pct": round((last - prev) / prev * 100, 2),
                "change_5d_pct": round((last - closes[0]) / closes[0] * 100, 2),
            }
            logger.info(
                "指数 %s 最新 %.2f，涨跌 %s%%，5 日 %s%%",
                name,
                last,
                out[key]["change_pct"],
                out[key]["change_5d_pct"],
            )
        except Exception as exc:  # noqa: BLE001 - 单个指数失败不中断
            logger.warning("指数 %s(%s) 拉取失败: %s", name, symbol, exc)
        time.sleep(0.5)
    return out


def main():
    parser = argparse.ArgumentParser(description="小源的新闻扫描：最新全球新闻 + 主要指数走势")
    parser.add_argument("--session", default="morning", choices=["morning", "evening"])
    parser.add_argument("--hours", type=int, default=24, help="新闻时间窗口（小时）")
    parser.add_argument("--out", default="out/news_scan.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    now_ts = int(time.time())
    since_ts = now_ts - args.hours * 3600

    all_news = []
    feeds_ok, feeds_failed = [], []
    for name, url in FEEDS:
        items, ok = fetch_feed(name, url, since_ts)
        (feeds_ok if ok else feeds_failed).append(name)
        all_news.extend(items)
        time.sleep(0.5)

    # 去重（按链接，没有链接按标题），按时间倒序，最多保留 MAX_NEWS_ITEMS 条
    seen, deduped = set(), []
    for item in sorted(all_news, key=lambda x: x["_ts"], reverse=True):
        key = item["link"] or item["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        item.pop("_ts", None)
        deduped.append(item)
        if len(deduped) >= MAX_NEWS_ITEMS:
            break

    indices = fetch_indices()

    logger.info(
        "新闻 %d 条（%d 个源成功、%d 个源失败：%s），指数 %d 个",
        len(deduped),
        len(feeds_ok),
        len(feeds_failed),
        feeds_failed or "无",
        len(indices),
    )
    if not deduped and not indices:
        logger.error("新闻和指数全失败，本次不产出摘要")
        return 1

    result = {
        "generated_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "session": args.session,
        "window_hours": args.hours,
        "indices": indices,
        "news": deduped,
        "feeds_ok": feeds_ok,
        "feeds_failed": feeds_failed,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("新闻摘要已写 %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
