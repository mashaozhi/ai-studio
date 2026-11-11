#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小源的选股：把新闻摘要 + 指数 + 自选股最新行情喂给 OpenRouter 免费模型，
输出当期机会标的 + 自选股当日评估，落成 JSON 简报和 Markdown 简报，
并把简报写进 Supabase briefs 表（写库失败只记日志，不影响文件产出）。

模型（2026-11-11 核实，OpenRouter :free 免费模型，名单常变，按顺序试）：
  meta-llama/llama-3.3-70b-instruct:free（主用）
  google/gemma-4-26b-a4b-it:free
  nvidia/nemotron-3.5-lightning:free
  openrouter/free（官方免费路由器，随机挑一个免费模型）
免费额度：每天 50 次调用（未充值）；本管道每天只调 2 次（早晚各 1 次）。

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

# 免费模型候选（按顺序试；名单常变，某个下线就自动试下一个）
MODEL_CANDIDATES = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3.5-lightning:free",
    "openrouter/free",
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
    """读 Supabase watchlist 表（自选股）；表是空的也照常返回空列表。"""
    rows = supabase_get(
        base_url,
        api_key,
        "watchlist",
        {"select": "symbol,market,name,note", "order": "added_at.asc"},
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


def call_openrouter(api_key, system, user, model_override=None):
    """按候选顺序调 OpenRouter，返回 (模型名, 解析后的 JSON)。全失败抛错。"""
    import requests

    models = [model_override] if model_override else MODEL_CANDIDATES
    last_error = None
    for model in models:
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/mashaozhi/ai-studio",
                    "X-Title": "小源投研简报",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 4000,
                },
                timeout=120,
            )
            if resp.status_code != 200:
                last_error = f"{model} HTTP {resp.status_code}: {resp.text[:300]}"
                logger.warning("模型 %s 调用失败: %s", model, last_error)
                continue
            content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if not match:
                    raise ValueError("模型返回不是 JSON")
                data = json.loads(match.group(0))
            if not isinstance(data, dict) or "candidates" not in data:
                raise ValueError("模型返回缺少 candidates 字段")
            logger.info("模型 %s 返回成功", model)
            return model, data
        except Exception as exc:  # noqa: BLE001 - 换下一个模型试
            last_error = str(exc)
            logger.warning("模型 %s 失败，换下一个: %s", model, exc)
        time.sleep(1)
    raise RuntimeError(f"OpenRouter 免费模型全部失败，最后错误：{last_error}")


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
    parser.add_argument("--model", default="", help="指定模型（默认按候选顺序自动试）")
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
