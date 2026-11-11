-- 小源的数据管道：纠正 watchlist 的 market 标注
-- ⚠️ 只有先执行 sql/005_verify_watchlist.sql、确认分布与种子文件不符
-- （例如部署时报过 CN 13、HK 7、US 12）才执行本文件；核对没问题就不要执行。
-- 以 sql/004_watchlist_seed.sql 为准：CN 21、HK 3、US 8，共 32 行。
--
-- 写法：按 symbol 逐行改回正确的市场，不碰种子文件之外的行（用户后来
-- 自己加的自选股不受影响）。幂等，可重复执行。
-- 前提：总行数仍为 32、每个 symbol 在表里只出现一次（005 的第 3 条查出
-- 总行数是 32 即可）。如果 005 查出重复 symbol 或多余行，先人工核对，
-- 不要直接执行本文件。
-- 执行后用 sql/005 再核对一次，分布应为 CN 21、HK 3、US 8。

-- A股 21 只
update public.watchlist set market = 'CN' where symbol in (
  '600584', '688008', '000988', '688313', '688127', '002241', '003021',
  '000725', '300170', '002343', '300364', '600900', '601088', '600941',
  '300750', '515120', '588000', '603893', '300458', '920418', '688507'
);

-- 港股 3 只
update public.watchlist set market = 'HK' where symbol in (
  '0700', '1810', '09896'
);

-- 美股 8 只（注意：伯克希尔哈撒韦 B 入库用 yfinance 格式 BRK-B）
update public.watchlist set market = 'US' where symbol in (
  'BRK-B', 'GLW', 'TSLA', 'SPCX', 'NVDA', 'SKDD', 'SKHY', 'MU'
);
