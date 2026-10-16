"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Page } from "../lib/api";
import type { Connection } from "../lib/integrations";
import type { Disclosure, EndpointStatus, InboxEvent, Receipt, WebhookEndpoint } from "../lib/events";
import { useAuth } from "./auth-provider";

const button = "rounded-lg border px-4 py-2 text-sm disabled:opacity-40";
const input = "mt-2 block w-full rounded-lg border p-3 text-sm";
const empty = <p className="mt-4 rounded-lg border border-dashed p-6 text-sm text-slate-500">No incoming events. Create an endpoint and explicitly deliver a synthetic signed event.</p>;
const types = ["job.created", "job.updated", "candidate.created", "candidate.updated"];

function History({ receipts }: { receipts: Page<Receipt> | null }) {
  return <div className="mt-4 overflow-x-auto"><table className="w-full text-left text-xs">
    <thead><tr><th className="p-2">Outcome</th><th className="p-2">Category</th><th className="p-2">Received</th><th className="p-2">Duration</th></tr></thead>
    <tbody>{receipts?.items.map(row => <tr key={row.id} className="border-t"><td className="p-2">{row.outcome}</td><td className="p-2">{row.error_category ?? "—"}</td><td className="p-2">{new Date(row.received_at).toLocaleString()}</td><td className="p-2">{row.duration_ms} ms</td></tr>)}</tbody>
  </table>{receipts?.total === 0 && <p className="mt-3 text-sm text-slate-500">No authenticated receipts yet.</p>}</div>;
}

