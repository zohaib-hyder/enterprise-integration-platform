import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError, type Me } from "../lib/api";
import { AuthGuard, AuthProvider, UserControls } from "../components/auth-provider";
import LoginForm from "../components/login-form";
import MemberManagement from "../components/member-management";
import AuditHistory from "../components/audit-history";
import Dashboard from "../components/dashboard";
const mocks = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mocks.replace }) }));
vi.mock("../lib/api", async importOriginal => { const actual = await importOriginal<typeof import("../lib/api")>(); return { ...actual, api: vi.fn() }; });
const me: Me = { user: { id: "user-1", email: "synthetic@example.test", display_name: "Synthetic Admin" }, active_tenant_id: "tenant-1", tenants: [{ id: "tenant-1", name: "Synthetic Alpha", slug: "alpha", role: "TENANT_ADMIN" }, { id: "tenant-2", name: "Synthetic Beta", slug: "beta", role: "TENANT_ADMIN" }], permissions: ["members:manage", "audit:read"] };
beforeEach(() => { vi.mocked(api).mockReset(); mocks.replace.mockReset(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function renderAuth(children: React.ReactNode) { return render(<AuthProvider>{children}</AuthProvider>); }

test("login form submits credentials and uses a fixed redirect", async () => {
  vi.mocked(api).mockResolvedValue(me);
  renderAuth(<LoginForm />);
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: me.user.email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic-form-password" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/"));
  expect(api).toHaveBeenCalledWith("auth/login", { method: "POST", data: { email: me.user.email, password: "synthetic-form-password" } });
});
test("login error uses a generic backend message", async () => {
  vi.mocked(api).mockRejectedValue(new ApiError(401, "Invalid email or password"));
  renderAuth(<LoginForm />);
  fireEvent.submit(screen.getByRole("button", { name: "Sign in" }).closest("form")!);
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Invalid email or password"));
});
test("unauthenticated navigation redirects to login", async () => {
  vi.mocked(api).mockRejectedValue(new ApiError(401, "Authentication required"));
  renderAuth(<AuthGuard><p>Private content</p></AuthGuard>);
  expect(screen.getByRole("status")).toHaveTextContent("Loading your workspace");
  await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login"));
  expect(screen.queryByText("Private content")).not.toBeInTheDocument();
});
test("authenticated navigation exposes permitted links", async () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.mocked(api).mockResolvedValue(me);
  renderAuth(<AuthGuard><Dashboard permissions={me.permissions} /></AuthGuard>);
  await waitFor(() => expect(screen.getByRole("link", { name: "Members" })).toHaveAttribute("href", "/members"));
  expect(screen.getByRole("link", { name: "Audit" })).toHaveAttribute("href", "/audit");
});
test("organization switcher sends verified explicit context", async () => {
  vi.mocked(api).mockImplementation(async path => path === "tenant/switch" ? { ...me, active_tenant_id: "tenant-2" } : me);
  renderAuth(<AuthGuard><UserControls /></AuthGuard>);
  await screen.findByLabelText("Organization");
  fireEvent.change(screen.getByLabelText("Organization"), { target: { value: "tenant-2" } });
  await waitFor(() => expect(api).toHaveBeenCalledWith("tenant/switch", { method: "POST", data: { tenant_id: "tenant-2" } }));
  await waitFor(() => expect(screen.getByLabelText("Organization")).toHaveValue("tenant-2"));
});
test("logout calls endpoint and returns to login", async () => {
  vi.mocked(api).mockImplementation(async path => path === "auth/logout" ? { message: "Signed out" } : me);
  renderAuth(<AuthGuard><UserControls /></AuthGuard>);
  fireEvent.click(await screen.findByRole("button", { name: "Sign out", hidden: true }));
  await waitFor(() => expect(api).toHaveBeenCalledWith("auth/logout", { method: "POST" }));
  expect(mocks.replace).toHaveBeenCalledWith("/login");
});
test("operations users cannot load member or audit management", async () => {
  vi.mocked(api).mockResolvedValue({ ...me, permissions: ["operations:read"] });
  renderAuth(<AuthGuard><MemberManagement /><AuditHistory /></AuthGuard>);
  await screen.findByText("Member management unavailable");
  expect(screen.getByText("Audit access denied")).toBeInTheDocument();
  expect(vi.mocked(api).mock.calls.every(([path]) => path === "auth/me")).toBe(true);
});
test("member page has loading and empty invitation states", async () => {
  let resolveMembers: (value: unknown) => void = () => {};
  const pending = new Promise(resolve => { resolveMembers = resolve; });
  vi.mocked(api).mockImplementation(async path => path === "auth/me" ? me : path.includes("/members?") ? pending : { items: [], total: 0, offset: 0, limit: 25 });
  renderAuth(<AuthGuard><MemberManagement /></AuthGuard>);
  await screen.findByText("Loading members…");
  resolveMembers({ items: [], total: 0, offset: 0, limit: 25 });
  await screen.findByText("No invitations yet.");
  expect(screen.getByRole("button", { name: "Create invitation" })).toBeEnabled();
});
test("backend permission denial is shown by member page", async () => {
  vi.mocked(api).mockImplementation(async path => { if (path === "auth/me") return me; throw new ApiError(403, "Permission denied"); });
  renderAuth(<AuthGuard><MemberManagement /></AuthGuard>);
  await screen.findByText("Member management unavailable");
});
test("API outage preserves error state without sign-in redirect", async () => {
  vi.mocked(api).mockRejectedValue(new ApiError(503, "Unavailable"));
  renderAuth(<AuthGuard><p>Private content</p></AuthGuard>);
  await screen.findByRole("alert");
  expect(mocks.replace).not.toHaveBeenCalled();
});
