// @vitest-environment node
import { afterEach, expect, test, vi } from "vitest";
import { NextRequest } from "next/server";
import { POST, GET } from "../app/api/platform/[...path]/route";
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
const context = (path: string) => ({ params: Promise.resolve({ path: path.split("/") }) });
test("proxy rejects forged Origin before contacting backend", async () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  const request = new NextRequest("http://localhost:3000/api/platform/auth/login", { method: "POST", headers: { origin: "https://attacker.example.test", "content-type": "application/json" }, body: "{}" });
  expect((await POST(request, context("auth/login"))).status).toBe(403);
  expect(fetch).not.toHaveBeenCalled();
});
test("proxy allowlists routes and query keys", async () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/admin"), context("admin"))).status).toBe(404);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/auth/me?token=secret"), context("auth/me"))).status).toBe(400);
  expect(fetch).not.toHaveBeenCalled();
});
test("proxy forwards verification headers and only platform cookies", async () => {
  vi.stubEnv("FRONTEND_ORIGINS", "http://localhost:3000");
  const headers = new Headers({ "content-type": "application/json" });
  headers.append("set-cookie", "platform_session=opaque; Path=/; HttpOnly; SameSite=strict");
  headers.append("set-cookie", "unrelated=value; Path=/");
  const fetch = vi.fn().mockResolvedValue(new Response('{"message":"ok"}', { headers })); vi.stubGlobal("fetch", fetch);
  const request = new NextRequest("http://localhost:3000/api/platform/auth/login", { method: "POST", headers: { origin: "http://localhost:3000", "x-csrf-token": "verification", cookie: "platform_csrf=verification; unrelated=private", "content-type": "application/json" }, body: "{}" });
  const response = await POST(request, context("auth/login"));
  expect(response.status).toBe(200);
  const forwarded = fetch.mock.calls[0][1].headers as Headers;
  expect(forwarded.get("origin")).toBe("http://localhost:3000");
  expect(forwarded.get("x-csrf-token")).toBe("verification");
  expect(forwarded.get("cookie")).toBe("platform_csrf=verification");
  expect(response.headers.get("set-cookie")).toContain("HttpOnly");
  expect(response.headers.get("set-cookie")).not.toContain("unrelated");
});
test("proxy bounds request body size", async () => {
  vi.stubEnv("FRONTEND_ORIGINS", "http://localhost:3000"); vi.stubGlobal("fetch", vi.fn());
  const request = new NextRequest("http://localhost:3000/api/platform/auth/login", { method: "POST", headers: { origin: "http://localhost:3000", "content-type": "application/json" }, body: JSON.stringify({ data: "a".repeat(16384) }) });
  expect((await POST(request, context("auth/login"))).status).toBe(413);
});

