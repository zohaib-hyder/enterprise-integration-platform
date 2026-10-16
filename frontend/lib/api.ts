export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(path: string, options: { method?: string; data?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers = new Headers();
  if (method !== "GET") {
    const verification = await fetch("/api/platform/auth/csrf", { credentials: "same-origin", cache: "no-store", signal: options.signal ?? AbortSignal.timeout(15000) });
    if (!verification.ok) throw new ApiError(verification.status, "Request verification unavailable");
    const { csrf_token } = await verification.json();
    headers.set("X-CSRF-Token", csrf_token);
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`/api/platform/${path}`, { method, headers, credentials: "same-origin", cache: "no-store", body: method === "GET" ? undefined : JSON.stringify(options.data ?? {}), signal: options.signal ?? AbortSignal.timeout(15000) });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, typeof data.detail === "string" ? data.detail : "Request failed");
  return data as T;
}
export type Role = "TENANT_ADMIN" | "INTEGRATION_ENGINEER" | "OPERATIONS_USER";
export type Tenant = { id: string; name: string; slug: string; role: Role };
export type Me = { user: { id: string; display_name: string; email: string }; tenants: Tenant[]; active_tenant_id: string | null; permissions: string[] };
export type Member = { id: string; user_id: string; display_name: string; email: string; role: Role; status: "ACTIVE" | "INACTIVE" };
export type Invitation = { id: string; email: string; role: Role; status: string; expires_at: string };
export type Audit = { id: string; action: string; actor_user_id: string | null; target_type: string; target_id: string | null; metadata: Record<string, string>; created_at: string };
export type Page<T> = { items: T[]; total: number; offset: number; limit: number };
