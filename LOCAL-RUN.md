# 小源数据管道：在自己电脑上定时跑（本地模型路线）

## 为什么这条路能走

GitHub Actions 的云端跑不到你电脑的 `127.0.0.1`，所以本地模型只能在你自己电脑上用。
改成本地跑之后，三步都在你电脑上完成：新闻扫描 → 本地模型选标的出简报 → 拉行情入库，
模型走 `http://127.0.0.1:38650/v1`，不用 OpenRouter、不花钱。
`pick_candidates.py` 已改成支持本地接口（`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`）。

## 需要准备的东西

- Python 3.11（[python.org](https://www.python.org/downloads/) 下载，安装时勾选 Add Python to PATH）
- 代码：本目录的 `scripts/`（`news_scan.py`、`pick_candidates.py`、`fetch_quotes.py`）
- 三个值（都只放你自己电脑，别发聊天里）：
  - `SUPABASE_URL`：`https://jneooqtzquqmvyphigfm.supabase.co`（公开地址）
  - `SUPABASE_ANON_KEY`：Supabase Dashboard → Project Settings → API Keys → 点
    "View legacy API keys" → 复制 `anon public` 那串（eyJ 开头）
  - `LLM_BASE_URL`：`http://127.0.0.1:38650/v1`
  - `LLM_API_KEY`：本地接口的 key（就是你发我的那个）
  - `LLM_MODEL`：可选，不填时脚本自动调接口的 `/v1/models` 取第一个模型
- 本地模型服务：在跑的两个时间点（早上 08:00、晚上 20:30 前后）要是开着的，
  最好设成开机自启；电脑在那两个时间点也要开着且联网。

## 步骤 1：装依赖

```bash
pip install requests feedparser yfinance
```

## 步骤 2：手动试跑一次（早盘为例）

在代码目录里，先建一个运行脚本，把 key 写进去（只存本地）：

Windows（`run_morning.bat`，和 `scripts/` 同一级目录）：

```bat
@echo off
cd /d C:\ai-studio
set SUPABASE_URL=https://jneooqtzquqmvyphigfm.supabase.co
set SUPABASE_ANON_KEY=你的anon key
set LLM_BASE_URL=http://127.0.0.1:38650/v1
set LLM_API_KEY=你的本地接口key
python scripts\news_scan.py --session morning --out out\news_scan.json
python scripts\pick_candidates.py --session morning --news out\news_scan.json --out-dir out
python scripts\fetch_quotes.py --market CN,HK --symbols-json out\brief_20261112_morning.json
```

macOS / Linux（`run_morning.sh`）：

```bash
#!/bin/bash
cd ~/ai-studio
export SUPABASE_URL=https://jneooqtzquqmvyphigfm.supabase.co
export SUPABASE_ANON_KEY=你的anon key
export LLM_BASE_URL=http://127.0.0.1:38650/v1
export LLM_API_KEY=你的本地接口key
python3 scripts/news_scan.py --session morning --out out/news_scan.json
python3 scripts/pick_candidates.py --session morning --news out/news_scan.json --out-dir out
python3 scripts/fetch_quotes.py --market CN,HK --symbols-json out/brief_20261112_morning.json
```

注意：

- `brief_20261112_morning.json` 里的日期换成当天（格式 `YYYYMMDD`）；如果日期写错，
  第三步会提示"读候选 JSON 失败"，只拉自选股、不中断，不算失败。
- 晚盘把 `morning` 换成 `evening`、`CN,HK` 换成 `US`、`brief_..._morning.json`
  换成 `brief_..._evening.json`。
- 成功标志：`out/` 里出现当天的 `brief_YYYYMMDD_morning.json` 和 `.md`，
  且 Supabase `briefs` 表多了一行（Dashboard → Table Editor → briefs 查看）。

## 步骤 3：定时

- Windows：任务计划程序 → 创建基本任务 → 每天 08:00 运行 `run_morning.bat`、
  每天 20:30 运行 `run_evening.bat`（晚盘版，把上面脚本里的 morning/CN,HK 改掉另存一份）。
  勾选"不管用户是否登录都要运行"和"使用最高权限运行"（避免锁屏后不跑）。
- macOS：用 `launchd`，写两个 plist（`~/Library/LaunchAgents/`），
  `StartCalendarInterval` 分别设 08:00 和 20:30，`ProgramArguments` 指向两个 `.sh`。

## 步骤 4：验证通过后，把 GitHub 上的定时工作流停掉

本地跑通一天后，到 GitHub 仓库 → Actions → 左侧选"小源每日投研简报" →
点 "Disable workflow"，免得每天收到失败邮件。云端工作流停掉不影响本地跑。

## 排障

- `未指定模型：本地接口请设 LLM_MODEL...`：接口的 `/v1/models` 没返回模型，
  要么模型服务没启动，要么在 `.bat`/`.sh` 里加一行 `set LLM_MODEL=模型名`。
- `模型全部失败` + 日志里是连接失败：本地模型服务没开，先启动它再重跑。
- 模型返回不是 JSON：本地小模型偶尔会输出不合规的 JSON，脚本会记日志；
  多跑一次通常能过，长期这样再考虑换个大点的本地模型。
- 简报只是参考，不构成投资建议；交易请自行决策。
