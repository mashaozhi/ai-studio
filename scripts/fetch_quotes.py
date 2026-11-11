#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小源的数据管道：拉取 A股 / 美股 / 港股日线行情，写入 Supabase。

数据来源（全部免费、无需 key、无需注册）：
  - 美股 / 港股：yfinance（Yahoo Finance 非官方库，1.8.x）
  - A股：腾讯行情日 K（web.ifzq.gtimg.cn/appstock/app/fqkline/get，前复权）
  - 备用（三个市场都可用）：东方财富 push2his
    （push2his.eastmoney.com/api/qt/stock/kline/get）

写入：Supabase PostgREST（/rest/v1/market_daily_quotes），
按 symbol + trade_date 做 upsert（Prefer: resolution=merge-duplicates），
重复跑不会产生重复行。

密钥：只从环境变量 SUPABASE_URL / SUPABASE_ANON_KEY 读取，
绝不写进代码、日志和文档。

用法：
  python scripts/fetch_quotes.py [--market CN|US|HK|CN,HK|ALL] [--symbols-json out/brief_YYYYMMDD_morning.json] [--dry-run]
  DRY_RUN=1 python scripts/fetch_quotes.py   # 只打印、不入库（本地调试用）

股票列表来源（不再读固定的 watchlist.yaml，2026-11-11 用户改方向）：
  - Supabase watchlist 表（自选股，见 sql/002_watchlist.sql）
  - --symbols-json 指定的简报 JSON 里的 candidates（当期候选标的）
  两者合并、去重后拉取；自选股表为空也照常跑（只拉候选标的）。

退出码：0 = 至少拿到一行并写入/打印；1 = 一行都没拿到，或入库失败。
单个股票失败只记日志、不中断整批（停牌、节假日都属正常）。
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 顶层只引标准库：yfinance / requests / yaml 都在函数里懒加载，
# 这样 --help 和语法检查不需要装任何第三方包。
logger = logging.getLogger("xiaoyuan")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东方财富公开的固定 ut 参数（akshare 等开源项目同款，非密钥）
EASTMONEY_UT = "7eea3edcaed734bea9cbfc24409ed989"

