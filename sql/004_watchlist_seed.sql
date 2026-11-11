-- 小源的自选股初始种子数据：watchlist 表
-- 来源：用户 2026-11-11 16:36 发来的两张截图，共 32 只。
-- 先执行 sql/002_watchlist.sql 建表，再执行本文件。
-- 幂等：重复执行不会产生重复行（ON CONFLICT DO NOTHING）。
-- 执行后验证：select market, count(*) from public.watchlist group by market;
--   应为 CN 21、HK 3、US 8，共 32 行。

insert into public.watchlist (symbol, market, name, note) values
  -- A股 21 只
  ('600584', 'CN', '长电科技', null),
  ('688008', 'CN', '澜起科技', null),
  ('000988', 'CN', '华工科技', null),
  ('688313', 'CN', '仕佳光子', null),
  ('688127', 'CN', '蓝特光学', null),
  ('002241', 'CN', '歌尔股份', null),
  ('003021', 'CN', '兆威机电', null),
  ('000725', 'CN', '京东方A', null),
  ('300170', 'CN', '汉得信息', null),
  ('002343', 'CN', '慈文传媒', null),
  ('300364', 'CN', '中文在线', null),
  ('600900', 'CN', '长江电力', null),
  ('601088', 'CN', '中国神华', null),
  ('600941', 'CN', '中国移动', null),
  ('300750', 'CN', '宁德时代', null),
  ('515120', 'CN', '创新药ETF广发', 'ETF'),
  ('588000', 'CN', '科创50ETF华夏', 'ETF'),
  ('603893', 'CN', '瑞芯微', null),
  ('300458', 'CN', '全志科技', null),
  ('920418', 'CN', '苏轴股份', '北交所'),
  ('688507', 'CN', '索辰科技', null),
  -- 港股 3 只
  ('0700', 'HK', '腾讯控股', null),
  ('1810', 'HK', '小米集团-W', null),
  ('09896', 'HK', '名创优品', null),
  -- 美股 8 只
  -- 注意：用户清单写的是 BRK.B，入库用 yfinance 格式 BRK-B（带点的写法
  -- yfinance 不认，候选校验的正则也不认），含义不变。
  ('BRK-B', 'US', '伯克希尔哈撒韦B', '用户清单写的是 BRK.B，入库用 yfinance 格式 BRK-B'),
  ('GLW', 'US', '康宁', null),
  ('TSLA', 'US', '特斯拉', null),
  -- SPCX：SpaceX 2026-06-12 纳斯达克上市（IPO 价 $135）；该代码 2026-04 前
  -- 属于一只 SPAC ETF，2026-06-12 之前的历史行情是旧 ETF 的，不连续。
  ('SPCX', 'US', 'SpaceX', 'SpaceX 2026-06-12 纳斯达克上市；该代码 2026-04 前属于一只 SPAC ETF，历史行情不连续'),
  ('NVDA', 'US', '英伟达', null),
  -- SKDD：GraniteShares 2 倍做空 SK 海力士的日 ETF（-2x，每日重置），2026-07-14 上市。
  ('SKDD', 'US', 'GraniteShares 2x Short SK Hynix Daily ETF', '2倍做空 SK 海力士的日 ETF，每日重置'),
  -- SKHY：SK 海力士美国存托凭证（ADR），2026-07-10 纳斯达克上市。
  ('SKHY', 'US', 'SK 海力士 ADR', 'SK Hynix 美国存托凭证，2026-07-10 纳斯达克上市'),
  ('MU', 'US', '美光科技', null)
on conflict (symbol, market) do nothing;
