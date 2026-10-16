"use client";
import { useCallback, useEffect, useState } from "react";
import { api, type Page } from "../lib/api";
import type { Connection } from "../lib/integrations";
import type { Mapping, MappingVersion } from "../lib/mappings";
import { workflowTemplate, type DeadLetter, type Execution, type History, type Retry, type Version, type WorkerStatus, type Workflow } from "../lib/workflows";
import { useAuth } from "./auth-provider";

const button = "rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm disabled:opacity-40";
const input = "mt-1 w-full rounded-lg border border-slate-200 p-2 text-sm";
function Pager({ total, offset, change }: { total: number; offset: number; change: (n: number) => void }) {
  return <div className="mt-4 flex items-center gap-3 text-sm"><button className={button} disabled={!offset} onClick={() => change(offset-25)}>Previous</button><span>{total} records</span><button className={button} disabled={offset+25 >= total} onClick={() => change(offset+25)}>Next</button></div>;
}
export default function WorkflowConsole({ mode }: { mode: "Workflows" | "Executions" | "Failures" }) {
  const { me } = useAuth();
  const read = me?.permissions.includes("operations:read") ?? false;
  const manage = me?.permissions.includes("integrations:manage") ?? false;
  const recover = me?.permissions.includes("executions:recover") ?? false;
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [offset, setOffset] = useState(0), [total, setTotal] = useState(0), [filter, setFilter] = useState("");
  const [workflows, setWorkflows] = useState<Workflow[]>([]), [executions, setExecutions] = useState<Execution[]>([]), [failures, setFailures] = useState<DeadLetter[]>([]), [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [selected, setSelected] = useState<Workflow | null>(null), [versions, setVersions] = useState<Version[]>([]), [version, setVersion] = useState<Version | null>(null), [versionOffset, setVersionOffset] = useState(0), [versionTotal, setVersionTotal] = useState(0);
  const [detail, setDetail] = useState<Execution | null>(null), [history, setHistory] = useState<History[]>([]), [retries, setRetries] = useState<Retry[]>([]), [historyOffset, setHistoryOffset] = useState(0), [historyTotal, setHistoryTotal] = useState(0), [retryOffset, setRetryOffset] = useState(0), [retryTotal, setRetryTotal] = useState(0), [confirmReplay, setConfirmReplay] = useState(false);
  const [connections, setConnections] = useState<Connection[]>([]), [mappings, setMappings] = useState<Mapping[]>([]), [connection, setConnection] = useState(""), [mappingVersions, setMappingVersions] = useState<MappingVersion[]>([]), [mappingVersion, setMappingVersion] = useState(""), [name, setName] = useState(""), [retryDemo, setRetryDemo] = useState(false), [rescan, setRescan] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!read) { setLoading(false); return; }
    try {
      const path = mode === "Workflows" ? "workflows" : mode === "Executions" ? `executions${filter ? `?state=${filter}&` : "?"}` : "dead-letters";
      const separator = path.includes("?") ? "" : "?";
      const [page, status] = await Promise.all([api<Page<Workflow | Execution | DeadLetter>>(`${path}${separator}offset=${offset}&limit=25`, { signal }), api<WorkerStatus>("worker-status", { signal })]);
      if (signal?.aborted) return;
      setTotal(page.total); setWorker(status);
      if (mode === "Workflows") setWorkflows(page.items as Workflow[]);
      else if (mode === "Executions") setExecutions(page.items as Execution[]);
      else setFailures(page.items as DeadLetter[]);
      setError("");
    } catch (e) { if (!signal?.aborted) setError(e instanceof Error ? e.message : "Unable to load workflow data"); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [read, mode, filter, offset]);
  useEffect(() => { const controller = new AbortController(); const timer = setTimeout(() => void load(controller.signal), 0); return () => { clearTimeout(timer); controller.abort(); }; }, [load]);
  useEffect(() => {
    if (!manage || mode !== "Workflows") return;
    const controller = new AbortController();
    Promise.all([api<Page<Connection>>("connections?limit=100", { signal: controller.signal }), api<Page<Mapping>>("mappings?limit=100", { signal: controller.signal })]).then(([c, m]) => { if (!controller.signal.aborted) { setConnections(c.items.filter(v => ["synthetic_crm", "synthetic_ats"].includes(v.connector_type) && v.status === "ENABLED")); setMappings(m.items.filter(v => v.source_kind === "requisition")); } }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [manage, mode]);
  useEffect(() => {
    const mapping = mappings.find(m => m.connection_id === connection);
    if (!mapping) return;
    const controller = new AbortController();
    api<Page<MappingVersion>>(`mappings/${mapping.id}/versions?limit=100`, { signal: controller.signal }).then(p => { if (!controller.signal.aborted) { setMappingVersions(p.items); setMappingVersion(p.items.find(v => v.version === mapping.active_version)?.id ?? ""); } }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [connection, mappings]);
  async function action(fn: () => Promise<void>) { setBusy(true); setError(""); setNotice(""); try { await fn(); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Operation failed"); } finally { setBusy(false); } }
  async function inspectWorkflow(w: Workflow, at = 0) { const p = await api<Page<Version>>(`workflows/${w.id}/versions?offset=${at}&limit=25`); setSelected(w); setVersions(p.items); setVersion(p.items.find(v => v.version === w.active_version) ?? p.items[0] ?? null); setVersionOffset(at); setVersionTotal(p.total); }
  async function inspectExecution(id: string, h = 0, r = 0) { const [d, hp, rp] = await Promise.all([api<Execution>(`executions/${id}`), api<Page<History>>(`executions/${id}/history?offset=${h}&limit=25`), api<Page<Retry>>(`executions/${id}/retries?offset=${r}&limit=25`)]); setDetail(d); setHistory(hp.items); setHistoryTotal(hp.total); setHistoryOffset(h); setRetries(rp.items); setRetryTotal(rp.total); setRetryOffset(r); setConfirmReplay(false); }
  if (!read) return <p role="alert">You do not have permission to inspect workflows.</p>;
  if (loading) return <p role="status">Loading {mode.toLowerCase()}…</p>;
  return <div className="space-y-6">
    <header className="flex flex-wrap items-center justify-between gap-4"><div><h2 className="text-xl font-semibold">{mode}</h2><p className="mt-1 text-sm text-slate-500">Durable sequential workflows · synthetic providers only</p></div><button className={button} disabled={busy} onClick={() => void action(async () => { if (detail) await inspectExecution(detail.id, historyOffset, retryOffset); })}>Refresh</button></header>
    {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}{notice && <p role="status">{notice}</p>}
    {worker && <section aria-label="Worker health" className="rounded-xl border bg-slate-50 p-4"><h3 className="font-medium">Worker health</h3><p className="mt-2 text-sm">{worker.active_workers.length} active worker heartbeat{worker.active_workers.length === 1 ? "" : "s"} · {Object.entries(worker.backlog).map(([s, n]) => `${s}: ${n}`).join(" · ") || "No tenant executions"}</p><p className="mt-2 text-xs text-slate-500">Observed {new Date(worker.checked_at).toLocaleString()}. Heartbeats reflect shared infrastructure; counts belong to this tenant.</p></section>}
    {mode === "Workflows" && <>
      {manage && <form className="grid gap-4 rounded-xl border p-5 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); void action(async () => { const source = connections.find(c => c.id === connection)?.connector_type; if (!source) throw new Error("Choose a source connection"); const definition = workflowTemplate(source, connection, mappingVersion, retryDemo); await api("workflows/validate", { method: "POST", data: definition }); const created = await api<Workflow>("workflows", { method: "POST", data: { name, description: "Safe synthetic job observation", definition } }); await inspectWorkflow(created); setName(""); setNotice("Workflow created inactive. Review and activate a version."); }); }}>
        <h3 className="font-semibold sm:col-span-2">Create workflow from a safe template</h3><label>Workflow name<input className={input} required maxLength={100} value={name} onChange={e => setName(e.target.value)} /></label>
        <label>Template<select className={input} value={retryDemo ? "retry" : "observe"} onChange={e => setRetryDemo(e.target.value === "retry")}><option value="observe">Normalize and observe job</option><option value="retry">Delay and transient retry demonstration</option></select></label>
        <label>Source connection<select className={input} required value={connection} onChange={e => { setConnection(e.target.value); setMappingVersions([]); setMappingVersion(""); }}><option value="">Choose a mapped CRM or ATS</option>{connections.filter(c => mappings.some(m => m.connection_id === c.id)).map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
        <label>Pinned mapping version<select className={input} required value={mappingVersion} onChange={e => setMappingVersion(e.target.value)}><option value="">Choose version</option>{mappingVersions.map(v => <option key={v.id} value={v.id}>Version {v.version}</option>)}</select></label>
        <p className="text-xs text-slate-500 sm:col-span-2">Trigger: job.updated. Actions read a synthetic requisition; no ATS synchronization. Configure source mappings on the Mappings page.</p><button className={button} disabled={busy || !mappingVersion}>Create workflow</button>
      </form>}
      <div className="grid gap-3">{workflows.map(w => <button key={w.id} className="rounded-xl border p-4 text-left hover:bg-slate-50" disabled={busy} onClick={() => void action(() => inspectWorkflow(w))}><strong>{w.name}</strong><span className="ml-3 text-xs">{w.status} · v{w.active_version}</span><p className="mt-2 text-sm text-slate-500">{w.trigger_configuration.source} · {w.trigger_configuration.event_types.join(", ")}</p></button>)}</div>{!workflows.length && <p>No workflows yet.</p>}
      {selected && <section className="space-y-4 rounded-xl border p-5"><h3 className="font-semibold">{selected.name} · version history</h3><label>Version<select className={input} value={version?.id ?? ""} onChange={e => setVersion(versions.find(v => v.id === e.target.value) ?? null)}>{versions.map(v => <option key={v.id} value={v.id}>Version {v.version}</option>)}</select></label>
        {version && <><p className="break-all text-xs">Trigger connection: {version.definition.trigger.connection_id} · {version.definition.trigger.event_types.join(", ")} · Event ID prefix: {version.definition.trigger.event_id_prefix ?? "any"}</p><ol className="grid gap-2 sm:grid-cols-2">{version.definition.steps.map((s, i) => <li key={s.id} className="rounded-lg bg-slate-50 p-3 text-sm"><span className="text-slate-400">{i+1} → </span><strong>{s.id}</strong><p className="mt-1 text-xs">{s.type}{s.config.mapping_version_id ? ` · Mapping ${String(s.config.mapping_version_id)}` : ""}{s.config.operation ? ` · ${String(s.config.operation)}` : ""}{s.config.seconds ? ` · ${String(s.config.seconds)} seconds` : ""}</p></li>)}</ol>
        {manage && <div className="flex flex-wrap gap-2"><button className={button} disabled={busy} onClick={() => void action(async () => { const w = await api<Workflow>(`workflows/${selected.id}/activate`, { method: "POST", data: { enabled: true, version_id: version.id } }); await inspectWorkflow(w); setNotice("Version activated"); })}>Activate version</button><button className={button} disabled={busy || selected.status !== "ACTIVE"} onClick={() => void action(async () => { const w = await api<Workflow>(`workflows/${selected.id}/activate`, { method: "POST", data: { enabled: false } }); await inspectWorkflow(w); })}>Deactivate</button><button className={button} disabled={busy} onClick={() => void action(async () => { await api(`workflows/${selected.id}/versions`, { method: "POST", data: { definition: version.definition } }); await inspectWorkflow(selected); setNotice("Immutable version created; activation remains explicit"); })}>Create new version from template</button></div>}</>}
        <Pager offset={versionOffset} total={versionTotal} change={at => void action(() => inspectWorkflow(selected, at))} /></section>}
      {manage && <section className="flex flex-wrap items-center gap-3 rounded-xl border p-4"><label className="text-sm"><input type="checkbox" checked={rescan} onChange={e => setRescan(e.target.checked)} /> Rescan events with no previous match</label><button className={button} disabled={busy} onClick={() => void action(async () => { const r = await api<{ executions_created: number }>("workflow-dispatch", { method: "POST", data: { rescan_no_match: rescan } }); setNotice(`${r.executions_created} execution(s) durably dispatched`); })}>Dispatch accepted events</button></section>}
    </>}
    {mode === "Executions" && <><label>State filter<select className={input} value={filter} onChange={e => { setFilter(e.target.value); setOffset(0); }}><option value="">All states</option>{["PENDING", "RUNNING", "WAITING", "RETRY_SCHEDULED", "SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"].map(s => <option key={s}>{s}</option>)}</select></label><div className="grid gap-3">{executions.map(e => <button className="rounded-xl border p-4 text-left" key={e.id} disabled={busy} onClick={() => void action(() => inspectExecution(e.id))}><strong>{e.state}</strong><p className="mt-2 break-all text-xs">{e.id}</p><p className="mt-1 text-xs text-slate-500">{new Date(e.created_at).toLocaleString()}{e.replay_of_id ? " · Replay execution" : ""}</p></button>)}</div>{!executions.length && <p>No executions match this selection.</p>}</>}
    {mode === "Failures" && <><p className="text-sm text-slate-500">Dead-letter records retain the original event and execution. Inspect a record before requesting replay.</p>{failures.map(f => <button className="block w-full rounded-xl border p-4 text-left" disabled={busy} key={f.id} onClick={() => void action(() => inspectExecution(f.execution_id))}><strong>{f.category}</strong><p className="mt-2 text-sm">{f.step_id} · {f.attempt_count} attempt(s)</p><p className="mt-1 break-all text-xs">Execution {f.execution_id}</p></button>)}{!failures.length && <p>No dead letters.</p>}</>}
    <Pager total={total} offset={offset} change={setOffset} />
    {detail && <section aria-label="Execution details" className="space-y-4 rounded-xl border p-5"><h3 className="font-semibold">Execution timeline · {detail.state}</h3><p className="break-all text-xs">Execution {detail.id}<br />Event {detail.event_id}<br />Root {detail.root_id}{detail.replay_of_id && <><br />Replay of {detail.replay_of_id}</>}</p>{detail.error_category && <p className="text-sm text-red-700">Failure: {detail.error_category}</p>}{detail.cancel_requested && <p>Cancellation requested; committed actions remain recorded.</p>}
      <ol className="space-y-2">{detail.steps?.map(s => <li key={s.id} className="rounded-lg bg-slate-50 p-3 text-sm"><strong>{s.step_id} · {s.state}</strong><p className="mt-1 text-xs">{s.step_type} · {s.attempt_count} attempt(s) · {s.checkpoint_saved ? "Checkpoint saved" : "No completed checkpoint"}{s.error_category && ` · ${s.error_category}`}</p></li>)}</ol>
      <h4 className="text-sm font-semibold">Retry schedule</h4>{retries.map(r => <p key={r.id} className="text-xs">Attempt {r.attempt} · {r.category} · {r.state} · due {new Date(r.available_at).toLocaleString()}</p>)}{!retries.length && <p className="text-xs">No scheduled retries.</p>}<Pager total={retryTotal} offset={retryOffset} change={at => void action(() => inspectExecution(detail.id, historyOffset, at))} />
      <h4 className="text-sm font-semibold">Persisted history</h4>{history.map(h => <p key={h.id} className="text-xs">{new Date(h.created_at).toLocaleString()} · {h.event_type} {h.step_id} {h.category}</p>)}<Pager total={historyTotal} offset={historyOffset} change={at => void action(() => inspectExecution(detail.id, at, retryOffset))} />
      {recover && !["SUCCEEDED", "FAILED", "DEAD_LETTERED", "CANCELLED"].includes(detail.state) && <button className={button} disabled={busy || detail.cancel_requested} onClick={() => void action(async () => { await api(`executions/${detail.id}/cancel`, { method: "POST" }); await inspectExecution(detail.id); })}>Cancel execution</button>}
      {recover && ["FAILED", "DEAD_LETTERED"].includes(detail.state) && <div className="space-y-3">{!confirmReplay ? <button className={button} disabled={busy} onClick={() => setConfirmReplay(true)}>Replay execution</button> : <div role="dialog" aria-label="Confirm replay"><p className="text-sm">Create a traceable replay from saved checkpoints? Completed actions will be retained. Server authorization and receipt checks may deny unsafe replay.</p><button className={button} disabled={busy} onClick={() => void action(async () => { const r = await api<Execution>(`executions/${detail.id}/replay`, { method: "POST", data: { confirmed: true } }); await inspectExecution(r.id); setNotice("Replay created from safe checkpoint"); })}>Confirm replay</button><button className={button} onClick={() => setConfirmReplay(false)}>Keep original</button></div>}</div>}
    </section>}
  </div>;
}
