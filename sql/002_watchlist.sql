-- 小源的数据管道：自选股表 watchlist
-- 在 Supabase Dashboard → SQL Editor 里粘贴执行，一次即可。
-- 自选股不再写固定清单文件，往这张表插一行就是加一只股票。
-- 初始 32 只见 sql/004_watchlist_seed.sql。

create table if not exists public.watchlist (
  symbol   text        not null,  -- 入库代码：A股 6 位（如 600519）、港股 4-5 位（如 0700、09896）、美股字母（如 AAPL、BRK-B）
  market   text        not null check (market in ('CN', 'US', 'HK')),
  name     text,                  -- 名称（只用来写日志和简报，不参与拉数）
  note     text,                  -- 备注（如“北交所”“ETF”“2倍做空”）
  added_at timestamptz not null default now(),
  primary key (symbol, market)
);

-- RLS：本项目是个人管道，脚本用 ANON KEY 读写这张表。
alter table public.watchlist enable row level security;

grant select, insert, update, delete on public.watchlist to anon;

drop policy if exists "anon_all_watchlist" on public.watchlist;
create policy "anon_all_watchlist" on public.watchlist
  for all to anon using (true) with check (true);

-- 加一只自选股（示例，执行时把代码和名字换掉）：
-- insert into public.watchlist (symbol, market, name) values ('600519', 'CN', '贵州茅台')
--   on conflict (symbol, market) do nothing;
-- 删一只自选股：
-- delete from public.watchlist where symbol = '600519' and market = 'CN';

notify pgrst, 'reload schema';
