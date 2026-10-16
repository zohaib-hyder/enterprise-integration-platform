import { NextRequest, NextResponse } from "next/server";
export const dynamic = "force-dynamic";
const uuid = "[0-9a-fA-F-]{36}";
const routes: Record<string, RegExp[]> = {
  GET: [/^ai\/(?:configuration|analyses|recommendations|review-queue|metrics|evaluation-runs)$/, new RegExp(`^ai/analyses/${uuid}(?:/evidence)?$`), new RegExp(`^ai/recommendations/${uuid}/reviews$`), new RegExp(`^ai/evaluation-runs/${uuid}/results$`), /^operations\/(?:overview|health|incidents|assignees|approvals|reconciliation-runs|runbooks)$/, new RegExp(`^operations/timeline/${uuid}$`), new RegExp(`^operations/incidents/${uuid}(?:/(?:activity|evidence))?$`), /^operations\/recovery\/requests$/, new RegExp(`^operations/recovery/requests/${uuid}(?:/activity)?$`), new RegExp(`^operations/reconciliation-runs/${uuid}/findings$`), /^operations\/runbooks\/[a-z-]{1,50}$/, /^notifications\/(?:templates|rules|intents|deliveries|metrics)$/, new RegExp(`^notifications/(?:templates|rules)/${uuid}/versions$`), new RegExp(`^notifications/intents/${uuid}$`), new RegExp(`^notifications/deliveries/${uuid}(?:/(?:attempts|receipts))?$`), /^calendar\/(?:events|requests|availability)$/, new RegExp(`^calendar/events/${uuid}$`), new RegExp(`^calendar/requests/${uuid}(?:/history)?$`),/^sync\/(?:configurations|executions|operations|conflicts|reconciliation-runs|status)$/, new RegExp(`^sync/configurations/${uuid}(?:/versions)?$`), new RegExp(`^sync/(?:plans|executions|operations)/${uuid}$`), new RegExp(`^sync/conflicts/${uuid}(?:/resolutions)?$`), new RegExp(`^sync/reconciliation-runs/${uuid}(?:/findings)?$`),/^operations\/metrics$/, /^alerts$/, new RegExp(`^alerts/${uuid}(?:/history)?$`), new RegExp(`^connections/${uuid}/(?:reliability|health-details|rate-limits|circuit(?:/history)?|credentials(?:/refresh-history)?|failure-injection)$`), /^workflow-schema$/, /^workflows$/, new RegExp(`^workflows/${uuid}(?:/versions)?$`), /^executions$/, new RegExp(`^executions/${uuid}(?:/(history|retries))?$`), /^dead-letters$/, /^worker-status$/, /^canonical-schemas$/, /^mapping-templates$/, /^mappings$/, new RegExp(`^mappings/${uuid}(?:/versions)?$`), /^canonical-records$/, new RegExp(`^canonical-records/${uuid}$`), /^external-identities$/, /^field-authority$/, /^transformation-attempts$/, new RegExp(`^transformation-attempts/${uuid}$`), /^invalid-records$/, new RegExp(`^invalid-records/${uuid}$`), /^transformation-conflicts$/, /^events$/, new RegExp(`^events/${uuid}(?:/receipts)?$`), /^webhook-endpoints$/, new RegExp(`^webhook-endpoints/${uuid}(?:/(receipts|status))?$`), /^connector-definitions$/, /^connections$/, new RegExp(`^connections/${uuid}(?:/(operations|records))?$`), /^auth\/(csrf|me)$/, /^tenants$/, /^tenant$/, new RegExp(`^tenants/${uuid}/(members|invitations|audit)$`)],
  POST: [/^ai\/(?:analyses|evaluation-runs)$/, new RegExp(`^ai/recommendations/${uuid}/(?:validate|review)$`), /^operations\/(?:incidents\/refresh|recovery\/(?:preview|requests)|reconciliation-runs)$/, new RegExp(`^operations/recovery/requests/${uuid}/confirm$`), new RegExp(`^operations/approvals/${uuid}/decide$`), /^notifications\/(?:templates|rules|manual|templates\/preview)$/, new RegExp(`^notifications/templates/${uuid}/(?:versions|activate)$`), new RegExp(`^notifications/deliveries/${uuid}/(?:retry|cancel|review)$`), /^calendar\/events$/, new RegExp(`^calendar/events/${uuid}/(?:reschedule|cancel)$`),/^sync\/configurations$/, new RegExp(`^sync/configurations/${uuid}/(?:versions|activate|preview|reconcile)$`), new RegExp(`^sync/conflicts/${uuid}/resolve$`), new RegExp(`^sync/findings/${uuid}/plan$`),new RegExp(`^alerts/${uuid}/(?:acknowledge|resolve)$`), new RegExp(`^connections/${uuid}/credentials(?:/refresh|/${uuid}/(?:rotate|revoke))?$`), /^workflows(?:\/validate)?$/, new RegExp(`^workflows/${uuid}/(versions|activate)$`), /^workflow-dispatch$/, new RegExp(`^executions/${uuid}/(cancel|replay)$`), /^mappings$/, new RegExp(`^mappings/${uuid}/(versions|activate|preview|normalize)$`), new RegExp(`^events/${uuid}/(preview-transformation|normalize)$`), /^external-identities$/, new RegExp(`^invalid-records/${uuid}/reprocess$`), /^webhook-endpoints$/, new RegExp(`^webhook-endpoints/${uuid}/(rotate-secret|simulate)$`), /^connections$/, new RegExp(`^connections/${uuid}/(health|execute)$`), /^auth\/(login|logout)$/, new RegExp(`^auth/sessions/${uuid}/revoke$`), /^tenant\/switch$/, /^invitations\/accept$/, new RegExp(`^tenants/${uuid}/invitations$`)],
  PUT: [/^ai\/configuration$/, new RegExp(`^connections/${uuid}/(?:reliability|rate-limits|failure-injection)$`)],
  PATCH: [new RegExp(`^operations/incidents/${uuid}$`), new RegExp(`^notifications/rules/${uuid}$`),new RegExp(`^webhook-endpoints/${uuid}$`), new RegExp(`^connections/${uuid}$`), /^tenant$/, new RegExp(`^tenants/${uuid}/members/${uuid}$`)],
  DELETE: [new RegExp(`^tenants/${uuid}/invitations/${uuid}$`)],
};
async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const pathname = path.join("/");
  if (!routes[request.method]?.some(pattern => pattern.test(pathname))) return NextResponse.json({ detail: "Route unavailable" }, { status: 404 });
  const origins = (process.env.FRONTEND_ORIGINS ?? (process.env.NODE_ENV === "production" ? "" : "http://localhost:3000,http://127.0.0.1:3000")).split(",").filter(Boolean);
  // Local production builds also need explicit .env.local origins. No Host-derived defaults.
  if (request.method !== "GET" && (!origins.includes(request.headers.get("origin") ?? "") || request.headers.get("sec-fetch-site") === "cross-site")) return NextResponse.json({ detail: "Request verification failed" }, { status: 403 });
  const query = request.nextUrl.searchParams;
  if ([...query.entries()].some(([key, value]) => {
    if (["notifications/intents","notifications/deliveries"].includes(pathname) && key === "channel") return !["SLACK","EMAIL",...(pathname === "notifications/deliveries" ? ["CALENDAR"] : [])].includes(value);
    if (pathname === "notifications/deliveries" && key === "status") return !["PENDING","RUNNING","RETRY_SCHEDULED","SUCCEEDED","FAILED","CANCELLED","UNKNOWN"].includes(value);
    if (pathname === "calendar/availability") {
      if (key === "connection_id") return !new RegExp(`^${uuid}$`).test(value);
      if (key === "calendar") return !["interviews","recruiting"].includes(value);
      if (["starts_at","ends_at"].includes(key)) return value.length > 40 || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value));
    }
    if (["operations/overview","operations/health"].includes(pathname) && key === "hours") return !["1","24","168","720"].includes(value);
    if (pathname === "operations/incidents" && key === "status") return !["OPEN","INVESTIGATING","MITIGATED","RESOLVED","REOPENED"].includes(value);
    if (pathname === "operations/incidents" && key === "severity") return !["INFO","WARNING","CRITICAL"].includes(value);
    if (["offset", "limit"].includes(key)) return !/^\d{1,6}$/.test(value);
    if (pathname === "operations/metrics" && key === "hours") return !/^\d{1,3}$/.test(value) || Number(value) < 1 || Number(value) > 168;
    if (pathname === "alerts" && key === "status") return !["OPEN", "ACKNOWLEDGED", "RESOLVED"].includes(value);
    if (pathname === "alerts" && key === "severity") return !["INFO", "WARNING", "CRITICAL"].includes(value);
    if (pathname === "alerts" && key === "category") return !["CONNECTION_UNHEALTHY","CREDENTIAL_EXPIRED","REFRESH_FAILED","RATE_LIMIT_EXCEEDED","CIRCUIT_OPEN","WORKFLOW_DEAD_LETTERED","WORKER_RECOVERY","REPEATED_CONNECTOR_FAILURE","REPEATED_DELIVERY_FAILURE","TERMINAL_NOTIFICATION_FAILURE","UNKNOWN_DELIVERY_OUTCOME","CALENDAR_CONFLICT"].includes(value);
    if (new RegExp(`^sync/conflicts/${uuid}$`).test(pathname) && key === "destination_external_id") return value.length > 100 || !/^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$/.test(value);
    if (pathname === "sync/executions" && key === "configuration_id") return !new RegExp(`^${uuid}$`).test(value);
    if (pathname === "sync/operations" && key === "execution_id") return !new RegExp(`^${uuid}$`).test(value);
    if (pathname === "sync/executions" && key === "state") return !["PLANNED","SUCCEEDED","HELD","SKIPPED"].includes(value);
    if (pathname === "sync/conflicts" && key === "status") return !["OPEN","RESOLVED"].includes(value);
    if (pathname === "executions" && key === "workflow_id") return !new RegExp(`^${uuid}$`).test(value);
    if (pathname === "executions" && key === "state") return !["PENDING", "RUNNING", "WAITING", "RETRY_SCHEDULED", "SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"].includes(value);
    const canonicalQueries: Record<string, string[]> = { mappings: ["connection_id"], "canonical-records": ["entity_type"], "external-identities": ["connection_id", "canonical_id", "source_kind", "external_id"], "field-authority": ["entity_type"], "transformation-attempts": ["outcome"], "invalid-records": ["status"] };
    if (canonicalQueries[pathname]?.includes(key)) {
      if (["connection_id", "canonical_id"].includes(key)) return !new RegExp(`^${uuid}$`).test(value);
      if (key === "entity_type") return !["client", "contact", "job_requisition", "candidate", "application", "activity"].includes(value);
      if (key === "source_kind") return !["contact", "candidate", "requisition"].includes(value);
      if (key === "external_id") return value.length > 100 || !/^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$/.test(value);
      if (key === "outcome") return !["SUCCEEDED", "NO_CHANGE", "QUARANTINED", "CONFLICT", "MANUAL_REVIEW"].includes(value);
      if (key === "status") return !["OPEN", "RESOLVED"].includes(value);
    }
    if (pathname !== "events" || !["source", "event_type", "status", "since", "until"].includes(key)) return true;
    if (value.length > 64) return true;
    if (key === "source") return !["synthetic_crm", "synthetic_ats"].includes(value);
    if (key === "event_type") return !["job.created", "job.updated", "candidate.created", "candidate.updated"].includes(value);
    if (key === "status") return !["ACCEPTED", "PENDING"].includes(value);
    return !/^\d{4}-\d{2}-\d{2}T/.test(value);
  })) return NextResponse.json({ detail: "Invalid request" }, { status: 400 });
  try {
    const base = new URL(process.env.BACKEND_URL ?? "http://127.0.0.1:8000");
    if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash) throw new Error("Invalid backend configuration");
    const url = new URL(`/api/v1/${pathname}`, base);
    url.search = query.toString();
    const headers = new Headers({ accept: "application/json" });
    const cookies = request.headers.get("cookie")?.split(";").filter(cookie => /^\s*(?:__Host-)?platform_(?:session|csrf)=/.test(cookie)).join(";");
    if (cookies) headers.set("cookie", cookies);
    for (const key of ["origin", "x-csrf-token", "sec-fetch-site", "x-correlation-id"]) {
      const value = request.headers.get(key);
      if (value) headers.set(key, value);
    }
    let body: Uint8Array | undefined;
    if (request.method !== "GET" && request.body) {
      if (!request.headers.get("content-type")?.startsWith("application/json")) return NextResponse.json({ detail: "JSON required" }, { status: 415 });
      const reader = request.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 16384) { await reader.cancel(); return NextResponse.json({ detail: "Request too large" }, { status: 413 }); }
        chunks.push(value);
      }
      body = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.length; }
      headers.set("content-type", "application/json");
    }
    const upstream = await fetch(url, { method: request.method, headers, body: body as BodyInit | undefined, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(15000) });
    const response = new NextResponse(await upstream.text(), { status: upstream.status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
    for (const cookie of upstream.headers.getSetCookie()) {
      if (/^(?:__Host-)?platform_(session|csrf)=/.test(cookie)) response.headers.append("set-cookie", cookie);
    }
    for (const name of ["retry-after", "x-correlation-id", "x-operation-id"]) {
      const value = upstream.headers.get(name);
      if (value) response.headers.set(name, value);
    }
    return response;
  } catch { return NextResponse.json({ detail: "Platform temporarily unavailable" }, { status: 503 }); }
}
export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE };
