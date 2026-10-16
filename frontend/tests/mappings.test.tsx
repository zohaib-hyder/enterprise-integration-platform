import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import { AuthGuard, AuthProvider } from "../components/auth-provider";
import Mappings from "../components/mappings";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("../lib/api", async importOriginal => { const actual = await importOriginal<typeof import("../lib/api")>(); return { ...actual, api: vi.fn() }; });
const me = { user: { id: "synthetic-user", display_name: "Synthetic Engineer", email: "engineer@example.test" }, tenants: [{ id: "tenant-a", name: "Synthetic Organization", role: "INTEGRATION_ENGINEER" }], active_tenant_id: "tenant-a", permissions: ["operations:read", "integrations:manage"] };
const mapping = { id: "mapping-a", connection_id: "connection-a", source_kind: "contact", entity_type: "contact", name: "Demo contact", active_version: 1 };
const definition = { schema_version: 1, fields: ["name", "email", "company"].map(target => ({ target, source: `data.${target}`, required: true, null_handling: "reject", default: null, steps: [{ operation: target === "email" ? "email" : "trim" }] })) };
const version = { id: "version-a", mapping_id: "mapping-a", version: 1, definition, definition_digest: "safe-digest", created_at: "2030-01-15T12:00:00Z" };
const record = { canonical_id: "canonical-a", entity_type: "contact", source_connection_id: "connection-a", source_external_id: "source-1", normalized_data: { name: "[REDACTED]", email: "[REDACTED]", company: "Fictional Harbor" }, revision: 1, field_provenance: {} };
const attempt = { id: "attempt-a", mapping_version_id: "version-a", canonical_id: "canonical-a", event_id: null, external_id: "source-1", outcome: "SUCCEEDED", issues: [], result_snapshot: record };
const invalid = { id: "invalid-a", status: "OPEN", resolution_attempt_id: null, attempt: { ...attempt, id: "invalid-attempt", outcome: "QUARANTINED", issues: [{ category: "MISSING_FIELD", field: "company" }] } };
const conflict = { ...attempt, id: "conflict-a", outcome: "CONFLICT", issues: [{ category: "AUTHORITY_CONFLICT", field: "company" }] };
const template = { source_type: "synthetic_crm", source_kind: "contact", entity_type: "contact", source_schema: {}, source_paths: ["data.name", "data.email", "data.company"], target_fields: ["name", "email", "company"], definition };
const page = (items: unknown[]) => ({ items, total: items.length, offset: 0, limit: 25 });
function defaultApi(path: string): unknown {
  if (path === "auth/me") return me;
  if (path.startsWith("mappings?")) return page([mapping]);
  if (path === "mapping-templates") return [template];
  if (path.startsWith("canonical-records?")) return page([record]);
  if (path === "canonical-records/canonical-a") return record;
  if (path.startsWith("invalid-records?")) return page([invalid]);
  if (path.startsWith("transformation-conflicts?")) return page([conflict]);
  if (path.startsWith("transformation-attempts?")) return page([attempt]);
  if (path === "transformation-attempts/attempt-a") return attempt;
  if (path.startsWith("external-identities?")) return page([{ id: "identity-a", connection_id: "connection-a", source_kind: "contact", external_id: "source-1", canonical_id: "canonical-a", provenance: "NORMALIZATION" }]);
  if (path.startsWith("field-authority?")) return page([{ id: "rule-a", entity_type: "contact", field: "company", owner: "synthetic_crm" }]);
  if (path.startsWith("connections?")) return page([{ id: "connection-a", name: "Synthetic CRM", connector_type: "synthetic_crm", status: "ENABLED" }]);
  if (path.startsWith("events?")) return page([]);
  if (path.startsWith("mappings/mapping-a/versions?")) return page([version]);
  if (path === "mappings/mapping-a/versions") return { ...version, id: "version-b", version: 2 };
  if (path.endsWith("/activate") || path === "mappings") return mapping;
  if (path.endsWith("/preview")) return { mapping_version_id: "version-a", outcome: "SUCCEEDED", canonical: record, persisted: false, issues: [] };
  if (path.endsWith("/normalize") || path.endsWith("/reprocess")) return attempt;
  return {};
}
beforeEach(() => { vi.mocked(api).mockReset(); vi.mocked(api).mockImplementation(async path => defaultApi(path)); });
afterEach(() => cleanup());
function view() { return render(<AuthProvider><AuthGuard><Mappings /></AuthGuard></AuthProvider>); }
async function selectMapping() { fireEvent.click(await screen.findByRole("button", { name: /Demo contact/ })); return screen.findByRole("region", { name: "Mapping details" }); }

test("mapping list shows source target and immutable active version", async () => {
  view(); await screen.findByRole("button", { name: /Demo contact/ });
  expect(screen.getByText(/contact → contact · active v1/)).toBeInTheDocument();
});
test("field editor only offers allowlisted paths and creates a new version", async () => {
  view(); const details = await selectMapping();
  const paths = within(details).getAllByLabelText("Source path");
  expect(within(paths[0]).queryByRole("option", { name: "data.password" })).not.toBeInTheDocument();
  fireEvent.change(paths[0], { target: { value: "data.company" } });
  fireEvent.click(within(details).getByRole("button", { name: "Save new mapping version" }));
  await screen.findByText(/Immutable version 2 created/);
  const request = vi.mocked(api).mock.calls.find(([path]) => path === "mappings/mapping-a/versions");
  expect(request?.[1]?.method).toBe("POST");
  expect((request?.[1]?.data as { definition: typeof definition }).definition.fields[0].source).toBe("data.company");
});
test("version activation is an explicit protected action", async () => {
  view(); const details = await selectMapping();
  fireEvent.click(within(details).getByRole("button", { name: "Activate viewed version" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("mappings/mapping-a/activate", { method: "POST", data: { mapping_version_id: "version-a" } }));
});
test("preview uses exact saved mapping version and does not automatically normalize", async () => {
  view(); const details = await selectMapping();
  fireEvent.click(within(details).getByRole("button", { name: "Preview transformation" }));
  await screen.findByText(/Preview only; no canonical record/);
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith("/normalize"))).toBe(false);
  const request = vi.mocked(api).mock.calls.find(([path]) => path.endsWith("/preview"));
  expect((request?.[1]?.data as { mapping_version_id: string }).mapping_version_id).toBe("version-a");
});
test("normalization requires explicit click and refreshes actual data", async () => {
  view(); const details = await selectMapping();
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith("/normalize"))).toBe(false);
  fireEvent.click(within(details).getByRole("button", { name: "Normalize source record" }));
  await screen.findByText(/Normalization completed locally/);
});
test("canonical browser inspects redacted data and provenance", async () => {
  view(); fireEvent.click(await screen.findByRole("button", { name: /contact · source-1/ }));
  await screen.findByText("Redacted canonical record and provenance");
  expect(screen.getByText(/\[REDACTED\]/)).toBeInTheDocument();
});
test("external identity lookup is scoped and bounded", async () => {
  view(); await screen.findByRole("button", { name: /Demo contact/ });
  fireEvent.change(screen.getByLabelText("Lookup external ID"), { target: { value: "source-1" } });
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith("external-identities?") && path.includes("external_id=source-1"))).toBe(true));
});
test("invalid queue exposes fixed issues and corrected-version reprocessing", async () => {
  view(); await selectMapping();
  expect(screen.getByText("MISSING_FIELD: company")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reprocess with viewed corrected version" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("invalid-records/invalid-a/reprocess", { method: "POST", data: { mapping_version_id: "version-a" } }));
});
test("source-of-truth conflict evidence is visible without overwriting controls", async () => {
  view(); await screen.findByText("AUTHORITY_CONFLICT: company");
  expect(screen.queryByRole("button", { name: /Force overwrite/ })).not.toBeInTheDocument();
});
test("operations user can inspect but has no mutation or preview controls", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, permissions: ["operations:read"] } : defaultApi(path));
  view(); const details = await selectMapping();
  expect(screen.queryByRole("button", { name: "Create mapping" })).not.toBeInTheDocument();
  expect(within(details).queryByRole("button", { name: "Save new mapping version" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Normalize source record" })).not.toBeInTheDocument();
  expect(within(details).getAllByLabelText("Source path")[0]).toBeDisabled();
});
test("empty state does not fabricate normalization metrics", async () => {
  vi.mocked(api).mockImplementation(async path => path.includes("?") && !path.startsWith("field-authority?") && !path.startsWith("connections?") ? page([]) : defaultApi(path));
  view(); await screen.findByText(/No mappings yet/); expect(screen.getByText("No canonical records yet.")).toBeInTheDocument();
});
test("loading and unavailable data have explicit error states", async () => {
  let reject: (value: unknown) => void = () => {};
  const pending = new Promise((_, rejection) => { reject = rejection; }); void pending.catch(() => {});
  vi.mocked(api).mockImplementation(async path => path.startsWith("mappings?") ? pending : defaultApi(path));
  view(); await screen.findByText("Loading mapping workspace…"); reject(new ApiError(503, "Mapping data unavailable"));
  await screen.findByRole("alert"); expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled();
});
test("tenant selection is required before data requests", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? { ...me, active_tenant_id: null, permissions: [] } : defaultApi(path));
  view(); await screen.findByRole("alert");
  expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith("mappings?"))).toBe(false);
});

test("invalid JSON field parameters cannot silently save the old definition", async () => {
  view(); const details = await selectMapping();
  const field = within(details).getAllByLabelText("Default value (JSON)")[0];
  fireEvent.change(field, { target: { value: "{" } }); fireEvent.blur(field);
  fireEvent.click(within(details).getByRole("button", { name: "Save new mapping version" }));
  await screen.findByText(/Correct invalid field parameters/);
  expect(vi.mocked(api).mock.calls.some(([path]) => path === "mappings/mapping-a/versions")).toBe(false);
});
