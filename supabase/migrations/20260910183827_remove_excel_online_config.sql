drop function if exists public.excel_sync_secret_get(text);
drop function if exists public.excel_sync_secret_set(text, text);

delete from vault.secrets
where name in (
  'excel_microsoft_client_id',
  'excel_microsoft_refresh_token',
  'excel_onedrive_share_url',
  'excel_sync_allowed_user_ids'
);
