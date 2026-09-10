create or replace function public.excel_sync_secret_get(p_name text)
returns text
language sql
security definer
set search_path = ''
as $$
  select decrypted_secret
  from vault.decrypted_secrets
  where name = p_name
  limit 1;
$$;

create or replace function public.excel_sync_secret_set(p_name text, p_value text)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  existing_id uuid;
begin
  if p_name not in (
    'excel_microsoft_client_id',
    'excel_microsoft_refresh_token',
    'excel_onedrive_share_url',
    'excel_sync_allowed_user_ids'
  ) then
    raise exception 'Nome de configuração do Excel Online não permitido.';
  end if;

  if nullif(btrim(p_value), '') is null then
    raise exception 'O valor da configuração do Excel Online não pode ficar vazio.';
  end if;

  select id
    into existing_id
    from vault.secrets
   where name = p_name
   limit 1;

  if existing_id is null then
    perform vault.create_secret(
      p_value,
      p_name,
      'Configuração privada da sincronização do Excel Online'
    );
  else
    perform vault.update_secret(
      existing_id,
      p_value,
      p_name,
      'Configuração privada da sincronização do Excel Online'
    );
  end if;

  return true;
end;
$$;

revoke all on function public.excel_sync_secret_get(text) from public, anon, authenticated;
revoke all on function public.excel_sync_secret_set(text, text) from public, anon, authenticated;
grant execute on function public.excel_sync_secret_get(text) to service_role;
grant execute on function public.excel_sync_secret_set(text, text) to service_role;
