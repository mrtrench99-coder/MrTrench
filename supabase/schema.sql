-- Run this once in the Supabase SQL Editor for the MrTrench project.
-- Each signed-in account can access only its own row through Supabase Auth RLS.
create table if not exists public.mrtrench_user_state (
    user_id uuid primary key references auth.users (id) on delete cascade,
    sqlite_state_base64 text not null default '',
    encrypted_settings text not null default '',
    updated_at timestamptz not null default now()
);

alter table public.mrtrench_user_state enable row level security;

drop policy if exists "Users can manage their own MrTrench state"
    on public.mrtrench_user_state;
create policy "Users can manage their own MrTrench state"
    on public.mrtrench_user_state
    for all
    to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

revoke all on table public.mrtrench_user_state from anon;
grant select, insert, update, delete on table public.mrtrench_user_state to authenticated;