def load_watchlist_supabase(base_url, api_key):
    """读 Supabase watchlist 表（自选股），返回 {市场: [{symbol, name}]}。

    表为空也照常返回空字典（只拉候选标的），不报错。
    与 pick_candidates.py 同理：先按 symbol 排序查（symbol 是主键列，
    一定在），失败就去掉排序重试一次，避免表刚建完、PostgREST schema
    缓存未刷新时一个 400 就丢掉整个自选表。
    """
    import requests

    url = base_url.rstrip("/") + "/rest/v1/watchlist"
    try:
        resp = requests.get(
            url,
            params={"select": "symbol,market,name", "order": "symbol.asc"},
            headers={"apikey": api_key, "Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - 排序查询失败就去掉排序重试
        logger.warning("读 watchlist（带排序）失败，去掉排序重试一次: %s", exc)
        time.sleep(2)
        resp = requests.get(
            url,
            params={"select": "symbol,market,name"},
            headers={"apikey": api_key, "Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
    markets = {}
    for row in resp.json() or []:
        market = (row.get("market") or "").strip().upper()
        symbol = str(row.get("symbol") or "").strip()
        if market not in ("CN", "US", "HK") or not symbol:
            continue
        if market == "CN":
            symbol = symbol.zfill(6)
        elif market == "HK":
            symbol = symbol.zfill(4)
        markets.setdefault(market, []).append(
            {"symbol": symbol, "name": row.get("name") or ""}
        )
    return markets


def load_candidates_json(path):
    """读简报 JSON 里的 candidates，返回 {市场: [{symbol, name}]}。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    markets = {}
    for cand in data.get("candidates") or []:
        market = (cand.get("market") or "").strip().upper()
        symbol = str(cand.get("symbol") or "").strip()
        if market not in ("CN", "US", "HK") or not symbol:
            continue
        if market == "CN":
            symbol = symbol.zfill(6)
        elif market == "HK":
            symbol = symbol.zfill(4)
        markets.setdefault(market, []).append(
            {"symbol": symbol, "name": cand.get("name") or ""}
        )
    return markets


def merge_lists(*lists):
    """合并多个 {市场: [...]}，同市场同代码去重。"""
    merged = {}
    for lst in lists:
        for market, items in lst.items():
            seen = {e["symbol"] for e in merged.get(market, [])}
            for item in items:
                if item["symbol"] not in seen:
                    merged.setdefault(market, []).append(item)
                    seen.add(item["symbol"])
    return merged


def make_row(symbol, market, trade_date, open_, high, low, close, volume, source):
    return {
        "symbol": symbol,
        "market": market,
        "trade_date": trade_date,
        "open": round(float(open_), 4),
        "high": round(float(high), 4),
        "low": round(float(low), 4),
        "close": round(float(close), 4),
        "volume": int(float(volume)) if volume is not None else 0,
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def rows_from_ohlcv(df, symbol, market, source):
    """从 yfinance 返回的 OHLCV DataFrame 提取行，跳过无收盘价的行。"""
    import pandas as pd

    rows = []
    for ts, r in df.iterrows():
        try:
            close = r["Close"]
            if pd.isna(close):
                continue
            volume = r["Volume"] if "Volume" in r and not pd.isna(r["Volume"]) else 0
            rows.append(
                make_row(
                    symbol,
                    market,
                    pd.Timestamp(ts).strftime("%Y-%m-%d"),
                    r["Open"],
                    r["High"],
                    r["Low"],
                    close,
                    volume,
                    source,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单根 K 线坏了不影响其他
            logger.warning("%s %s 跳过: %s", symbol, ts, exc)
    return rows


def fetch_us_hk_yahoo(items, market):
    """美股/港股：yfinance 批量拉取；整批失败时逐只重试。

    返回 {symbol: [row, ...]}，失败的 symbol 不在结果里。
    """
    import pandas as pd
    import yfinance as yf

    tickers = []
    ticker_of = {}
    for item in items:
        yahoo_symbol = f"{item['symbol']}.HK" if market == "HK" else item["symbol"]
        tickers.append(yahoo_symbol)
        ticker_of[yahoo_symbol] = item["symbol"]

    out = {}
    try:
        data = yf.download(
            tickers,
            period="5d",
            interval="1d",
            auto_adjust=False,  # 只要未复权价，和 A 股口径一致
            progress=False,
            group_by="ticker",
            threads=False,  # 串行请求，别触发雅虎限流
        )
    except Exception as exc:  # noqa: BLE001 - 整批失败就走逐只兜底
        logger.warning("yfinance 批量拉取失败，改逐只拉取: %s", exc)
        data = None

    if data is not None and len(data) > 0:
        multi = isinstance(data.columns, pd.MultiIndex)
        for yahoo_symbol in tickers:
            symbol = ticker_of[yahoo_symbol]
            try:
                df = data[yahoo_symbol] if multi else data
                rows = rows_from_ohlcv(df, symbol, market, "yfinance")
                if rows:
                    out[symbol] = rows
                else:
                    logger.warning("%s yfinance 返回空（可能停牌/节假日）", symbol)
            except Exception as exc:  # noqa: BLE001 - 单只失败不中断
                logger.warning("%s yfinance 解析失败: %s", symbol, exc)
        return out

    # 兜底：逐只 Ticker.history，中间留 0.5 秒间隔
    for yahoo_symbol in tickers:
        symbol = ticker_of[yahoo_symbol]
        try:
            hist = yf.Ticker(yahoo_symbol).history(
                period="5d", interval="1d", auto_adjust=False
            )
            rows = rows_from_ohlcv(hist, symbol, market, "yfinance")
            if rows:
                out[symbol] = rows
            else:
                logger.warning("%s yfinance 返回空（可能停牌/节假日）", symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s yfinance 拉取失败: %s", symbol, exc)
        time.sleep(0.5)
    return out


def fetch_tencent(code, bars=10):
    """A股主源：腾讯日 K（前复权）。

    接口：web.ifzq.gtimg.cn/appstock/app/fqkline/get
    返回行格式：[日期, 开, 收, 高, 低, 成交量]（注意：收在高之前）。
    """
    import requests

    full = cn_prefix(code) + code
    resp = requests.get(
        TENCENT_KLINE,
        params={"param": f"{full},day,,,{bars},qfq", "_var": "kline_dayqfq"},
        headers={"User-Agent": UA},
        timeout=15,
    )
    resp.raise_for_status()
    text = resp.text.strip()
    if "kline_dayqfq=" in text:
        text = text.split("kline_dayqfq=", 1)[1]
    payload = json.loads(text.rstrip(";").strip())
    node = (payload.get("data") or {}).get(full) or {}
    klines = node.get("qfqday") or node.get("day") or []
    rows = []
    for k in klines:
        if len(k) < 6:
            continue
        rows.append(
            make_row(code, "CN", str(k[0])[:10], k[1], k[3], k[4], k[2], k[5], "tencent")
        )
    return rows


def eastmoney_secid(code, market):
    """东方财富 secid：沪 1. / 深 0. / 港 116. / 美 105.。"""
    if market == "CN":
        return ("1." if code.startswith(("6", "9")) else "0.") + code
    if market == "HK":
        return "116." + code.zfill(5)
    return "105." + code


def fetch_eastmoney(code, market, bars=10):
    """备用源：东方财富 push2his 日 K（前复权），三个市场通用。

    klines 每行：日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率。
    """
    import requests

    resp = requests.get(
        EASTMONEY_KLINE,
        params={
            "secid": eastmoney_secid(code, market),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": "0",
            "end": "20500101",
            "lmt": str(max(bars, 10)),
            "ut": EASTMONEY_UT,
        },
        headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
        timeout=15,
    )
    resp.raise_for_status()
    klines = (resp.json().get("data") or {}).get("klines") or []
    rows = []
    for line in klines[-bars:]:
        p = line.split(",")
        if len(p) < 6:
            continue
        rows.append(
            make_row(code, market, p[0], p[1], p[3], p[4], p[2], p[5], "eastmoney")
        )
    return rows


def upsert_rows(base_url, api_key, rows, chunk=100):
    """批量 upsert 到 Supabase PostgREST。

    关键：URL 必须带 ?on_conflict=symbol,trade_date，
    否则撞主键会整批报 23505；Prefer 用 resolution=merge-duplicates。
    """
    import requests

    url = (
        base_url.rstrip("/")
        + "/rest/v1/market_daily_quotes?on_conflict=symbol,trade_date"
    )
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    total = 0
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        resp = requests.post(url, headers=headers, json=part, timeout=30)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Supabase upsert 失败 HTTP {resp.status_code}: {resp.text[:500]}"
            )
        total += len(part)
    return total


def main():
    parser = argparse.ArgumentParser(description="小源的数据管道：拉取三地日线行情并写入 Supabase")
    parser.add_argument(
        "--market",
        default="ALL",
        help="CN/US/HK/ALL，或逗号组合如 CN,HK（早盘拉 A股+港股）",
    )
    parser.add_argument("--symbols-json", default="", help="简报 JSON 路径，读其中 candidates 里的代码")
    parser.add_argument("--dry-run", action="store_true", help="只打印、不入库")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not dry_run and (not supabase_url or not supabase_key):
        print(
            "缺少环境变量 SUPABASE_URL 或 SUPABASE_ANON_KEY，"
            "本地调试请加 --dry-run，线上由 GitHub Secrets 注入。",
            file=sys.stderr,
        )
        return 2

    want = (
        ["CN", "US", "HK"]
        if args.market == "ALL"
        else [m.strip().upper() for m in args.market.split(",") if m.strip()]
    )
    for m in want:
        if m not in ("CN", "US", "HK"):
            print(f"--market 参数无效：{m}（只能是 CN/US/HK/ALL 或逗号组合）", file=sys.stderr)
            return 2

    # 股票列表来源：Supabase watchlist 表（自选股）+ 简报 JSON 的 candidates；
    # 固定清单 data/watchlist.yaml 已废弃（2026-11-11 用户改方向：不跑固定股票）。
    watchlist = {}
    if not dry_run:
        try:
            watchlist = load_watchlist_supabase(supabase_url, supabase_key)
        except Exception as exc:  # noqa: BLE001 - 自选股读不到就只拉候选
            logger.warning("读 Supabase watchlist 失败，本次只拉候选标的: %s", exc)
    if args.symbols_json:
        try:
            watchlist = merge_lists(watchlist, load_candidates_json(Path(args.symbols_json)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读候选 JSON %s 失败，本次只拉自选股: %s", args.symbols_json, exc)
    watchlist = {m: items for m, items in watchlist.items() if m in want}
    total_symbols = sum(len(v) for v in watchlist.values())
    if total_symbols == 0:
        logger.info("本次无股票可拉：自选股表为空且无候选标的，跳过拉数（简报已出，不算失败）")
        return 0
    logger.info("本次拉取 %d 只：%s", total_symbols, {m: len(v) for m, v in watchlist.items()})

    all_rows = []
    failures = []

    # --- 美股 / 港股：yfinance 批量拉，失败的逐只走东方财富兜底 ---
    for market in ("US", "HK"):
        if market not in want:
            continue
        items = watchlist.get(market, [])
        got = fetch_us_hk_yahoo(items, market)
        for item in items:
            symbol = item["symbol"]
            if symbol in got:
                all_rows.extend(got[symbol])
                logger.info("%s %s yfinance 拿到 %d 行", market, symbol, len(got[symbol]))
            else:
                try:
                    rows = fetch_eastmoney(symbol, market)
                    if rows:
                        all_rows.extend(rows)
                        logger.info("%s %s 东方财富兜底拿到 %d 行", market, symbol, len(rows))
                    else:
                        failures.append(f"{market}:{symbol}（yfinance、东方财富都无数据）")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{market}:{symbol}（{exc}）")
            time.sleep(0.3)

    # --- A股：腾讯主源，失败走东方财富兜底 ---
    if "CN" in want:
        for item in watchlist.get("CN", []):
            symbol = item["symbol"]
            rows = []
            try:
                rows = fetch_tencent(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("CN %s 腾讯失败，试东方财富: %s", symbol, exc)
            if not rows:
                try:
                    rows = fetch_eastmoney(symbol, "CN")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CN %s 东方财富也失败: %s", symbol, exc)
            if rows:
                all_rows.extend(rows)
                logger.info("CN %s 拿到 %d 行（来源 %s）", symbol, len(rows), rows[0]["source"])
            else:
                failures.append(f"CN:{symbol}（腾讯、东方财富都无数据）")
            time.sleep(0.3)

    # 去重：同一 (symbol, trade_date) 只留最后一条
    deduped = {}
    for row in all_rows:
        deduped[(row["symbol"], row["trade_date"])] = row
    rows = sorted(deduped.values(), key=lambda r: (r["market"], r["symbol"], r["trade_date"]))

    logger.info("共拿到 %d 行（去重后），失败 %d 只：%s", len(rows), len(failures), failures or "无")

    if not rows:
        logger.error("一行数据都没拿到，本次不入库")
        return 1

    if dry_run:
        for row in rows[:10]:
            print(row)
        if len(rows) > 10:
            print(f"... 还有 {len(rows) - 10} 行未打印")
        logger.info("[DRY RUN] 未写入 Supabase")
        return 0

    written = upsert_rows(supabase_url, supabase_key, rows)
    logger.info("已写入 Supabase %d 行（表 market_daily_quotes）", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
