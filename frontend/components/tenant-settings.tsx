"use client";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { useAuth } from "./auth-provider";
export default function TenantSettings() {
  const { me, refresh } = useAuth(); const tenant = me?.tenants.find(item => item.id === me.active_tenant_id);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [message, setMessage] = useState("");
  if (!tenant || !me?.permissions.includes("tenant:settings")) return <section><h1 className="text-3xl font-bold">Settings access denied</h1><p role="alert" className="mt-4">Tenant administrator access is required.</p></section>;
  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); setError(""); setMessage("");
    try { await api("tenant", { method: "PATCH", data: { name: data.get("name") } }); await refresh(); setMessage("Organization name updated"); }
    catch (error) { setError(error instanceof ApiError ? error.message : "Settings could not be saved"); }
    finally { setBusy(false); }
  }
  return <section><h1 className="text-3xl font-bold">Organization settings</h1><p className="mt-3 text-sm text-slate-500">Organization identity only. Integration settings are planned for later phases.</p><form onSubmit={save} className="mt-6 max-w-lg space-y-5"><label className="block">Organization name<input name="name" defaultValue={tenant.name} required maxLength={200} className="mt-2 block w-full rounded border bg-white p-3" /></label><p className="text-sm text-slate-500">Stable slug: {tenant.slug}</p>{error && <p role="alert" className="text-red-700">{error}</p>}{message && <p role="status" className="text-emerald-700">{message}</p>}<button disabled={busy} className="rounded bg-blue-600 px-4 py-3 text-white disabled:opacity-50">{busy ? "Saving…" : "Save settings"}</button></form></section>;
}
