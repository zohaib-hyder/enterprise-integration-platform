"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
const modules = ["Overview", "Operations", "AI Insights", "Review Queue", "Connections", "Workflows", "Events", "Executions", "Failures", "Mappings", "Synchronization", "Notifications", "Calendar", "Alerts", "Members", "Audit", "Settings"];
type Status = "loading" | "healthy" | "unavailable";
export default function Dashboard({ content, section = "Overview", permissions = [] }: { content?: React.ReactNode; section?: string; permissions?: string[] } = {}) {
  const [status, setStatus] = useState<Status>("loading");
  const [checking, setChecking] = useState(true);
  const check = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await fetch("/api/health", { cache: "no-store", signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(6000)]) : AbortSignal.timeout(6000) });
      const body = await response.json();
      if (!signal?.aborted) setStatus(response.ok && body.status === "healthy" ? "healthy" : "unavailable");
    } catch { if (!signal?.aborted) setStatus("unavailable"); }
    finally { if (!signal?.aborted) setChecking(false); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    const initial = setTimeout(() => void check(controller.signal), 0);
    const timer = setInterval(() => void check(controller.signal), 30000);
    return () => { controller.abort(); clearTimeout(initial); clearInterval(timer); };
  }, [check]);
  return <div className="min-h-screen lg:flex">
    <aside className="bg-[#101e36] px-6 py-8 text-slate-300 lg:fixed lg:inset-y-0 lg:w-64 lg:overflow-y-auto">
      <Link href="/" className="flex items-center gap-3 text-xl font-bold tracking-tight text-white"><span className="rounded-lg bg-blue-500 px-3 py-2">N</span> Northstar</Link>
      <p className="mt-3 text-xs uppercase tracking-[.18em] text-slate-400">Integration workspace</p>
      <nav aria-label="Main navigation" className="mt-10 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-1">
        {modules.map(item => {
          const href = item === "AI Insights" ? "/ai-insights" : item === "Review Queue" ? "/ai-review" : ["Operations", "Workflows", "Executions", "Failures", "Alerts", "Notifications", "Calendar"].includes(item) ? `/${item.toLowerCase()}` : item === "Synchronization" ? "/synchronization" : item === "Mappings" ? "/mappings" : item === "Events" ? "/events" : item === "Connections" ? "/connections" : item === "Overview" ? "/" : item === "Members" && permissions.includes("members:manage") ? "/members" : item === "Audit" && permissions.includes("audit:read") ? "/audit" : item === "Settings" && permissions.includes("tenant:settings") ? "/settings" : null;
          return href ? <Link key={item} href={href} aria-current={item === section ? "page" : undefined} className={`rounded-lg px-4 py-3 text-sm font-semibold ${item === section ? "bg-blue-500/20 text-blue-200" : "text-slate-300"}`}>{item}</Link> : <button key={item} disabled className="flex items-center justify-between rounded-lg px-4 py-3 text-left text-sm text-slate-400"><span>{item}</span><span className="ml-2 text-[10px] text-slate-500">{["Members", "Audit", "Settings"].includes(item) ? "Admin only" : "Coming soon"}</span></button>;
        })}
      </nav>
      <p className="mt-10 border-t border-slate-700 pt-5 text-xs leading-5 text-slate-400">Portfolio environment<br/>Synthetic company · Phase 12</p>
    </aside>
    <main id="main-content" tabIndex={-1} className="min-w-0 flex-1 lg:ml-64">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 bg-white px-6 py-5 md:px-10"><span className="text-sm text-slate-500">Workspace / <strong className="font-medium text-slate-800">{section}</strong></span><span className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600">Foundation preview</span></header>
      <div className="mx-auto max-w-6xl px-6 py-10 md:px-10">{content ?? <>
        <p className="text-xs font-bold uppercase tracking-[.2em] text-blue-600">Operations, connected</p>
        <h1 className="mt-3 text-3xl font-bold tracking-tight md:text-4xl">Workspace overview</h1>
        <p className="mt-4 max-w-2xl text-base leading-7 text-slate-600">A shared foundation for Northstar’s staffing systems. Local connector simulators are available in Connections. The signed event inbox is available in Events. Durable workflows and execution history are available locally.</p>
        <section aria-label="API connectivity" className="mt-8 flex flex-wrap items-center justify-between gap-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div><h2 className="font-semibold">Platform API</h2><p className="mt-2 text-sm text-slate-500">Live health check · refreshed every 30 seconds</p><p className="mt-1 text-xs text-slate-500">API health does not confirm database readiness.</p></div>
          <div className="flex items-center gap-4"><p role="status" aria-live="polite" className={`rounded-full px-3 py-2 text-sm font-medium ${status === "healthy" ? "bg-emerald-50 text-emerald-700" : status === "unavailable" ? "bg-amber-50 text-amber-800" : "bg-slate-100 text-slate-600"}`}>{status === "loading" ? "Checking API…" : status === "healthy" ? "API connected" : "API unavailable"}</p><button disabled={checking} onClick={() => { setChecking(true); void check(); }} className="rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:opacity-50">{checking ? "Checking…" : "Retry"}</button></div>
          {status === "unavailable" && <p className="w-full text-sm text-amber-800">The API could not be reached. Start the backend and check the frontend server configuration, then retry.</p>}
        </section>
        <div className="mt-8 grid gap-6 md:grid-cols-3">{[["01", "Connect your systems", "CRM, ATS, calendar, email and Slack local simulators are available in Connections."], ["02", "Coordinate workflows", "Sequential workflow dispatch, canonical mapping, retries and recovery are available locally."], ["03", "Make work visible", "Inspect persisted execution timelines, worker heartbeats and dead letters."]].map(([number, title, text]) => <section key={number} className="rounded-xl border border-slate-200 bg-white p-6"><span className="text-sm font-semibold text-blue-600">{number} / {"Available locally"}</span><h2 className="mt-5 text-lg font-semibold">{title}</h2><p className="mt-3 text-sm leading-6 text-slate-500">{text}</p></section>)}</div>
        <section className="mt-8 rounded-xl border border-dashed border-slate-300 p-8"><h2 className="text-lg font-semibold">An honest starting point</h2><p className="mt-3 max-w-3xl text-sm leading-6 text-slate-600">The foundation includes user sessions, tenant membership, audit history, durable local connector simulators and a signed event inbox. All providers are synthetic. Execution state and worker status come from PostgreSQL. Unfinished modules are marked coming soon.</p></section>
        <footer className="mt-8 text-xs text-slate-600">Northstar Staffing · Fictional North American staffing company · v0.1.0 · Phase 12</footer>
      </>} </div>
    </main>
  </div>;
}