test("event filters are allowed only on inbox and public ingress is never browser-proxied", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{"items":[]}', { headers: { "content-type": "application/json" } })); vi.stubGlobal("fetch", fetch);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/events?source=synthetic_crm&event_type=job.created&since=2026-10-13T00%3A00%3A00Z"), context("events"))).status).toBe(200);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/connections?source=synthetic_crm"), context("connections"))).status).toBe(400);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/events?tenant_id=forged"), context("events"))).status).toBe(400);
  const request = new NextRequest("http://localhost:3000/api/platform/webhooks/ingest/" + "A".repeat(43), { method: "POST" });
  expect((await POST(request, context("webhooks/ingest/" + "A".repeat(43)))).status).toBe(404);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("mapping APIs reject arbitrary tenant, code and source selectors", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{"items":[]}', { headers: { "content-type": "application/json" } })); vi.stubGlobal("fetch", fetch);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/canonical-records?entity_type=candidate"), context("canonical-records"))).status).toBe(200);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/mappings?tenant_id=forged"), context("mappings"))).status).toBe(400);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/external-identities?source_kind=arbitrary"), context("external-identities"))).status).toBe(400);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/canonical-records?entity_type=arbitrary"), context("canonical-records"))).status).toBe(400);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/mappings/eval"), context("mappings/eval"))).status).toBe(404);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("workflow BFF allows explicit execution filters and refuses tenant/lease manipulation", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{"items":[]}', { headers: { "content-type": "application/json" } })); vi.stubGlobal("fetch", fetch);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/executions?state=RETRY_SCHEDULED"), context("executions"))).status).toBe(200);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/executions?tenant_id=forged"), context("executions"))).status).toBe(400);
  expect((await GET(new NextRequest("http://localhost:3000/api/platform/executions?state=ARBITRARY"), context("executions"))).status).toBe(400);
  expect((await POST(new NextRequest("http://localhost:3000/api/platform/executions/00000000-0000-0000-0000-000000000001/claim", { method: "POST" }), context("executions/00000000-0000-0000-0000-000000000001/claim"))).status).toBe(404);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('reliability BFF exposes only exact routes filters and safe response metadata',async () => {
  vi.stubEnv('FRONTEND_ORIGINS','http://localhost:3000');
  const fetch=vi.fn().mockResolvedValue(new Response('{"error":{"category":"RATE_LIMITED"}}',{status:429,headers:{'content-type':'application/json','retry-after':'2','x-operation-id':'safe-operation','authorization':'private'}}));vi.stubGlobal('fetch',fetch);
  const {PUT}=await import('../app/api/platform/[...path]/route');
  const uuid='00000000-0000-0000-0000-000000000001';
  const response=await PUT(new NextRequest(`http://localhost:3000/api/platform/connections/${uuid}/reliability`,{method:'PUT',headers:{origin:'http://localhost:3000','content-type':'application/json'},body:'{}'}),context(`connections/${uuid}/reliability`));
  expect(response.status).toBe(429);expect(response.headers.get('retry-after')).toBe('2');expect(response.headers.get('x-operation-id')).toBe('safe-operation');expect(response.headers.has('authorization')).toBe(false);
  expect((await GET(new NextRequest('http://localhost:3000/api/platform/operations/metrics?tenant_id=forged'),context('operations/metrics'))).status).toBe(400);
  expect((await GET(new NextRequest('http://localhost:3000/api/platform/alerts?severity=ARBITRARY'),context('alerts'))).status).toBe(400);
  expect((await POST(new NextRequest(`http://localhost:3000/api/platform/connections/${uuid}/credentials/reveal`,{method:'POST'}),context(`connections/${uuid}/credentials/reveal`))).status).toBe(404);
});
test('synchronization proxy exposes narrow routes and denies tenant injection or arbitrary writes',async()=>{
 const fetch=vi.fn().mockResolvedValue(new Response('{"items":[]}',{headers:{'content-type':'application/json'}}));vi.stubGlobal('fetch',fetch);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/sync/configurations'),context('sync/configurations'))).status).toBe(200);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/sync/executions?tenant_id=foreign'),context('sync/executions'))).status).toBe(400);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/sync/admin/secrets'),context('sync/admin/secrets'))).status).toBe(404);
 expect((await POST(new NextRequest('http://localhost:3000/api/platform/sync/apply',{method:'POST'}),context('sync/apply'))).status).toBe(404);
 expect(fetch).toHaveBeenCalledTimes(1);
});

test('notification proxy allows bounded filters and only explicit recovery actions',async()=>{
 vi.stubEnv('FRONTEND_ORIGINS','http://localhost:3000');
 const fetch=vi.fn().mockImplementation(async()=>new Response('{"items":[]}',{headers:{'content-type':'application/json'}}));vi.stubGlobal('fetch',fetch);
 const id='00000000-0000-0000-0000-000000000001';
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/notifications/deliveries?channel=SLACK&status=UNKNOWN'),context('notifications/deliveries'))).status).toBe(200);
 expect((await POST(new NextRequest(`http://localhost:3000/api/platform/notifications/deliveries/${id}/review`,{method:'POST',headers:{origin:'http://localhost:3000','content-type':'application/json'},body:'{}'}),context(`notifications/deliveries/${id}/review`))).status).toBe(200);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/notifications/deliveries?tenant_id=forged'),context('notifications/deliveries'))).status).toBe(400);
 expect((await POST(new NextRequest(`http://localhost:3000/api/platform/notifications/deliveries/${id}/force-resend`,{method:'POST'}),context(`notifications/deliveries/${id}/force-resend`))).status).toBe(404);
 expect(fetch).toHaveBeenCalledTimes(2);
});

