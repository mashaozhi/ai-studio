# 小源的工作说明（数据专员）

小源负责 A股 / 美股 / 港股三地投研：每天开盘前按最新全球新闻、
全球市场走势、政治新闻给出有机会的投资标的；对自选股做监控评估，
给出当日交易建议（只做参考，不交易）。

## 新流程（2026-11-11 改版，不再跑固定股票清单）

```
新闻扫描 → 选标的出简报 → 拉候选与自选股行情入库 → 上传简报
news_scan.py → pick_candidates.py → fetch_quotes.py → Actions Artifacts + briefs 表
```

| 步骤 | 脚本 | 做什么 |
| ---- | ---- | ------ |
| 1. 新闻扫描 | `scripts/news_scan.py` | 拉 24 小时全球财经与政治新闻（RSS）+ 四大指数走势，输出 `out/news_scan.json` |
| 2. 选标的 | `scripts/pick_candidates.py` | 调 OpenRouter 免费模型，输出当期机会标的 + 自选股当日评估，落成 `out/brief_YYYYMMDD_场次.json/.md`，并写进 `briefs` 表 |
| 3. 拉数入库 | `scripts/fetch_quotes.py` | 拉候选标的 + 自选股的日线行情，upsert 进 `market_daily_quotes` 表 |
| 4. 简报归档 | GitHub Actions | 简报文件上传为本次运行的 Artifacts（可下载）；同时已在 `briefs` 表里 |

## 两期简报

| 场次 | 北京时间 | 覆盖市场 | 说明 |
| ---- | -------- | -------- | ---- |
| 早盘 morning | 每天 08:30 前出 | A股 + 港股 | 两边 09:30 开盘 |
| 晚盘 evening | 每天 21:00 前出 | 美股 | 夏令时 21:30 开盘（冬令时 22:30，21:00 出也赶得上） |

- 定时：GitHub Actions 每天 00:30 UTC（08:30 北京时间）和 13:00 UTC（21:00 北京时间）各跑一次。
- 手动：GitHub 仓库 → Actions →「小源每日投研简报」→ Run workflow，可指定场次。

## 数据来源（全部免费，无需注册、无需 key）

### 新闻（RSS，2026-11-11 核实）

| 来源 | 地址 | 状态 |
| ---- | ---- | ---- |
| Google News RSS 搜索（主源，按主题查） | `news.google.com/rss/search?q=...` | 2026-09 第三方实测可用 |
| BBC 商业 / 国际 RSS | `feeds.bbci.co.uk/news/business/rss.xml` 等 | 2026-10 第三方实测仍有新鲜内容 |
| Yahoo News 头条 | `news.yahoo.com/rss` | 可用 |
| Yahoo Finance 通用财经 feed | `finance.yahoo.com/news/rssindex` | 已下线（404），不用 |
| 路透 | — | 没有公开稳定的免费 RSS，不用 |

- 单个源挂了只记日志、跳过，不中断整批；新闻去重后最多喂给模型 30 条。
- 注意：RSS 源会不定期变动，连续几天新闻明显变少时，先看 `news_scan` 步骤的日志里哪个源失败了。

### 行情

| 市场 | 主源 | 备用 | 说明 |
| ---- | ---- | ---- | ---- |
| 美股 | yfinance（Yahoo Finance，1.8.x） | 东方财富 push2his（secid `105.AAPL`） | 代码如 `AAPL`；`BRK.B` 入库用 `BRK-B` |
| 港股 | yfinance（代码加 `.HK`，如 `0700.HK`） | 东方财富 push2his（secid `116.00700`） | 入库代码用 4-5 位，如 `0700`、`09896` |
| A股 | 腾讯行情日 K（`web.ifzq.gtimg.cn`，前复权） | 东方财富 push2his（secid `1.600519` 沪 / `0.000001` 深） | 入库代码用 6 位，如 `600519` |

- 每次拉最近 5 个交易日（A股取最近 10 根 K 线），upsert 去重，重复跑不产生重复行。
- 单只股票失败（停牌、节假日、接口抽风）只记日志、不中断整批，日志末尾有失败清单。
- yfinance 是批量拉取、串行请求，请求之间留了间隔，别触发雅虎限流。

### 选股模型（OpenRouter 免费）

