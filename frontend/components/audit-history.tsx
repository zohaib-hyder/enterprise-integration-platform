"use client";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Audit, type Page } from "../lib/api";
import { useAuth } from "./auth-provider";
export default function AuditHistory() {
  const { me } = useAuth(); const tenant = me?.active_tenant_id; const allowed = me?.permissions.includes("audit:read") ?? false;
  const [page, setPage] = useState<Page<Audit> | null>(null); const [offset, setOffset] = useState(0); const [error, setError] = useState(""); const [denied, setDenied] = useState(false); const [loading, setLoading] = useState(true);
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!tenant || !allowed) return;
    try { const page = await api<Page<Audit>>(`tenants/${tenant}/audit?offset=${offset}&limit=25`, { signal }); if (!signal?.aborted) { setPage(page); setError(""); } }
    catch (error) { if (!signal?.aborted) { setDenied(error instanceof ApiError && [401, 403].includes(error.status)); setError(error instanceof ApiError ? error.message : "Audit history unavailable"); } }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [tenant, allowed, offset]);
  useEffect(() => { const controller = new AbortController(); const task = setTimeout(() => void load(controller.signal), 0); return () => { controller.abort(); clearTimeout(task); }; }, [load]);
  if (!tenant || !allowed || denied) return <section><h1 className="text-3xl font-bold">Audit access denied</h1><p role="alert" className="mt-4">Tenant administrator access is required.</p></section>;
  return <section><h1 className="text-3xl font-bold">Audit history</h1><p className="mt-3 text-sm text-slate-500">Tenant access changes and invitation activity. Authentication events are recorded separately without tenant attribution.</p>{error && <p role="alert" className="mt-4 text-red-700">{error} <button onClick={() => void load()} className="underline">Retry</button></p>}{loading ? <p role="status" className="mt-6">Loading audit events…</p> : <div className="mt-6 space-y-3">{page?.items.map(event => <article key={event.id} className="rounded-lg border bg-white p-5"><div className="flex flex-wrap justify-between gap-3"><h2 className="font-semibold">{event.action}</h2><time className="text-xs text-slate-500" dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time></div><p className="mt-3 break-all text-xs text-slate-500">Actor: {event.actor_user_id ?? "System"} · {event.target_type}: {event.target_id ?? "—"}</p>{Object.keys(event.metadata).length > 0 && <p className="mt-3 text-sm text-slate-600">{Object.entries(event.metadata).map(([key, value]) => `${key}: ${value}`).join(" · ")}</p>}</article>)}{page?.items.length === 0 && <p className="text-slate-500">No audit events on this page.</p>}</div>}<div className="mt-6 flex items-center gap-4 text-sm"><button disabled={loading || offset === 0} onClick={() => { setLoading(true); setOffset(Math.max(0, offset - 25)); }} className="rounded border p-2 disabled:opacity-40">Previous</button><span>{page?.total ?? 0} events</span><button disabled={loading || offset + 25 >= (page?.total ?? 0)} onClick={() => { setLoading(true); setOffset(offset + 25); }} className="rounded border p-2 disabled:opacity-40">Next</button></div></section>;
}
