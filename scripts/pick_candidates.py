#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小源的选股：把新闻摘要 + 指数 + 自选股最新行情喂给 OpenRouter 付费模型，
输出当期机会标的 + 自选股当日评估，落成 JSON 简报和 Markdown 简报，
并把简报写进 Supabase briefs 表（写库失败只记日志，不影响文件产出）。

模型：openai/gpt-5-mini（付费模型；账号已充值 10 美元信用额，不再遍历免费模型）。
免费模型已不可靠，不再使用：meta-llama/llama-3.3-70b-instruct:free（免费版已下架，404）、
google/gemma-4-26b-a4b-it:free（上游限流 429）、openrouter/free（自动路由曾返回无法解析的 JSON）。
本管道每天只调 2 次（早晚各 1 次）。

用法：
  python scripts/pick_candidates.py --session morning|evening
      --news out/news_scan.json --out-dir out [--model ...] [--dry-run]

退出码：0 = 简报文件已产出；1 = 模型全失败或输入缺失。
简报只是参考，不构成投资建议；交易请自行决策。
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("xiaoyuan")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 默认模型：openai/gpt-5-mini（付费；账号已充值，不再遍历免费模型）。
MODEL_CANDIDATES = [
  "openai/gpt-5-mini",
]

# 场次 -> 覆盖市场（早上出 A股+港股，晚上出美股）
SESSION_MARKETS = {"morning": ["CN", "HK"], "evening": ["US"]}
SESSION_LABEL = {"morning": "早盘（A股+港股）", "evening": "晚盘（美股）"}

MAX_NEWS_FOR_PROMPT = 30  # 喂给模型的新闻条数上限（控 token）
VERDICTS = ("持有", "观望", "注意")  # 自选股评估只允许这三个结论
MAX_CANDIDATES_PER_MARKET = 5

CST = timezone(timedelta(hours=8))


