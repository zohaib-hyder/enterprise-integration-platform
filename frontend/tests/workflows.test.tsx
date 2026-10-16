import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import { AuthGuard, AuthProvider } from "../components/auth-provider";
import WorkflowConsole from "../components/workflows";
import { workflowTemplate } from "../lib/workflows";
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("../lib/api", async importOriginal => ({ ...await importOriginal<typeof import("../lib/api")>(), api: vi.fn() }));
const me = { user: { id: "user", display_name: "Synthetic", email: "engineer@example.test" }, tenants: [{ id: "tenant", name: "Synthetic", role: "INTEGRATION_ENGINEER" }], active_tenant_id: "tenant", permissions: ["operations:read", "integrations:manage", "executions:recover"] };
const definition = workflowTemplate("synthetic_crm", "crm", "mapping-version", false);
const workflow = { id: "workflow", name: "Job observer", status: "ACTIVE", active_version: 1, trigger_configuration: definition.trigger };
const execution = { id: "execution", workflow_id: "workflow", event_id: "event", root_id: "execution", replay_of_id: null, state: "DEAD_LETTERED", error_category: "TEMPORARY_UNAVAILABLE", created_at: "2030-01-15T12:00:00Z", steps: [{ id: "step", step_id: "normalize", step_type: "NORMALIZE_EVENT", state: "SUCCEEDED", attempt_count: 1, checkpoint_saved: true }, { id: "action", step_id: "observe", step_type: "MOCK_CONNECTOR_ACTION", state: "FAILED", attempt_count: 3, checkpoint_saved: false }] };
const page = (items: unknown[]) => ({ items, total: items.length, offset: 0, limit: 25 });
function defaultApi(path: string): unknown {
  if (path === "auth/me") return me;
  if (path === "worker-status") return { active_workers: [], backlog: { DEAD_LETTERED: 1 }, checked_at: "2030-01-15T12:00:00Z" };
  if (path.startsWith("workflows?")) return page([workflow]);
  if (path === "workflows" || path.endsWith("/activate")) return workflow;
  if (path.startsWith("workflows/workflow/versions?")) return page([{ id: "version", version: 1, definition }]);
  if (path.startsWith("connections?")) return page([{ id: "crm", name: "Demo CRM", connector_type: "synthetic_crm", status: "ENABLED" }]);
  if (path.startsWith("mappings?")) return page([{ id: "mapping", connection_id: "crm", source_kind: "requisition", active_version: 1 }]);
  if (path.startsWith("mappings/mapping/versions")) return page([{ id: "mapping-version", version: 1 }]);
  if (path.startsWith("executions?")) return page([execution]);
  if (path.startsWith("dead-letters?")) return page([{ id: "dead", execution_id: "execution", step_id: "observe", category: "TEMPORARY_UNAVAILABLE", attempt_count: 3 }]);
  if (path.startsWith("executions/execution/history")) return page([{ id: "history", step_id: "observe", event_type: "RETRY_SCHEDULED", category: "TEMPORARY_UNAVAILABLE", created_at: "2030-01-15T12:00:00Z" }]);
  if (path.startsWith("executions/execution/retries")) return page([{ id: "retry", attempt: 1, state: "CONSUMED", category: "TIMEOUT", available_at: "2030-01-15T12:00:01Z" }]);
  if (path === "executions/execution" || path.endsWith("/replay")) return execution;
  return {};
}
beforeEach(() => { vi.mocked(api).mockReset(); vi.mocked(api).mockImplementation(async path => defaultApi(path)); });
afterEach(() => cleanup());
function view(mode: "Workflows" | "Executions" | "Failures") { return render(<AuthProvider><AuthGuard><WorkflowConsole mode={mode} /></AuthGuard></AuthProvider>); }

test("workflow listing and worker health use live API data", async () => { view("Workflows"); await screen.findByRole("button", { name: /Job observer/ }); expect(screen.getByText(/0 active worker heartbeats/)).toBeInTheDocument(); });
test("safe template creation pins a mapping and validates before creation", async () => {
  view("Workflows"); await screen.findByRole("button", { name: /Job observer/ });
  fireEvent.change(screen.getByLabelText("Workflow name"), { target: { value: "New observer" } });
  fireEvent.change(screen.getByLabelText("Source connection"), { target: { value: "crm" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Create workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create workflow" }));
  await screen.findByText(/Workflow created inactive/);
  expect(api).toHaveBeenCalledWith("workflows/validate", { method: "POST", data: definition });
  const request = vi.mocked(api).mock.calls.find(([p]) => p === "workflows");
  expect(request?.[1]?.data).toMatchObject({ name: "New observer", definition: { schema_version: 1 } });
});
test("workflow activation requires an explicit control", async () => {
  view("Workflows"); fireEvent.click(await screen.findByRole("button", { name: /Job observer/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Activate version" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("workflows/workflow/activate", { method: "POST", data: { enabled: true, version_id: "version" } }));
});
test("execution timeline exposes checkpoints and persisted retry history", async () => {
  view("Executions"); fireEvent.click(await screen.findByRole("button", { name: /DEAD_LETTERED/ }));
  const details = await screen.findByRole("region", { name: "Execution details" });
  expect(within(details).getByText(/Checkpoint saved/)).toBeInTheDocument();
  expect(within(details).getByText(/RETRY_SCHEDULED/)).toBeInTheDocument();
  expect(within(details).getByText(/CONSUMED/)).toBeInTheDocument();
});
test("failure inspection and replay require confirmation", async () => {
  view("Failures"); fireEvent.click(await screen.findByRole("button", { name: /TEMPORARY_UNAVAILABLE/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Replay execution" }));
  expect(vi.mocked(api).mock.calls.some(([p]) => p.endsWith("/replay"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Confirm replay" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("executions/execution/replay", { method: "POST", data: { confirmed: true } }));
});
test("pending execution can be cancelled", async () => {
  vi.mocked(api).mockImplementation(async path => path === "executions/execution" ? { ...execution, state: "PENDING" } : defaultApi(path));
  view("Executions"); fireEvent.click(await screen.findByRole("button", { name: /DEAD_LETTERED/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Cancel execution" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("executions/execution/cancel", { method: "POST" }));
});
test("operations role cannot create or activate definitions", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, permissions: ["operations:read", "executions:recover"] } : defaultApi(path));
  view("Workflows"); fireEvent.click(await screen.findByRole("button", { name: /Job observer/ }));
  await screen.findByText(/version history/);
  expect(screen.queryByRole("button", { name: "Activate version" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Create workflow" })).not.toBeInTheDocument();
});
test("read-only observer does not receive replay controls", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, permissions: ["operations:read"] } : defaultApi(path));
  view("Executions"); fireEvent.click(await screen.findByRole("button", { name: /DEAD_LETTERED/ }));
  await screen.findByRole("region", { name: "Execution details" });
  expect(screen.queryByRole("button", { name: "Replay execution" })).not.toBeInTheDocument();
});
test("workflow error and empty states are honest", async () => {
  vi.mocked(api).mockImplementation(async path => { if (path.startsWith("workflows?")) throw new ApiError(403, "Permission denied"); return defaultApi(path); });
  view("Workflows"); await screen.findByRole("alert"); expect(screen.getByText("Permission denied")).toBeInTheDocument();
  expect(screen.getByText("No workflows yet.")).toBeInTheDocument();
});
test("initial workflow state shows loading", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? me : new Promise(() => {}));
  view("Workflows"); await screen.findByText("Loading workflows…");
});
