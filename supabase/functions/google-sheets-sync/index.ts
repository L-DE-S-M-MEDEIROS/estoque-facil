import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { buildSheetSnapshot, type InventoryPayload, type JsonRecord, todayInSaoPaulo } from "./logic.ts";

declare const Deno: {
  env: { get(name: string): string | undefined };
  serve(handler: (request: Request) => Response | Promise<Response>): void;
};

const WORKSPACE_KEY = "bolsas-baby";
const corsHeaders: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

class FunctionError extends Error {
  constructor(message: string, readonly status = 400, readonly code = "google_sheets_error") {
    super(message);
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Cache-Control": "no-store", "Content-Type": "application/json; charset=utf-8" },
  });
}

function environment(name: string): string {
  const value = Deno.env.get(name)?.trim();
  if (!value) throw new FunctionError("A configuração segura do Supabase está incompleta.", 503, "service_not_configured");
  return value;
}

function serviceRoleKey(): string {
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")?.trim();
  if (legacy) return legacy;
  try {
    const configured = JSON.parse(environment("SUPABASE_SECRET_KEYS")) as Record<string, string>;
    const key = configured.default?.trim();
    if (key) return key;
  } catch {
    // The common configuration error is returned below without exposing details.
  }
  throw new FunctionError("A configuração segura do Supabase está incompleta.", 503, "service_not_configured");
}

async function serviceRequest(path: string, init: RequestInit = {}): Promise<Response> {
  const key = serviceRoleKey();
  return await fetch(`${environment("SUPABASE_URL")}${path}`, {
    ...init,
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

async function responseBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function getConfig(name: "google_sheets_allowed_emails" | "google_sheets_spreadsheet_id"): Promise<string> {
  const response = await serviceRequest("/rest/v1/rpc/google_sheets_secret_get", {
    method: "POST",
    body: JSON.stringify({ p_name: name }),
  });
  const value = await responseBody(response);
  if (!response.ok || typeof value !== "string" || !value.trim()) {
    throw new FunctionError("A integração com o Google Planilhas ainda não foi configurada.", 503, "service_not_configured");
  }
  return value.trim();
}

function asRecord(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

async function requireGoogleUser(request: Request): Promise<string> {
  const authorization = request.headers.get("authorization") ?? "";
  const token = authorization.replace(/^Bearer\s+/i, "").trim();
  if (!token || token.length > 4096) throw new FunctionError("Autorização Google ausente.", 401, "google_auth_required");
  let verification: Response;
  try {
    verification = await fetch(`https://oauth2.googleapis.com/tokeninfo?access_token=${encodeURIComponent(token)}`);
  } catch {
    throw new FunctionError("Não foi possível validar a conta Google.", 503, "google_auth_unavailable");
  }
  const claims = asRecord(await responseBody(verification));
  const email = text(claims.email).toLowerCase();
  if (!verification.ok || !email || String(claims.email_verified).toLowerCase() !== "true") {
    throw new FunctionError("A autorização Google expirou ou é inválida.", 401, "invalid_google_auth");
  }
  let allowed: unknown;
  try {
    allowed = JSON.parse(await getConfig("google_sheets_allowed_emails"));
  } catch (error) {
    if (error instanceof FunctionError) throw error;
    allowed = [];
  }
  const emails = Array.isArray(allowed) ? allowed.map((item) => text(item).toLowerCase()).filter(Boolean) : [];
  if (!emails.includes(email)) throw new FunctionError("Esta conta Google não está autorizada para o estoque.", 403, "google_user_not_allowed");
  return email;
}

async function requireSpreadsheet(body: JsonRecord): Promise<void> {
  const received = text(body.spreadsheet_id);
  const expected = await getConfig("google_sheets_spreadsheet_id");
  if (!received || received !== expected) throw new FunctionError("Esta planilha não está autorizada para o estoque.", 403, "spreadsheet_not_allowed");
}

type RemoteSnapshot = {
  payload: InventoryPayload;
  revision: number;
  updated_at: string;
  device_id: string;
  updated_by: string | null;
};

async function remoteSnapshot(): Promise<RemoteSnapshot> {
  const query = new URLSearchParams({
    select: "payload,revision,updated_at,device_id,updated_by",
    workspace_key: `eq.${WORKSPACE_KEY}`,
  });
  const response = await serviceRequest(`/rest/v1/shared_inventory_snapshot?${query}`);
  const result = await responseBody(response);
  if (!response.ok || !Array.isArray(result) || result.length !== 1) {
    throw new FunctionError("O estoque online ainda não possui uma cópia válida.", 503, "inventory_unavailable");
  }
  const item = asRecord(result[0]);
  return {
    payload: asRecord(item.payload) as InventoryPayload,
    revision: Number(item.revision),
    updated_at: text(item.updated_at),
    device_id: text(item.device_id),
    updated_by: text(item.updated_by) || null,
  };
}

function sheetResponse(snapshot: RemoteSnapshot) {
  return {
    ok: true,
    revision: snapshot.revision,
    updated_at: snapshot.updated_at,
    ...buildSheetSnapshot(snapshot.payload, todayInSaoPaulo()),
  };
}

async function handle(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders });
  if (request.method !== "POST") return json({ error: "Método não permitido.", code: "method_not_allowed" }, 405);
  await requireGoogleUser(request);
  let body: JsonRecord;
  try {
    body = asRecord(await request.json());
  } catch {
    throw new FunctionError("O pedido da planilha não é JSON válido.", 400, "invalid_request");
  }
  await requireSpreadsheet(body);
  const action = text(body.action);
  if (action === "status" || action === "snapshot") return json(sheetResponse(await remoteSnapshot()));
  if (action === "record_count") {
    throw new FunctionError("As contagens da planilha são locais e não são enviadas ao aplicativo.", 410, "local_count_only");
  }
  throw new FunctionError("Ação desconhecida.", 400, "unknown_action");
}

Deno.serve(async (request: Request) => {
  try {
    return await handle(request);
  } catch (error) {
    if (error instanceof FunctionError) return json({ error: error.message, code: error.code }, error.status);
    return json({ error: "Falha inesperada na sincronização com o Google Planilhas.", code: "internal_error" }, 500);
  }
});