def supabase_get(base_url, api_key, table, params):
    """调 Supabase PostgREST GET，返回 JSON 列表。"""
    import requests

    resp = requests.get(
        base_url.rstrip("/") + f"/rest/v1/{table}",
        params=params,
        headers={"apikey": api_key, "Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json() or []


def load_watchlist(base_url, api_key):
    """读 Supabase watchlist 表（自选股）；表是空的也照常返回空列表。

    注意：002 建的 watchlist 表里有 added_at 列（timestamptz，default now()），
    没有 created_at 列——按 added_at 排序本身没错。但表刚建完就跑工作流时，
    PostgREST 的 schema 缓存可能还没刷出来，带 order 的查询会报 400。
    所以先按 symbol 排序查（symbol 是主键列，一定在），失败就去掉排序
    重试一次；两次都失败才抛错（main 里会按空自选股跑，不会因为
    查不到自选股就丢掉整期简报）。
    """
    try:
        rows = supabase_get(
            base_url,
            api_key,
            "watchlist",
            {"select": "symbol,market,name,note", "order": "symbol.asc"},
        )
    except Exception as exc:  # noqa: BLE001 - 排序查询失败就去掉排序重试
        logger.warning("读 watchlist（带排序）失败，去掉排序重试一次: %s", exc)
        time.sleep(2)
        rows = supabase_get(
            base_url, api_key, "watchlist", {"select": "symbol,market,name,note"}
        )
    out = []
    for row in rows:
        market = (row.get("market") or "").strip().upper()
        symbol = str(row.get("symbol") or "").strip()
        if market not in ("CN", "US", "HK") or not symbol:
            continue
        if market == "CN":
            symbol = symbol.zfill(6)
        elif market == "HK":
            symbol = symbol.zfill(4)
        out.append(
            {
                "symbol": symbol,
                "market": market,
                "name": row.get("name") or "",
                "note": row.get("note") or "",
            }
        )
    return out


def load_latest_quotes(base_url, api_key, symbols):
    """读自选股最近 5 天行情，返回 {symbol: {close, change_pct, change_5d_pct, trade_date}}。"""
    if not symbols:
        return {}
    in_list = "(" + ",".join(symbols) + ")"
    rows = supabase_get(
        base_url,
        api_key,
        "market_daily_quotes",
        {
            "select": "symbol,market,trade_date,close,volume",
            "symbol": f"in.{in_list}",
            "order": "trade_date.desc",
            "limit": "1000",
        },
    )
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]), []).append(row)
    out = {}
    for symbol, rs in by_symbol.items():
        rs.sort(key=lambda r: r["trade_date"], reverse=True)
        last = rs[0]
        try:
            close = float(last["close"])
        except (TypeError, ValueError):
            continue
        info = {
            "close": close,
            "trade_date": last["trade_date"],
            "change_pct": None,
            "change_5d_pct": None,
        }
        if len(rs) >= 2:
            try:
                prev = float(rs[1]["close"])
                info["change_pct"] = round((close - prev) / prev * 100, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if len(rs) >= 2:
            try:
                first = float(rs[-1]["close"])
                info["change_5d_pct"] = round((close - first) / first * 100, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        out[symbol] = info
    return out


def build_prompt(session, news_data, watchlist, quotes):
    """拼喂给模型的提示词：指数 + 新闻 + 自选股行情，要求输出严格 JSON。"""
    markets = SESSION_MARKETS[session]
    market_names = {"CN": "A股", "US": "美股", "HK": "港股"}
    indices = news_data.get("indices") or {}
    index_lines = []
    for key in ("SP500", "NASDAQ", "HSI", "SSE"):
        info = indices.get(key)
        if not info:
            continue
        index_lines.append(
            f"- {info['name']}：最新 {info['close']}，涨跌 {info['change_pct']}%，近 5 日 {info['change_5d_pct']}%"
        )
    news_lines = []
    for item in (news_data.get("news") or [])[:MAX_NEWS_FOR_PROMPT]:
        summary = f"——{item.get('summary') or ''}" if item.get("summary") else ""
        news_lines.append(f"- 【{item.get('source') or '新闻'}】{item.get('title') or ''}{summary}")
    watch_lines = []
    for w in watchlist:
        if w["market"] not in markets:
            continue
        q = quotes.get(w["symbol"]) or {}
        price = (
            f"最新价 {q['close']}（{q['trade_date']}），日涨跌 {q['change_pct']}%，近 5 日 {q['change_5d_pct']}%"
            if q.get("close") is not None
            else "暂无行情数据"
        )
        note = f"，备注：{w['note']}" if w.get("note") else ""
        watch_lines.append(
            f"- {w['symbol']}（{w['market']}，{w['name'] or '未命名'}）：{price}{note}"
        )

    system = (
        "你是一名投研助手，只做信息整理与参考建议，不构成投资建议。"
        "输出必须是严格 JSON，不要用 Markdown 包裹，不要加任何解释文字。"
    )
    user = f"""今天是 {datetime.now(CST).strftime('%Y-%m-%d')}，本场是{SESSION_LABEL[session]}，只覆盖以下市场：{'、'.join(market_names[m] for m in markets)}。

全球主要指数：
{chr(10).join(index_lines) if index_lines else '- 暂无指数数据'}

最近 24 小时全球财经与政治新闻（标题+摘要）：
{chr(10).join(news_lines) if news_lines else '- 暂无新闻数据'}

自选股（只评估本场覆盖市场的）：
{chr(10).join(watch_lines) if watch_lines else '- 自选股表目前是空的'}

请输出以下 JSON（字段名固定）：
{{
  "candidates": [
    {{"symbol": "代码", "market": "CN/US/HK", "name": "名称", "reason": "入选理由，必须对应上面某条具体新闻或指数走势", "risk": "风险点", "confidence": "高/中/低"}}
  ],
  "watchlist": [
    {{"symbol": "代码", "market": "CN/US/HK", "verdict": "持有/观望/注意", "note": "当日评估与交易建议，一两句话"}}
  ]
}}

规则：
- candidates 每个市场最多 {MAX_CANDIDATES_PER_MARKET} 只，必须是真实上市的股票代码；A股 6 位数字、港股 4-5 位数字、美股 1-5 位字母；不要编造代码。
- 每只候选必须有 risk（风险点），不能只说利好；confidence 只能是 高/中/低。
- watchlist 里要评估上面列出的每一只本场自选股，verdict 只能是 持有/观望/注意 三选一；自选股为空时返回空数组。
- 只输出 JSON，不要输出其他文字。"""
    return system, user


def _extract_json(text):
    """从模型返回文本里提取 JSON 对象。

    依次尝试：整体解析 → 去 ```json 围栏后解析 → 截第一个 { 到最后一个 }
    之间的内容解析 → 贪婪匹配 { ... } 解析。模型常在 JSON 前后加解释
    文字或用围栏包裹，整体解析失败不等于没返回 JSON。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("模型返回为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("模型返回不是 JSON")


def call_openrouter(api_key, system, user, model_override=None):
    """按候选顺序调 OpenRouter，返回 (模型名, 解析后的 JSON)。全失败抛错。

    加固（2026-11-11 run #2 的教训）：
    - HTTP 429（上游临时限流）：每个模型等 30 秒后重试一次，再换下一个。
    - 响应体不是合法 JSON / 缺 choices 字段 / 带 error 字段时，记清原因
      再换下一个，不再让 KeyError 这类原始异常直接冒泡成“全部失败”。
    - 请求头只用 ASCII（X-Title 曾写中文导致 latin-1 编码报错），
      请求体用 UTF-8 字节发。
    """
    import requests

    models = [model_override] if model_override else MODEL_CANDIDATES
    last_error = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "HTTP-Referer": "https://github.com/mashaozhi/ai-studio",
        "X-Title": "Xiaoyuan Investment Brief",
    }
    payload_template = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    for model in models:
        payload = dict(payload_template, model=model)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in (1, 2):
            try:
                resp = requests.post(
                    OPENROUTER_URL, headers=headers, data=body, timeout=120
                )
            except Exception as exc:  # noqa: BLE001 - 网络异常不重试，换下一个
                last_error = f"{model} 请求异常: {exc}"
                logger.warning("模型 %s 请求异常，换下一个: %s", model, exc)
                break
            if resp.status_code == 429:
                if attempt == 1:
                    logger.warning("模型 %s 被限流（429），30 秒后重试一次", model)
                    time.sleep(30)
                    continue
                last_error = f"{model} HTTP 429: {resp.text[:300]}"
                logger.warning("模型 %s 限流重试后仍 429，换下一个", model)
                break
            if resp.status_code != 200:
                last_error = f"{model} HTTP {resp.status_code}: {resp.text[:300]}"
                logger.warning("模型 %s 调用失败: %s", model, last_error)
                break
            try:
                raw = resp.json()
            except ValueError:
                last_error = f"{model} 返回不是 JSON: {resp.text[:300]}"
                logger.warning("模型 %s 失败，换下一个: %s", model, last_error)
                break
            if isinstance(raw, dict) and raw.get("error"):
                last_error = f"{model} 返回错误: {raw['error']}"
                logger.warning("模型 %s 失败，换下一个: %s", model, last_error)
                break
            try:
                content = (raw["choices"][0]["message"]["content"] or "").strip()
            except (KeyError, IndexError, TypeError):
                last_error = f"{model} 返回缺少 choices: {str(raw)[:300]}"
                logger.warning("模型 %s 失败，换下一个: %s", model, last_error)
                break
            try:
                data = _extract_json(content)
            except Exception as exc:  # noqa: BLE001 - 解析失败就换下一个模型
                last_error = f"{model} 返回无法解析为 JSON: {exc}"
                logger.warning("模型 %s 失败，换下一个: %s", model, last_error)
                break
            if not isinstance(data, dict) or "candidates" not in data:
                last_error = f"{model} 返回缺少 candidates 字段"
                logger.warning("模型 %s 失败，换下一个: %s", model, last_error)
                break
            logger.info("模型 %s 返回成功", model)
            return model, data
        time.sleep(1)
    raise RuntimeError(f"OpenRouter 模型全部失败，最后错误：{last_error}")


def validate_candidates(raw, markets):
    """校验候选标的：市场、代码格式、必填字段；不合格的丢掉并记日志。"""
    out = []
    per_market = {m: 0 for m in markets}
    for cand in raw or []:
        market = (cand.get("market") or "").strip().upper()
        symbol = str(cand.get("symbol") or "").strip().upper()
        if market not in markets:
            continue
        if market == "CN" and not re.fullmatch(r"\d{6}", symbol):
            logger.warning("丢弃无效 A股候选：%s", cand)
            continue
        if market == "HK" and not re.fullmatch(r"\d{4,5}", symbol):
            logger.warning("丢弃无效港股候选：%s", cand)
            continue
        if market == "US" and not re.fullmatch(r"[A-Z]{1,5}", symbol):
            logger.warning("丢弃无效美股候选：%s", cand)
            continue
        if not (cand.get("reason") or "").strip() or not (cand.get("risk") or "").strip():
            logger.warning("丢弃缺理由/风险的候选：%s", cand)
            continue
        if per_market[market] >= MAX_CANDIDATES_PER_MARKET:
            continue
        per_market[market] += 1
        confidence = (cand.get("confidence") or "中").strip()
        if confidence not in ("高", "中", "低"):
            confidence = "中"
        out.append(
            {
                "symbol": symbol,
                "market": market,
                "name": (cand.get("name") or "").strip(),
                "reason": (cand.get("reason") or "").strip(),
                "risk": (cand.get("risk") or "").strip(),
                "confidence": confidence,
            }
        )
    return out


def validate_watchlist_evals(raw, watchlist, markets):
    """校验自选股评估：只保留本场自选股，verdict 非法的一律归为 观望。"""
    wanted = {
        (w["symbol"], w["market"])
        for w in watchlist
        if w["market"] in markets
    }
    out = []
    for item in raw or []:
        market = (item.get("market") or "").strip().upper()
        symbol = str(item.get("symbol") or "").strip()
        if market == "CN":
            symbol = symbol.zfill(6)
        elif market == "HK":
            symbol = symbol.zfill(4)
        if (symbol, market) not in wanted:
            continue
        verdict = (item.get("verdict") or "").strip()
        if verdict not in VERDICTS:
            verdict = "观望"
        out.append(
            {
                "symbol": symbol,
                "market": market,
                "verdict": verdict,
                "note": (item.get("note") or "").strip(),
            }
        )
    # 模型漏评的自选股补一条 观望，避免静默缺失
    seen = {(e["symbol"], e["market"]) for e in out}
    for w in watchlist:
        if w["market"] in markets and (w["symbol"], w["market"]) not in seen:
            out.append(
                {
                    "symbol": w["symbol"],
                    "market": w["market"],
                    "verdict": "观望",
                    "note": "本场模型未给出评估，暂按观望处理。",
                }
            )
    return out


def render_markdown(brief_date, session, indices, news_data, candidates, evals):
    """把简报渲染成 Markdown。"""
    market_names = {"CN": "A股", "US": "美股", "HK": "港股"}
    lines = [
        f"# 投研简报 {brief_date} {SESSION_LABEL[session]}",
        "",
        f"生成时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M')}（北京时间）",
        "",
        "## 全球市场走势",
        "",
    ]
    if indices:
        lines.append("| 指数 | 最新 | 涨跌 | 近 5 日 |")
        lines.append("| ---- | ---- | ---- | ------- |")
        for key in ("SP500", "NASDAQ", "HSI", "SSE"):
            info = indices.get(key)
            if info:
                lines.append(
                    f"| {info['name']} | {info['close']} | {info['change_pct']}% | {info['change_5d_pct']}% |"
                )
    else:
        lines.append("暂无指数数据。")
    lines += ["", "## 要闻摘要", ""]
    top_news = (news_data.get("news") or [])[:8]
    if top_news:
        for item in top_news:
            lines.append(f"- {item.get('source') or '新闻'}：{item.get('title') or ''}")
    else:
        lines.append("暂无新闻数据。")
    lines += ["", "## 本场机会标的", ""]
    if candidates:
        for market in SESSION_MARKETS[session]:
            group = [c for c in candidates if c["market"] == market]
            if not group:
                continue
            lines.append(f"### {market_names[market]}")
            lines.append("")
            for c in group:
                lines.append(
                    f"- **{c['symbol']} {c['name']}**（信心{c['confidence']}）"
                )
                lines.append(f"  - 理由：{c['reason']}")
                lines.append(f"  - 风险：{c['risk']}")
            lines.append("")
    else:
        lines.append("本场暂无入选标的。")
    lines += ["", "## 自选股当日评估", ""]
    if evals:
        lines.append("| 代码 | 市场 | 建议 | 评估 |")
        lines.append("| ---- | ---- | ---- | ---- |")
        for e in evals:
            lines.append(
                f"| {e['symbol']} | {market_names[e['market']]} | {e['verdict']} | {e['note']} |"
            )
    else:
        lines.append("自选股表目前是空的，先往 Supabase watchlist 表里加股票（见 README）。")
    lines += [
        "",
        "---",
        "",
        "仅供参考，不构成投资建议；交易请自行决策。",
        "",
    ]
    return "\n".join(lines)


def save_brief_to_supabase(base_url, api_key, brief_date, session, markets, markdown, candidates):
    """把简报写进 briefs 表；失败只记日志（文件已产出，不算整体失败）。"""
    import requests

    url = (
        base_url.rstrip("/")
        + "/rest/v1/briefs?on_conflict=brief_date,session"
    )
    resp = requests.post(
        url,
        headers={
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json={
            "brief_date": brief_date,
            "session": session,
            "markets": ",".join(markets),
            "markdown": markdown,
            "candidates": candidates,
        },
        timeout=20,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")


def main():
    parser = argparse.ArgumentParser(description="小源的选股：出当期机会标的 + 自选股当日评估")
    parser.add_argument("--session", default="morning", choices=["morning", "evening"])
    parser.add_argument("--news", default="out/news_scan.json")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--model", default="", help="指定模型（默认 openai/gpt-5-mini）")
    parser.add_argument("--dry-run", action="store_true", help="只打印提示词、不调模型")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    news_path = Path(args.news)
    if not news_path.exists():
        print(f"找不到新闻摘要 {news_path}，先跑 scripts/news_scan.py", file=sys.stderr)
        return 1
    with open(news_path, encoding="utf-8") as f:
        news_data = json.load(f)

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("缺少环境变量 SUPABASE_URL 或 SUPABASE_ANON_KEY", file=sys.stderr)
        return 2
    if not openrouter_key and not args.dry_run:
        print("缺少环境变量 OPENROUTER_API_KEY", file=sys.stderr)
        return 2

    markets = SESSION_MARKETS[args.session]
    try:
        watchlist = load_watchlist(supabase_url, supabase_key)
    except Exception as exc:  # noqa: BLE001 - 自选股读不到就按空表跑
        logger.warning("读 Supabase watchlist 失败，按空自选股跑: %s", exc)
        watchlist = []
    watch_in_scope = [w for w in watchlist if w["market"] in markets]
    quotes = {}
    try:
        quotes = load_latest_quotes(
            supabase_url, supabase_key, [w["symbol"] for w in watch_in_scope]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读自选股行情失败，按无行情跑: %s", exc)

    logger.info("自选股 %d 只（本场 %d 只），新闻 %d 条", len(watchlist), len(watch_in_scope), len(news_data.get("news") or []))

    system, user = build_prompt(args.session, news_data, watchlist, quotes)
    if args.dry_run:
        print("===== SYSTEM =====")
        print(system)
        print("===== USER =====")
        print(user)
        logger.info("[DRY RUN] 未调模型")
        return 0

    model_used, data = call_openrouter(openrouter_key, system, user, args.model or None)
    candidates = validate_candidates(data.get("candidates"), markets)
    evals = validate_watchlist_evals(data.get("watchlist"), watchlist, markets)
    logger.info("候选标的 %d 只，自选股评估 %d 只（模型：%s）", len(candidates), len(evals), model_used)

    brief_date = datetime.now(CST).strftime("%Y-%m-%d")
    indices = news_data.get("indices") or {}
    markdown = render_markdown(brief_date, args.session, indices, news_data, candidates, evals)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"brief_{brief_date}_{args.session}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    brief_json = {
        "brief_date": brief_date,
        "session": args.session,
        "markets": markets,
        "generated_at": datetime.now(CST).isoformat(),
        "model": model_used,
        "indices": indices,
        "candidates": candidates,
        "watchlist_evals": evals,
        "news_count": len(news_data.get("news") or []),
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(brief_json, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    logger.info("简报已写 %s 和 %s", json_path, md_path)

    try:
        save_brief_to_supabase(
            supabase_url, supabase_key, brief_date, args.session, markets, markdown, candidates
        )
        logger.info("简报已写入 Supabase briefs 表")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写 briefs 表失败（文件已产出，不影响本次）：%s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
