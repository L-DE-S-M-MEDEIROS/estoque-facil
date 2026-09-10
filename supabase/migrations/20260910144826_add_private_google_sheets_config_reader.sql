create or replace function public.google_sheets_secret_get(p_name text)
returns text
language sql
security definer
set search_path = ''
as $$
  select decrypted_secret
  from vault.decrypted_secrets
  where name = p_name
    and p_name in ('google_sheets_allowed_emails', 'google_sheets_spreadsheet_id')
  limit 1;
$$;

revoke all on function public.google_sheets_secret_get(text) from public, anon, authenticated;
grant execute on function public.google_sheets_secret_get(text) to service_role;