- 按顺序试：`meta-llama/llama-3.3-70b-instruct:free`（主用）→
  `google/gemma-4-26b-a4b-it:free` → `nvidia/nemotron-3.5-lightning:free` →
  `openrouter/free`（官方免费路由器）。免费模型名单常变，某个下线就自动试下一个。
- 免费额度每天 50 次调用（未充值）；本管道每天只调 2 次（早晚各 1 次），够用。
- 模型输出会被校验：候选标的代码格式不对、缺理由或风险点的直接丢掉；
  自选股评估结论只允许 持有 / 观望 / 注意，模型漏评的补一条 观望。

## 表结构

### `market_daily_quotes`（行情，日线）

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| symbol | text | 入库代码（A股 6 位 / 港股 4-5 位 / 美股字母） |
| market | text | `CN` / `US` / `HK` |
| trade_date | date | 交易日 |
| open / high / low / close | numeric | 开 / 高 / 低 / 收 |
| volume | bigint | 成交量（见下） |
| source | text | `yfinance` / `tencent` / `eastmoney` |
| updated_at | timestamptz | 写入时间 |

主键：`(symbol, trade_date)`。建表 SQL 见 `sql/001_market_daily_quotes.sql`。

### `watchlist`（自选股）

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| symbol | text | 入库代码 |
| market | text | `CN` / `US` / `HK` |
| name | text | 名称 |
| note | text | 备注 |
| added_at | timestamptz | 加入时间 |

主键：`(symbol, market)`。建表 SQL 见 `sql/002_watchlist.sql`，
初始 32 只见 `sql/004_watchlist_seed.sql`。

### `briefs`（简报）

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| id | uuid | 主键 |
| brief_date | date | 简报日期（北京时间） |
| session | text | `morning` / `evening` |
| markets | text | `CN,HK` 或 `US` |
| markdown | text | 简报全文（Markdown） |
| candidates | jsonb | 当期机会标的（JSON） |
| created_at | timestamptz | 写入时间 |

唯一约束：`(brief_date, session)`，同一天同一场次重复跑会覆盖。
建表 SQL 见 `sql/003_briefs.sql`。

## 怎么加 / 删自选股

往 Supabase `watchlist` 表插一行就是加一只，删一行就是删一只。
在 Supabase Dashboard → SQL Editor 里执行（示例）：

```sql
-- 加一只：贵州茅台
insert into public.watchlist (symbol, market, name)
values ('600519', 'CN', '贵州茅台')
on conflict (symbol, market) do nothing;

-- 删一只
delete from public.watchlist where symbol = '600519' and market = 'CN';

-- 看当前自选股
select market, symbol, name, note from public.watchlist order by market, symbol;
```

注意：
- A股 6 位、港股 4-5 位、美股字母；`BRK.B` 请写 `BRK-B`（yfinance 格式）。
- 自选股表是空的也照常跑，只做候选标的，不报错。

## 简报存在哪

1. **Supabase `briefs` 表**：每期一行，含全文 Markdown 和候选 JSON。
   查最近一期：`select brief_date, session, markets, markdown from public.briefs order by brief_date desc, session desc limit 1;`
2. **GitHub Actions Artifacts**：每次运行 → Artifacts → 下载 `brief-morning-<运行号>` 或 `brief-evening-<运行号>`（含 `.json` 和 `.md`）。

## 失败了去哪看日志

GitHub 仓库 → Actions →「小源每日投研简报」→ 点开某次运行 → 各步骤的日志。
常见情况：

- 某只股票在失败清单里：多半是停牌或节假日无交易，等下次自动跑就行。
- 新闻源失败：`news_scan` 步骤的日志会写哪个源挂了；单个源挂不影响整批，
  连续几天新闻明显变少再来找原因（先看 RSS 源是不是又变了）。
- 模型全失败：`pick_candidates` 步骤会报最后错误；先看是不是免费模型名单变了
  （换模型名），再看 OpenRouter 当天 50 次额度是不是用完了。
- yfinance 整批失败：雅虎限流或接口变动，日志会显示改走逐只重试；
  连续几天都失败再来找原因（先看 yfinance 有没有发新版）。
- 入库报 401 / 403：检查仓库 Secrets 里的 `SUPABASE_URL` /
  `SUPABASE_ANON_KEY` 是否还在（密钥只放 Secrets，绝不写进代码）。
