-- 小源的数据管道：核对 watchlist 实际入库内容（只读查询，不改数据）
-- 在 Supabase Dashboard → SQL Editor 里执行。
-- 预期（以 sql/004_watchlist_seed.sql 为准）：CN 21 行、HK 3 行、US 8 行，共 32 行。
-- 若分布与预期不符（例如部署时报过 CN 13、HK 7、US 12），先看下面第一条
-- 查询列出的每一行，确认哪几只的 market 标错了，再按需执行
-- sql/006_fix_watchlist_markets.sql 纠正；不要没核对就直接执行 006。

-- 1) 全表明细（按市场、代码排序，逐行核对名称与市场是否对得上）
select symbol, market, name
from public.watchlist
order by market, symbol;

-- 2) 各市场行数汇总（应为 CN 21、HK 3、US 8）
select market, count(*) as rows
from public.watchlist
group by market
order by market;

-- 3) 总行数（应为 32）
select count(*) as total_rows from public.watchlist;
