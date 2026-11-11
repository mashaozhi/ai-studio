-- 小源的数据管道：简报表 briefs
-- 在 Supabase Dashboard → SQL Editor 里粘贴执行，一次即可。
-- 每天两期简报各写一行（早上 A股+港股、晚上美股），同一天同一场次重复跑会覆盖。

create table if not exists public.briefs (
  id          uuid        primary key default gen_random_uuid(),
  brief_date  date        not null,  -- 简报日期（北京时间）
  session     text        not null check (session in ('morning', 'evening')),
  markets     text        not null,  -- 'CN,HK' 或 'US'
  markdown    text        not null,  -- 简报全文（Markdown）
  candidates  jsonb       not null default '[]'::jsonb,  -- 当期机会标的（JSON）
  created_at  timestamptz not null default now(),
  unique (brief_date, session)
);

create index if not exists idx_briefs_date
  on public.briefs (brief_date desc);

-- RLS：脚本用 ANON KEY 写简报；读简报也是 anon（个人项目）。
alter table public.briefs enable row level security;

grant select, insert, update on public.briefs to anon;

drop policy if exists "anon_select_briefs" on public.briefs;
create policy "anon_select_briefs" on public.briefs
  for select to anon using (true);

drop policy if exists "anon_insert_briefs" on public.briefs;
create policy "anon_insert_briefs" on public.briefs
  for insert to anon with check (true);

drop policy if exists "anon_update_briefs" on public.briefs;
create policy "anon_update_briefs" on public.briefs
  for update to anon using (true) with check (true);

-- 查最近一期简报：
-- select brief_date, session, markets, markdown from public.briefs
--   order by brief_date desc, session desc limit 1;

notify pgrst, 'reload schema';
