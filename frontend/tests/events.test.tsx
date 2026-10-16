import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import { AuthGuard, AuthProvider } from "../components/auth-provider";
import Events from "../components/events";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("../lib/api", async importOriginal => { const actual = await importOriginal<typeof import("../lib/api")>(); return { ...actual, api: vi.fn() }; });
const me = { user: { id: "user-a", display_name: "Synthetic Engineer", email: "engineer@example.test" }, tenants: [{ id: "tenant-a", name: "Synthetic Organization", role: "INTEGRATION_ENGINEER" }], active_tenant_id: "tenant-a", permissions: ["operations:read", "integrations:manage"] };
const hook = { id: "hook-a", connection_id: "connection-a", source: "synthetic_crm", status: "ACTIVE", public_id: "A".repeat(43), signature_version: "hmac-sha256-v1", timestamp_tolerance: 300, current_secret_version: 1, previous_secret_valid_until: null };
const event = { id: "event-a", endpoint_id: "hook-a", connection_id: "connection-a", source: "synthetic_crm", provider_event_id: "synthetic-event-1", event_type: "job.created", schema_version: 1, ingestion_status: "ACCEPTED", processing_status: "PENDING", received_at: "2026-10-13T01:00:00Z", occurred_at: "2026-10-13T00:00:00Z", duplicate_count: 1, correlation_id: "correlation-a", payload: null };
const receipt = { id: "receipt-a", endpoint_id: "hook-a", event_id: "event-a", outcome: "DUPLICATE", error_category: null, duration_ms: 8, correlation_id: "correlation-a", received_at: "2026-10-13T01:00:00Z" };
const page = (items: unknown[]) => ({ items, total: items.length, offset: 0, limit: 25 });
function defaultApi(path: string): unknown {
  if (path === "auth/me") return me;
  if (path.startsWith("events?")) return page([event]);
  if (path === "events/event-a") return { ...event, payload: { data: { email: "[REDACTED]", title: "Synthetic Job" } } };
  if (path.includes("/receipts?")) return page([receipt]);
  if (path.startsWith("webhook-endpoints?")) return page([hook]);
  if (path === "webhook-endpoints/hook-a") return hook;
  if (path.endsWith("/status")) return { status: "ACTIVE", connection_status: "ENABLED", secret_available: true, receipt_counts: { ACCEPTED: 1, DUPLICATE: 1 }, error_counts: {}, last_received_at: null };
  if (path.startsWith("connections?")) return page([{ id: "connection-a", name: "Synthetic CRM", connector_type: "synthetic_crm", status: "ENABLED" }]);
  if (path === "webhook-endpoints" || path.endsWith("/rotate-secret")) return { endpoint: hook, signing_secret: "one-time-synthetic-test-secret", secret_version: 1 };
  if (path.endsWith("/simulate")) return { synthetic: true, delivery_mode: "local_in_process", results: [{ http_status: 202, status: "accepted" }] };
  return {};
}
beforeEach(() => { vi.mocked(api).mockReset(); vi.mocked(api).mockImplementation(async path => defaultApi(path)); });
afterEach(() => cleanup());
function view() { return render(<AuthProvider><AuthGuard><Events /></AuthGuard></AuthProvider>); }
async function selectEndpoint() { fireEvent.click(await screen.findByRole("button", { name: /synthetic_crm · ACTIVE/ })); await screen.findByRole("region", { name: "Endpoint details" }); }