- 入库报 `no unique or exclusion constraint`：PostgREST 的 schema 缓存还没刷新，
  等几分钟重跑一次；SQL 文件末尾已有 `notify pgrst, 'reload schema'`。
- 模型报 401：检查 `OPENROUTER_API_KEY` 是否已加进仓库 Secrets（见下）。

## 本地调试

```bash
pip install yfinance requests feedparser
python scripts/news_scan.py --session morning --out /tmp/news_scan.json
OPENROUTER_API_KEY=xxx SUPABASE_URL=xxx SUPABASE_ANON_KEY=xxx \
  python scripts/pick_candidates.py --session morning --news /tmp/news_scan.json --out-dir /tmp/out
DRY_RUN=1 python scripts/fetch_quotes.py --market US --symbols-json /tmp/out/brief_*_morning.json
python scripts/pick_candidates.py --dry-run --news /tmp/news_scan.json  # 只打印提示词、不调模型
```

## 部署清单（浏览器要做的事）

1. 提交代码到仓库 `mashaozhi/ai-studio`：新增 `scripts/news_scan.py`、
   `scripts/pick_candidates.py`，改 `scripts/fetch_quotes.py`、
   `.github/workflows/xiaoyuan-daily.yml`、`README-XIAOYUAN.md`，
   删除 `data/watchlist.yaml`，新增 `sql/002_watchlist.sql`、
   `sql/003_briefs.sql`、`sql/004_watchlist_seed.sql`。
2. 在 Supabase SQL Editor 里依次执行 `sql/001_market_daily_quotes.sql`（如果还没建）、
   `sql/002_watchlist.sql`、`sql/003_briefs.sql`、`sql/004_watchlist_seed.sql`。
3. 确认仓库 Secrets：`SUPABASE_URL`、`SUPABASE_ANON_KEY`（已有）；
   **补加 `OPENROUTER_API_KEY`**——OpenRouter 的 ai-studio Key 已在仓库 Secrets 里，
   但不知道它存的 Secret 名叫什么，部署时核对一下：如果已有的那个 Secret 名就是
   `OPENROUTER_API_KEY` 就不用动，否则把 Key 的值另存一份叫 `OPENROUTER_API_KEY`。
4. 手动跑一次验证：Actions →「小源每日投研简报」→ Run workflow（选 morning），
   看四步都绿、简报文件能下载、Supabase `briefs` 表里多了一行、
   `market_daily_quotes` 表里多了一批当日行情。

## 注意事项（口径统一，别踩坑）

- **成交量单位不统一**：A股来源返回的是**手**（1 手 = 100 股），
  美股/港股返回的是**股**。入库存的是来源原值，没做换算；
  用量做分析时记得 A股先 ×100。
- **复权口径**：A股腾讯/东方财富都是**前复权**价；yfinance 用的是
  **未复权**价（`auto_adjust=False`）。算涨跌幅、均线时建议同一市场内比较。
- **SPCX 代码复用**：SpaceX 2026-06-12 才在纳斯达克以 SPCX 上市，
  该代码 2026-04 前属于一只 SPAC ETF，2026-06-12 之前的历史行情是旧 ETF 的，
  做回测、算长期涨幅时别直接用。
- **杠杆 ETF 每日重置**：SKDD 是 -2 倍做空 SK 海力士的日 ETF，只适合看当日，
  拿多天会偏离 -2 倍，别按普通股票算持有收益。
- **北交所**：920418 苏轴股份是北交所股票；腾讯接口前缀逻辑目前按深市处理，
  拉不到时会走东方财富兜底，兜底也失败就进失败清单（不中断整批）。
- **免费额度**：GitHub Actions 公开仓库免费；Supabase 免费项目；
  OpenRouter 免费模型每天 50 次（本管道每天用 2 次）；
  三个数据源都免费、无需 key。什么都不用充钱。
- **密钥规则**：`SUPABASE_URL`、`SUPABASE_ANON_KEY`、`OPENROUTER_API_KEY`
  只存在仓库 Secrets 里，代码只从环境变量读，绝不写进代码、日志和文档。
- **简报只是参考**：机会标的和交易建议都由模型按新闻+行情整理，
  不构成投资建议；交易请自行决策。