test('calendar proxy requires aware interval filters and blocks tenant injection',async()=>{
 const fetch=vi.fn().mockResolvedValue(new Response('{"available":true}',{headers:{'content-type':'application/json'}}));vi.stubGlobal('fetch',fetch);
 const query='connection_id=00000000-0000-0000-0000-000000000001&calendar=interviews&starts_at=2030-01-01T10:00:00Z&ends_at=2030-01-01T11:00:00Z';
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/calendar/availability?'+query),context('calendar/availability'))).status).toBe(200);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/calendar/availability?starts_at=2030-01-01'),context('calendar/availability'))).status).toBe(400);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/calendar/events?tenant_id=forged'),context('calendar/events'))).status).toBe(400);
 expect((await POST(new NextRequest('http://localhost:3000/api/platform/calendar/provider-admin',{method:'POST'}),context('calendar/provider-admin'))).status).toBe(404);
 expect(fetch).toHaveBeenCalledTimes(1);
});

test('operations proxy exposes bounded views and exact confirmation routes',async()=>{
 vi.stubEnv('FRONTEND_ORIGINS','http://localhost:3000');
 const fetch=vi.fn().mockImplementation(async()=>new Response('{}',{headers:{'content-type':'application/json'}}));vi.stubGlobal('fetch',fetch);
 const id='00000000-0000-0000-0000-000000000001';
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/operations/overview?hours=720'),context('operations/overview'))).status).toBe(200);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/operations/incidents?status=REOPENED&severity=CRITICAL'),context('operations/incidents'))).status).toBe(200);
 expect((await POST(new NextRequest(`http://localhost:3000/api/platform/operations/recovery/requests/${id}/confirm`,{method:'POST',headers:{origin:'http://localhost:3000','content-type':'application/json'},body:'{}'}),context(`operations/recovery/requests/${id}/confirm`))).status).toBe(200);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/operations/overview?hours=999'),context('operations/overview'))).status).toBe(400);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/operations/incidents?tenant_id=forged'),context('operations/incidents'))).status).toBe(400);
 expect(fetch).toHaveBeenCalledTimes(3);
});

test('operations proxy blocks arbitrary repair and approval overrides',async()=>{
 const fetch=vi.fn();vi.stubGlobal('fetch',fetch);const id='00000000-0000-0000-0000-000000000001';
 expect((await POST(new NextRequest(`http://localhost:3000/api/platform/operations/approvals/${id}/force`,{method:'POST'}),context(`operations/approvals/${id}/force`))).status).toBe(404);
 expect((await POST(new NextRequest('http://localhost:3000/api/platform/operations/repair-all',{method:'POST'}),context('operations/repair-all'))).status).toBe(404);
 expect((await GET(new NextRequest(`http://localhost:3000/api/platform/operations/timeline/${id}?tenant_id=forged`),context(`operations/timeline/${id}`))).status).toBe(400);
 expect(fetch).not.toHaveBeenCalled();
});

test('AI BFF denies execution and tenant query injection',async()=>{
 const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
 expect((await GET(new NextRequest('http://localhost:3000/api/platform/ai/analyses?tenant_id=foreign'),context('ai/analyses'))).status).toBe(400);
 expect((await POST(new NextRequest('http://localhost:3000/api/platform/ai/recommendations/00000000-0000-0000-0000-000000000001/execute',{method:'POST'}),context('ai/recommendations/00000000-0000-0000-0000-000000000001/execute'))).status).toBe(404);
 expect(fetch).not.toHaveBeenCalled();
});
test('AI analysis requires same origin before forwarding',async()=>{
 const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
 expect((await POST(new NextRequest('http://localhost:3000/api/platform/ai/analyses',{method:'POST',headers:{origin:'https://evil.test','content-type':'application/json'},body:'{}'}),context('ai/analyses'))).status).toBe(403);
 expect(fetch).not.toHaveBeenCalled();
});
