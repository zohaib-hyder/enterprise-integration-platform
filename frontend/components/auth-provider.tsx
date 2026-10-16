"use client";
import Link from "next/link";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, type Me } from "../lib/api";
type AuthState = { me: Me | null; loading: boolean; error: string; refresh: () => Promise<void>; switchTenant: (id: string) => Promise<void>; logout: () => Promise<void> };
const AuthContext = createContext<AuthState | null>(null);
export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("Auth provider required");
  return context;
}
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const router = useRouter();
  const refresh = useCallback(async () => {
    try { setMe(await api<Me>("auth/me")); setError(""); }
    catch (error) { setMe(null); setError(error instanceof ApiError && error.status === 401 ? "" : "Workspace could not be loaded. Please retry."); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { const task = setTimeout(() => void refresh(), 0); return () => clearTimeout(task); }, [refresh]);
  const switchTenant = async (id: string) => { const next = await api<Me>("tenant/switch", { method: "POST", data: { tenant_id: id } }); setMe(next); };
  const logout = async () => { try { await api("auth/logout", { method: "POST" }); } catch (error) { if (!(error instanceof ApiError) || error.status !== 401) throw error; } setMe(null); router.replace("/login"); };
  return <AuthContext.Provider value={{ me, loading, error, refresh, switchTenant, logout }}>{children}</AuthContext.Provider>;
}
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { me, loading, error, refresh } = useAuth();
  const router = useRouter();
  useEffect(() => { if (!loading && !me && !error) router.replace("/login"); }, [loading, me, error, router]);
  if (loading) return <main className="p-10" role="status">Loading your workspace…</main>;
  if (error) return <main className="p-10"><p role="alert">{error}</p><button className="mt-4 rounded border px-4 py-2" onClick={() => void refresh()}>Retry</button></main>;
  if (!me) return <main className="p-10"><p>Sign in to access the workspace.</p><Link href="/login" className="text-blue-600">Sign in</Link></main>;
  return <>{children}</>;
}
export function UserControls() {
  const { me, switchTenant, logout } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!me) return null;
  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await action(); } catch (error) { setError(error instanceof ApiError ? error.message : "Action failed. Please retry."); }
    finally { setBusy(false); }
  };
  return <div role="banner" aria-label="Organization and account" className="border-b border-slate-200 bg-white px-6 py-4 lg:ml-64"><div className="flex flex-wrap items-center justify-between gap-4">
    <label className="text-sm font-medium">Organization <select aria-label="Organization" disabled={busy} value={me.active_tenant_id ?? ""} onChange={event => void perform(() => switchTenant(event.target.value))} className="ml-3 rounded border border-slate-300 bg-white px-3 py-2">{!me.active_tenant_id && <option value="" disabled>Select organization</option>}{me.tenants.map(tenant => <option key={tenant.id} value={tenant.id}>{tenant.name}</option>)}</select></label>
    <details className="relative"><summary className="cursor-pointer text-sm font-semibold">{me.user.display_name} ▾</summary><div className="absolute right-0 z-20 mt-3 min-w-64 rounded-lg border bg-white p-4 shadow-lg"><p className="text-sm">{me.user.email}</p><p className="mt-2 text-xs text-slate-500">{me.tenants.find(tenant => tenant.id === me.active_tenant_id)?.role.replaceAll("_", " ") ?? "No active organization"}</p><Link href="/accept-invitation" className="mt-4 block text-sm text-blue-600">Accept an invitation</Link><button disabled={busy} onClick={() => void perform(logout)} className="mt-4 rounded border px-3 py-2 text-sm">Sign out</button></div></details>
  </div>{error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}{me.tenants.length === 0 && <p className="mt-3 text-sm text-amber-800">No active memberships. Ask an administrator for an invitation.</p>}</div>;
}