test("event list shows persisted state and duplicate indicator", async () => {
  view(); await screen.findByRole("button", { name: "synthetic-event-1" });
  expect(screen.getByText("ACCEPTED")).toBeInTheDocument();
  expect(screen.getByText("PENDING")).toBeInTheDocument();
  expect(screen.getByText("1 persisted events")).toBeInTheDocument();
});
test("source type status and date filters use bounded query fields", async () => {
  view(); await screen.findByRole("button", { name: "synthetic-event-1" });
  fireEvent.change(screen.getByLabelText("Source"), { target: { value: "synthetic_ats" } });
  fireEvent.change(screen.getByLabelText("Event type"), { target: { value: "candidate.created" } });
  fireEvent.change(screen.getByLabelText("Status"), { target: { value: "PENDING" } });
  fireEvent.change(screen.getByLabelText("Received after"), { target: { value: "2026-10-13T00:00" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith("events?") && path.includes("source=synthetic_ats") && path.includes("event_type=candidate.created") && path.includes("status=PENDING") && path.includes("since="))).toBe(true));
});
test("details expose only redacted payload and actual delivery receipts", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: "synthetic-event-1" }));
  const region = await screen.findByRole("region", { name: "Event details" });
  expect(within(region).getByText(/REDACTED/)).toBeInTheDocument();
  expect(within(region).getByText("DUPLICATE")).toBeInTheDocument();
  fireEvent.click(within(region).getByRole("button", { name: "Close details" }));
  expect(screen.queryByRole("region", { name: "Event details" })).not.toBeInTheDocument();
});
test("endpoint creation has masked one-time disclosure that is cleared", async () => {
  view(); await waitFor(() => expect(screen.getByRole("button", { name: "Create endpoint" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "Create endpoint" }));
  fireEvent.change(screen.getByLabelText("Authorized connection"), { target: { value: "connection-a" } });
  fireEvent.click(screen.getByRole("button", { name: "Create signed endpoint" }));
  const dialog = await screen.findByRole("dialog", { name: "One-time signing secret" });
  const secret = within(dialog).getByLabelText("Signing secret") as HTMLInputElement;
  expect(secret.type).toBe("password"); expect(secret.value).toBe("one-time-synthetic-test-secret");
  expect(api).toHaveBeenCalledWith("webhook-endpoints", { method: "POST", data: { connection_id: "connection-a", source: "synthetic_crm" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Saved; close disclosure" }));
  expect(secret.value).toBe(""); expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
test("secret rotation uses bounded grace and separate disclosure", async () => {
  view(); await selectEndpoint();
  fireEvent.change(screen.getByLabelText("Rotation grace (seconds)"), { target: { value: "60" } });
  fireEvent.click(screen.getByRole("button", { name: "Rotate signing secret" }));
  await screen.findByRole("dialog", { name: "One-time signing secret" });
  expect(api).toHaveBeenCalledWith("webhook-endpoints/hook-a/rotate-secret", { method: "POST", data: { grace_seconds: 60 } });
});
test("synthetic delivery requires explicit authorized action and reports local mode", async () => {
  view(); await selectEndpoint();
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith("/simulate"))).toBe(false);
  fireEvent.change(screen.getByLabelText("Provider event ID"), { target: { value: "explicit-demo-1" } });
  fireEvent.change(screen.getByLabelText("Delivery scenario"), { target: { value: "duplicate" } });
  fireEvent.click(screen.getByRole("button", { name: "Deliver synthetic event" }));
  await screen.findByText(/local_in_process/);
  expect(api).toHaveBeenCalledWith("webhook-endpoints/hook-a/simulate", { method: "POST", data: { event_type: "job.created", event_id: "explicit-demo-1", scenario: "duplicate" } });
});
test("operations role reads history but cannot manage credentials", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, permissions: ["operations:read"] } : defaultApi(path));
  view(); await selectEndpoint();
  expect(screen.queryByRole("button", { name: "Create endpoint" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Rotate signing secret" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Deliver synthetic event" })).not.toBeInTheDocument();
  expect(screen.getByText("DUPLICATE")).toBeInTheDocument();
});
test("no active tenant or permission shows denial without inbox request", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, active_tenant_id: null, permissions: [] } : defaultApi(path));
  view(); await screen.findByRole("alert");
  expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith("events?"))).toBe(false);
});
test("empty inbox and endpoints do not fabricate event activity", async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith("events?") || path.startsWith("webhook-endpoints?") ? page([]) : defaultApi(path));
  view(); await screen.findByText(/No incoming events/); expect(screen.getByText("No webhook endpoints configured.")).toBeInTheDocument();
});
test("loading and failed inbox have explicit states and retry", async () => {
  let reject: (value: unknown) => void = () => {};
  const pending = new Promise((_, rejection) => { reject = rejection; }); void pending.catch(() => {});
  vi.mocked(api).mockImplementation(async path => path.startsWith("events?") ? pending : defaultApi(path));
  view(); await screen.findByText("Loading events…"); reject(new ApiError(503, "Inbox unavailable"));
  await screen.findByRole("alert"); expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
});
test("endpoint can be disabled without exposing any secret", async () => {
  view(); await selectEndpoint(); fireEvent.click(screen.getByRole("button", { name: "Disable endpoint" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("webhook-endpoints/hook-a", { method: "PATCH", data: { enabled: false } }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
