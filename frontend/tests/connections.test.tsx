import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import { type Capability, type Connection } from "../lib/integrations";
import { AuthGuard, AuthProvider } from "../components/auth-provider";
import Connections from "../components/connections";
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("../lib/api", async importOriginal => { const actual = await importOriginal<typeof import("../lib/api")>(); return { ...actual, api: vi.fn() }; });
const me = { user: { id: "synthetic-user", display_name: "Synthetic Engineer", email: "engineer@example.test" }, tenants: [{ id: "tenant-a", name: "Synthetic Organization", role: "INTEGRATION_ENGINEER" }], active_tenant_id: "tenant-a", permissions: ["tenant:read", "integrations:manage"] };
const connection: Connection = { id: "connection-a", connector_type: "synthetic_crm", name: "Synthetic CRM", status: "ENABLED", public_config: { schema_version: 1, workspace_label: "Synthetic Sales" }, health_status: "UNKNOWN", last_checked_at: null, synthetic: true };
const catalog: Capability[] = [{ connector_type: "synthetic_crm", name: "HubSpot-style CRM simulator", synthetic: true, enabled: true, limitation: null, config_schema_version: 1, config_schema: {}, operations: [{ name: "contact.list", schema_version: 1, mutating: false, payload_schema: {} }, { name: "contact.upsert", schema_version: 1, mutating: true, payload_schema: {} }] }, { connector_type: "generic_rest", name: "Generic REST (disabled)", synthetic: false, enabled: false, limitation: "No outbound execution", config_schema_version: 1, config_schema: {}, operations: [] }];
const empty = { items: [], total: 0, offset: 0, limit: 25 };
function defaultApi(path: string): unknown {
  if (path === "auth/me") return me;
  if (path === "connector-definitions") return catalog;
  if (path === "connections/connection-a/health") return { status: "HEALTHY", message: "Local simulator" };
  if (path === "connections/connection-a") return connection;
  if (path.endsWith("/execute")) return { schema_version: 1, operation: "contact.list", synthetic: true, data: empty };
  if (path.includes("/records?")) return { ...empty, total: 1, items: [{ id: "record-a", kind: "contact", external_id: "demo-contact-1", data: { name: "Synthetic Client", email: "client@example.test", company: "Fictional Company" }, synthetic: true }] };
  if (path.startsWith("connections?")) return { ...empty, items: [connection], total: 1 };
  return connection;
}
beforeEach(() => { vi.mocked(api).mockReset(); vi.mocked(api).mockImplementation(async path => defaultApi(path)); });
afterEach(() => cleanup());
function view() { return render(<AuthProvider><AuthGuard><Connections /></AuthGuard></AuthProvider>); }

test("connections listing and catalog expose only synthetic status", async () => {
  view();
  await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ });
  expect(screen.getByText("HubSpot-style CRM simulator")).toBeInTheDocument();
  expect(screen.getByText("Generic REST (disabled)")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Add connection" })).toBeEnabled();
});
test("connection configuration saves allowlisted fields", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ }));
  fireEvent.change(screen.getByLabelText("Workspace label"), { target: { value: "Updated Synthetic Workspace" } });
  fireEvent.click(screen.getByRole("button", { name: "Save configuration" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("connections/connection-a", { method: "PATCH", data: { name: "Synthetic CRM", config: { schema_version: 1, workspace_label: "Updated Synthetic Workspace" } } }));
  await screen.findByText("Configuration saved");
});
test("test connection uses protected health action", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ }));
  fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
  await screen.findByText("HEALTHY: Local simulator");
  expect(api).toHaveBeenCalledWith("connections/connection-a/health", { method: "POST" });
});
test("operations users see metadata without management or records", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, permissions: ["tenant:read", "operations:read"] } : defaultApi(path));
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ }));
  expect(screen.queryByRole("button", { name: "Add connection" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Test connection" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Execute simulator operation" })).not.toBeInTheDocument();
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes("/records"))).toBe(false);
});
test("synthetic record browser displays validated records", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ }));
  await screen.findByText("Synthetic Client");
  expect(screen.getByText("client@example.test")).toBeInTheDocument();
  expect(screen.getByText("Fictional Company")).toBeInTheDocument();
  expect(screen.getByText("contact.list · v1")).toBeInTheDocument();
});
test("empty state supports first connection setup", async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith("connections?") ? empty : defaultApi(path));
  view(); await screen.findByText(/No connections yet/);
  fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
  const dialog = screen.getByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("Connection name"), { target: { value: "New Synthetic CRM" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create connection" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("connections", { method: "POST", data: { name: "New Synthetic CRM", connector_type: "synthetic_crm", config: { schema_version: 1, workspace_label: "Synthetic business workspace" }, enabled: true } }));
});
test("failed loads and loading state are explicit", async () => {
  let reject: (value: unknown) => void = () => {};
  const pending = new Promise((_, rejectPromise) => { reject = rejectPromise; });
  void pending.catch(() => {});
  vi.mocked(api).mockImplementation(async path => path.startsWith("connections?") ? pending : defaultApi(path));
  view(); await screen.findByText("Loading connections…"); reject(new ApiError(503, "Platform unavailable"));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
});
test("explicit operation executes a versioned payload", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ }));
  fireEvent.click(screen.getByRole("button", { name: "Execute simulator operation" }));
  await screen.findByText("Validated synthetic result");
  expect(api).toHaveBeenCalledWith("connections/connection-a/execute", { method: "POST", data: { schema_version: 1, name: "contact.list", payload: { offset: 0, limit: 25 } } });
});
test("disable action uses backend connection state", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic CRM.*ENABLED/ }));
  fireEvent.click(screen.getByRole("button", { name: "Disable connection" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("connections/connection-a", { method: "PATCH", data: { enabled: false } }));
});
test("notification browser never claims an external delivery", async () => {
  const slack: Connection = { ...connection, connector_type: "synthetic_slack", name: "Synthetic Slack", public_config: { allowed_channels: ["recruiting"] } };
  vi.mocked(api).mockImplementation(async path => path.startsWith("connections?") ? { ...empty, items: [slack], total: 1 } : path.includes("/records?") ? { ...empty, items: [{ id: "notice-a", channel: "recruiting", text: "Synthetic local notification", status: "SIMULATED", synthetic: true }], total: 1 } : defaultApi(path));
  view(); fireEvent.click(await screen.findByRole("button", { name: /Synthetic Slack.*ENABLED/ }));
  await screen.findByText("Synthetic local notification");
  expect(screen.getByText("SIMULATED")).toBeInTheDocument();
});
