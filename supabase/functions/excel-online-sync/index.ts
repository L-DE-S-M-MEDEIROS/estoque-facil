import "jsr:@supabase/functions-js/edge-runtime.d.ts";

Deno.serve(() => new Response(JSON.stringify({
  error: "A integração com o Excel Online foi desativada. Use o Google Planilhas.",
  code: "excel_online_disabled",
}), {
  status: 410,
  headers: {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
  },
}));