export default function Events() {
  const { me } = useAuth();
  const manage = me?.permissions.includes("integrations:manage") ?? false;
  const read = me?.permissions.includes("operations:read") ?? false;
  const [events, setEvents] = useState<Page<InboxEvent> | null>(null);
  const [endpoints, setEndpoints] = useState<Page<WebhookEndpoint> | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [source, setSource] = useState("");
  const [eventType, setEventType] = useState("");
  const [status, setStatus] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [filters, setFilters] = useState("");
  const [offset, setOffset] = useState(0);
  const [endpointOffset, setEndpointOffset] = useState(0);
  const [detail, setDetail] = useState<InboxEvent | null>(null);
  const [eventReceipts, setEventReceipts] = useState<Page<Receipt> | null>(null);
  const [eventReceiptOffset, setEventReceiptOffset] = useState(0);
  const [selected, setSelected] = useState<WebhookEndpoint | null>(null);
  const [endpointReceipts, setEndpointReceipts] = useState<Page<Receipt> | null>(null);
  const [endpointReceiptOffset, setEndpointReceiptOffset] = useState(0);
  const [health, setHealth] = useState<EndpointStatus | null>(null);
  const [creating, setCreating] = useState(false);
  const [connectionId, setConnectionId] = useState("");
  const [grace, setGrace] = useState(300);
  const [simulationType, setSimulationType] = useState("job.created");
  const [simulationId, setSimulationId] = useState("");
  const [scenario, setScenario] = useState("valid");
  const [simulationResult, setSimulationResult] = useState<unknown>(null);
  // Credentials exist only in this transient disclosure and DOM, never normal React data state.
  const pendingSecret = useRef("");
  const secretInput = useRef<HTMLInputElement | null>(null);
  const [disclosing, setDisclosing] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const mounted = useRef(true);
  const attachSecret = useCallback((node: HTMLInputElement | null) => {
    if (secretInput.current) secretInput.current.value = "";
    secretInput.current = node;
    if (node) { node.value = pendingSecret.current; pendingSecret.current = ""; }
  }, []);
  function clearSecret() {
    if (secretInput.current) secretInput.current.value = "";
    pendingSecret.current = ""; setRevealed(false); setDisclosing(false);
  }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; pendingSecret.current = ""; if (secretInput.current) secretInput.current.value = ""; }; }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!read || !me?.active_tenant_id) { setLoading(false); return; }
    try {
      const [inbox, hooks, connectionPage] = await Promise.all([
        api<Page<InboxEvent>>(`events?offset=${offset}&limit=25${filters}`, { signal }),
        api<Page<WebhookEndpoint>>(`webhook-endpoints?offset=${endpointOffset}&limit=25`, { signal }),
        manage ? api<Page<Connection>>("connections?offset=0&limit=100", { signal }) : Promise.resolve(null),
      ]);
      if (!signal?.aborted && mounted.current) {
        setEvents(inbox); setEndpoints(hooks);
        setConnections(connectionPage?.items.filter(row => ["synthetic_crm", "synthetic_ats"].includes(row.connector_type) && row.status === "ENABLED") ?? []);
        setError("");
      }
    } catch (error) { if (!signal?.aborted && mounted.current) setError(error instanceof ApiError ? error.message : "Event inbox unavailable"); }
    finally { if (!signal?.aborted && mounted.current) setLoading(false); }
  }, [read, manage, me?.active_tenant_id, offset, endpointOffset, filters]);
  useEffect(() => {
    const controller = new AbortController();
    const initial = setTimeout(() => void load(controller.signal), 0);
    return () => { clearTimeout(initial); controller.abort(); };
  }, [load]);

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); }
    catch (error) { if (mounted.current) setError(error instanceof ApiError ? error.message : "Action failed. Check input and retry."); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function openEvent(row: InboxEvent, receiptOffset = 0) {
    await perform(async () => {
      const [event, history] = await Promise.all([
        api<InboxEvent>(`events/${row.id}`), api<Page<Receipt>>(`events/${row.id}/receipts?offset=${receiptOffset}&limit=25`),
      ]);
      if (mounted.current) { setDetail(event); setEventReceipts(history); setEventReceiptOffset(receiptOffset); }
    });
  }
  async function openEndpoint(row: WebhookEndpoint, receiptOffset = 0) {
    await perform(async () => {
      const [current, history, health] = await Promise.all([
        api<WebhookEndpoint>(`webhook-endpoints/${row.id}`),
        api<Page<Receipt>>(`webhook-endpoints/${row.id}/receipts?offset=${receiptOffset}&limit=25`),
        api<EndpointStatus>(`webhook-endpoints/${row.id}/status`),
      ]);
      if (mounted.current) {
        setSelected(current); setEndpointReceipts(history); setHealth(health); setEndpointReceiptOffset(receiptOffset);
        if (selected?.id !== row.id) { setSimulationId(`demo-${crypto.randomUUID()}`); setSimulationType("job.created"); setSimulationResult(null); }
      }
    });
  }
  function disclose(response: Disclosure) {
    if (!mounted.current) return;
    if (secretInput.current) secretInput.current.value = response.signing_secret;
    else pendingSecret.current = response.signing_secret;
    setRevealed(false); setDisclosing(true);
  }
  async function create(event: React.FormEvent) {
    event.preventDefault();
    await perform(async () => {
      const connection = connections.find(row => row.id === connectionId);
      if (!connection) throw new Error("Select a connection");
      const response = await api<Disclosure>("webhook-endpoints", { method: "POST", data: { connection_id: connection.id, source: connection.connector_type } });
      disclose(response); setCreating(false); setSelected(response.endpoint); setEndpointReceipts(null); setHealth(null); setSimulationId(`demo-${crypto.randomUUID()}`); setSimulationType("job.created"); setSimulationResult(null);
      await load(); setNotice("Endpoint created. Save the signing secret from its one-time disclosure.");
    });
  }
  function applyFilters(event: React.FormEvent) {
    event.preventDefault();
    const query = new URLSearchParams();
    if (source) query.set("source", source);
    if (eventType) query.set("event_type", eventType);
    if (status) query.set("status", status);
    if (since) query.set("since", new Date(since).toISOString());
    if (until) query.set("until", new Date(until).toISOString());
    setOffset(0); setLoading(true); setFilters(query.size ? `&${query}` : "");
    if (offset === 0 && filters === (query.size ? `&${query}` : "")) void load();
  }

  if (!read || !me?.active_tenant_id) return <section><h1 className="text-3xl font-bold">Events</h1><p role="alert" className="mt-4">Select an authorized organization with event-read permission.</p></section>;
  return <section>
    <h1 className="text-3xl font-bold">Event inbox</h1><p className="mt-2 text-sm text-slate-500">Inbox PENDING records accepted ingestion. Dispatch receipts and Executions track workflow progress separately.</p>
    <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-500">Actual persisted synthetic webhook events. Accepted inbox evidence remains PENDING; workflow dispatch and progress are tracked in Executions. Full provider synchronization remains deferred. Only authenticated deliveries receive tenant-attributed history.</p>
    {error && <p role="alert" className="mt-4 rounded-lg bg-red-50 p-4 text-sm text-red-700">{error} <button className="underline" onClick={() => void load()}>Retry</button></p>}
    {notice && <p role="status" className="mt-4 rounded-lg bg-blue-50 p-4 text-sm text-blue-800">{notice}</p>}
    <form onSubmit={applyFilters} className="mt-6 grid gap-4 rounded-xl border bg-white p-5 sm:grid-cols-2 xl:grid-cols-3">
      <label className="text-sm">Source<select className={input} value={source} onChange={event => setSource(event.target.value)}><option value="">All sources</option><option value="synthetic_crm">Synthetic CRM</option><option value="synthetic_ats">Synthetic ATS</option></select></label>
      <label className="text-sm">Event type<select className={input} value={eventType} onChange={event => setEventType(event.target.value)}><option value="">All types</option>{types.map(type => <option key={type}>{type}</option>)}</select></label>
      <label className="text-sm">Status<select className={input} value={status} onChange={event => setStatus(event.target.value)}><option value="">All inbox events</option><option value="ACCEPTED">Accepted ingestion</option><option value="PENDING">Pending processing</option></select></label>
      <label className="text-sm">Received after<input className={input} type="datetime-local" value={since} onChange={event => setSince(event.target.value)} /></label>
      <label className="text-sm">Received before<input className={input} type="datetime-local" value={until} onChange={event => setUntil(event.target.value)} /></label>
      <button className={button} disabled={busy}>Apply filters</button>
    </form>
    {loading ? <p role="status" className="mt-5">Loading events…</p> : events?.total === 0 ? empty : <div className="mt-6 overflow-x-auto rounded-xl border bg-white"><table className="w-full text-left text-sm"><thead className="bg-slate-50"><tr>{["Event", "Source", "Ingestion / processing", "Duplicates", "Received"].map(title => <th key={title} className="p-4">{title}</th>)}</tr></thead><tbody>{events?.items.map(row => <tr key={row.id} className="border-t"><td className="p-4"><button disabled={busy} onClick={() => void openEvent(row)} className="font-medium text-blue-700 underline">{row.provider_event_id}</button><p className="mt-1 text-xs text-slate-500">{row.event_type}</p></td><td className="p-4">{row.source}</td><td className="p-4"><span className="rounded bg-emerald-50 px-2 py-1 text-xs text-emerald-800">{row.ingestion_status}</span><span className="ml-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">{row.processing_status}</span></td><td className="p-4">{row.duplicate_count}</td><td className="p-4 text-xs">{new Date(row.received_at).toLocaleString()}</td></tr>)}</tbody></table></div>}
    <div className="mt-4 flex items-center gap-4"><button className={button} disabled={busy || loading || offset === 0} onClick={() => { setLoading(true); setOffset(offset - 25); }}>Previous events</button><span className="text-sm">{events?.total ?? 0} persisted events</span><button className={button} disabled={busy || loading || offset + 25 >= (events?.total ?? 0)} onClick={() => { setLoading(true); setOffset(offset + 25); }}>Next events</button></div>
    {detail && <section aria-label="Event details" className="mt-6 rounded-xl border bg-white p-6"><div className="flex justify-between gap-4"><h2 className="text-xl font-semibold">Event details</h2><button className={button} onClick={() => { setDetail(null); setEventReceipts(null); }}>Close details</button></div><p className="mt-3 text-sm">{detail.provider_event_id} · {detail.event_type} · {detail.source}</p><p className="mt-2 break-all text-xs text-slate-500">Correlation: {detail.correlation_id}</p><h3 className="mt-5 font-medium">Redacted normalized payload</h3><pre className="mt-3 max-h-80 overflow-auto rounded bg-slate-950 p-4 text-xs text-slate-100">{JSON.stringify(detail.payload, null, 2)}</pre><h3 className="mt-5 font-medium">Delivery receipts</h3><History receipts={eventReceipts} /><div className="mt-3 flex gap-3"><button className={button} disabled={busy || eventReceiptOffset === 0} onClick={() => void openEvent(detail, eventReceiptOffset - 25)}>Previous event receipts</button><button className={button} disabled={busy || eventReceiptOffset + 25 >= (eventReceipts?.total ?? 0)} onClick={() => void openEvent(detail, eventReceiptOffset + 25)}>Next event receipts</button></div></section>}
    <div className="mt-10 flex flex-wrap items-center justify-between gap-4"><h2 className="text-xl font-semibold">Webhook endpoints</h2>{manage && <button className={button} disabled={busy || loading} onClick={() => { setCreating(true); setConnectionId(connections[0]?.id ?? ""); }}>Create endpoint</button>}</div>
    {!manage && <p className="mt-3 text-sm text-slate-500">Read-only endpoint and event history. An administrator or integration engineer manages signing credentials.</p>}
    {endpoints?.total === 0 && <p className="mt-4 text-sm text-slate-500">No webhook endpoints configured.</p>}
    <div className="mt-4 grid gap-3 md:grid-cols-2">{endpoints?.items.map(row => <button key={row.id} disabled={busy} onClick={() => void openEndpoint(row)} className="rounded-xl border bg-white p-5 text-left"><strong className="text-sm">{row.source} · {row.status}</strong><p className="mt-2 break-all text-xs text-slate-500">{row.id}</p><p className="mt-2 text-xs">HMAC v1 · secret version {row.current_secret_version}</p></button>)}</div>
    <div className="mt-4 flex gap-3"><button className={button} disabled={busy || endpointOffset === 0} onClick={() => setEndpointOffset(endpointOffset - 25)}>Previous endpoints</button><button className={button} disabled={busy || endpointOffset + 25 >= (endpoints?.total ?? 0)} onClick={() => setEndpointOffset(endpointOffset + 25)}>Next endpoints</button></div>
    {creating && manage && <form onSubmit={create} className="mt-5 rounded-xl border bg-white p-6"><h3 className="font-semibold">Create a synthetic webhook endpoint</h3><label className="mt-4 block text-sm">Authorized connection<select required className={input} value={connectionId} onChange={event => setConnectionId(event.target.value)}><option value="">Select a connection</option>{connections.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label><p className="mt-3 text-xs text-slate-500">Only enabled CRM/ATS connections in your active organization are eligible. First 100 connections shown.</p><div className="mt-4 flex gap-3"><button className={button} disabled={busy || !connectionId}>Create signed endpoint</button><button type="button" className={button} onClick={() => setCreating(false)}>Cancel</button></div></form>}
    {selected && <section aria-label="Endpoint details" className="mt-6 rounded-xl border bg-white p-6"><h3 className="font-semibold">Endpoint details</h3><p className="mt-3 break-all rounded bg-slate-50 p-3 font-mono text-xs">/api/v1/webhooks/ingest/{selected.public_id}</p><p className="mt-3 text-sm">{selected.status} · {selected.timestamp_tolerance}s timestamp tolerance · current secret v{selected.current_secret_version}</p>{selected.previous_secret_valid_until && <p className="mt-2 text-xs text-slate-500">Previous key deadline: {new Date(selected.previous_secret_valid_until).toLocaleString()}</p>}
      {health && <div className="mt-4 rounded bg-slate-50 p-4 text-sm"><p>Connection {health.connection_status} · signing key {health.secret_available ? "available" : "unavailable"}</p><p className="mt-2 text-xs">Authenticated receipts: {Object.entries(health.receipt_counts).map(([key, count]) => `${key}: ${count}`).join(" · ") || "none"}</p><p className="mt-2 text-xs">Rejection categories: {Object.entries(health.error_counts).map(([key, count]) => `${key}: ${count}`).join(" · ") || "none"}</p></div>}
      {manage && <div className="mt-5 space-y-4"><button disabled={busy} className={button} onClick={() => void perform(async () => { const updated = await api<WebhookEndpoint>(`webhook-endpoints/${selected.id}`, { method: "PATCH", data: { enabled: selected.status !== "ACTIVE" } }); setSelected(updated); await load(); setNotice("Endpoint status updated"); })}>{selected.status === "ACTIVE" ? "Disable endpoint" : "Enable endpoint"}</button><div className="flex flex-wrap items-end gap-3"><label className="text-sm">Rotation grace (seconds)<input type="number" min={0} max={900} value={grace} onChange={event => setGrace(Number(event.target.value))} className={input} /></label><button className={button} disabled={busy || grace < 0 || grace > 900} onClick={() => void perform(async () => { const response = await api<Disclosure>(`webhook-endpoints/${selected.id}/rotate-secret`, { method: "POST", data: { grace_seconds: grace } }); disclose(response); setSelected(response.endpoint); await load(); setNotice("Signing secret rotated. Save the new one-time value."); })}>Rotate signing secret</button></div>
      <form onSubmit={event => { event.preventDefault(); void perform(async () => { const result = await api(`webhook-endpoints/${selected.id}/simulate`, { method: "POST", data: { event_type: simulationType, event_id: simulationId, scenario } }); setSimulationResult(result); await load(); const [history, currentHealth] = await Promise.all([api<Page<Receipt>>(`webhook-endpoints/${selected.id}/receipts?offset=0&limit=25`), api<EndpointStatus>(`webhook-endpoints/${selected.id}/status`)]); if (mounted.current) { setEndpointReceipts(history); setEndpointReceiptOffset(0); setHealth(currentHealth); } setNotice("Explicit local simulation completed. No external provider was contacted."); }); }} className="space-y-3 border-t pt-5"><h4 className="font-medium">Synthetic signed delivery</h4><p className="text-xs text-slate-500">Explicitly exercises the same verifier and inbox in process. Use the documented CLI for real localhost HTTP delivery.</p><label className="block text-sm">Simulation event type<select className={input} value={simulationType} onChange={event => setSimulationType(event.target.value)}>{types.filter(type => selected.source === "synthetic_ats" || type.startsWith("job.")).map(type => <option key={type}>{type}</option>)}</select></label><label className="block text-sm">Provider event ID<input required maxLength={100} pattern="[a-zA-Z0-9][a-zA-Z0-9_.:-]*" className={input} value={simulationId} onChange={event => setSimulationId(event.target.value)} /></label><label className="block text-sm">Delivery scenario<select className={input} value={scenario} onChange={event => setScenario(event.target.value)}>{["valid", "duplicate", "invalid_signature", "stale", "altered_payload"].map(value => <option key={value}>{value}</option>)}</select></label><button className={button} disabled={busy || selected.status !== "ACTIVE"}>Deliver synthetic event</button>{simulationResult !== null && <pre className="max-h-60 overflow-auto rounded bg-slate-950 p-4 text-xs text-slate-100">{JSON.stringify(simulationResult, null, 2)}</pre>}</form></div>}
      <h4 className="mt-6 font-medium">Authenticated endpoint delivery history</h4><History receipts={endpointReceipts} /><div className="mt-3 flex gap-3"><button className={button} disabled={busy || endpointReceiptOffset === 0} onClick={() => void openEndpoint(selected, endpointReceiptOffset - 25)}>Previous endpoint receipts</button><button className={button} disabled={busy || endpointReceiptOffset + 25 >= (endpointReceipts?.total ?? 0)} onClick={() => void openEndpoint(selected, endpointReceiptOffset + 25)}>Next endpoint receipts</button><button className={button} disabled={busy} onClick={() => void openEndpoint(selected, endpointReceiptOffset)}>Refresh endpoint history</button></div>
    </section>}
    {disclosing && manage && <section role="dialog" aria-modal="true" aria-label="One-time signing secret" className="fixed inset-0 z-40 flex items-center justify-center bg-slate-950/50 p-6"><div className="w-full max-w-xl rounded-xl bg-white p-6"><h2 className="text-xl font-semibold">One-time signing secret</h2><p className="mt-3 text-sm text-slate-500">Save this privately for the synthetic sender. It cannot be retrieved after closing. Never put it in a URL, log or issue.</p><label className="mt-4 block text-sm">Signing secret<input ref={attachSecret} type={revealed ? "text" : "password"} readOnly autoComplete="off" className={input} /></label><div className="mt-5 flex gap-3"><button className={button} onClick={() => setRevealed(!revealed)}>{revealed ? "Hide secret" : "Reveal secret"}</button><button className={button} onClick={clearSecret}>Saved; close disclosure</button></div></div></section>}
  </section>;
}
