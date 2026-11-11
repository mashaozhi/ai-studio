-- 小源的数据管道：行情表 market_daily_quotes
-- 在 Supabase Dashboard → SQL Editor 里粘贴执行，一次即可。
-- 表结构：每天每个股票一行，按 symbol + trade_date 去重（upsert 用）。

create table if not exists public.market_daily_quotes (
  symbol     text        not null,  -- 入库代码：A股 6 位（如 600519）、港股 4 位（如 0700）、美股字母（如 AAPL）
  market     text        not null check (market in ('CN', 'US', 'HK')),
  trade_date date        not null,  -- 交易日
  open       numeric(18, 4),       -- 开盘价
  high       numeric(18, 4),       -- 最高价
  low        numeric(18, 4),       -- 最低价
  close      numeric(18, 4),       -- 收盘价
  volume     bigint,               -- 成交量（A股为手，美股/港股为股，见 README）
  source     text        not null,  -- 数据来源：yfinance / tencent / eastmoney
  updated_at timestamptz not null default now(),
  primary key (symbol, trade_date)
);

-- 按市场+日期查行情的常用姿势，加个索引
create index if not exists idx_mdq_market_date
  on public.market_daily_quotes (market, trade_date desc);

-- RLS：本项目是个人管道，脚本用 ANON KEY 写库，
-- 所以要给 anon 角色开这张表的读写权限。
-- 注意：ANON KEY 不是密钥里的 service_role，权限只限这一张表；
-- 如果以后想收紧，把策略删掉、改用 service_role key 跑脚本。
alter table public.market_daily_quotes enable row level security;

grant select, insert, update on public.market_daily_quotes to anon;

drop policy if exists "anon_select_mdq" on public.market_daily_quotes;
create policy "anon_select_mdq" on public.market_daily_quotes
  for select to anon using (true);

drop policy if exists "anon_insert_mdq" on public.market_daily_quotes;
create policy "anon_insert_mdq" on public.market_daily_quotes
  for insert to anon with check (true);

drop policy if exists "anon_update_mdq" on public.market_daily_quotes;
create policy "anon_update_mdq" on public.market_daily_quotes
  for update to anon using (true) with check (true);

-- 建表/改主键后让 PostgREST 重新读 schema，否则 upsert 可能报
-- "no unique or exclusion constraint"（缓存还没刷新）
notify pgrst, 'reload schema';
