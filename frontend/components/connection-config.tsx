"use client";
import type { Config, ConnectorType } from "../lib/integrations";
export default function ConnectionConfig({ type, config, onChange, disabled = false }: { type: ConnectorType; config: Config; onChange: (config: Config) => void; disabled?: boolean }) {
  if (type === "generic_rest") return <p className="rounded bg-amber-50 p-3 text-sm text-amber-900">Generic REST is disabled. No destinations, credentials or network execution are accepted.</p>;
  const options = type === "synthetic_slack" ? ["recruiting", "operations", "alerts"] : ["interviews", "recruiting"];
  if (type === "synthetic_slack" || type === "synthetic_calendar") {
    const key = type === "synthetic_slack" ? "allowed_channels" : "allowed_calendars";
    return <fieldset disabled={disabled}><legend className="text-sm font-medium">{type === "synthetic_slack" ? "Allowed channels" : "Allowed calendars"}</legend><div className="mt-2 flex flex-wrap gap-4">{options.map(value => <label key={value} className="text-sm"><input type="checkbox" checked={(config[key] ?? []).includes(value)} onChange={event => onChange({ ...config, [key]: event.target.checked ? [...(config[key] ?? []), value] : (config[key] ?? []).filter(item => item !== value) })} className="mr-2" />{value}</label>)}</div></fieldset>;
  }
  if (type === "synthetic_email") return <label className="block text-sm font-medium">Allowed recipients (comma-separated example.test addresses)<input disabled={disabled} required value={(config.allowed_recipients ?? []).join(", ")} onChange={event => onChange({ ...config, allowed_recipients: event.target.value.split(",").map(value => value.trim()) })} className="mt-2 block w-full rounded border bg-white p-3" /></label>;
  return <label className="block text-sm font-medium">Workspace label<input disabled={disabled} required maxLength={100} value={config.workspace_label ?? ""} onChange={event => onChange({ ...config, workspace_label: event.target.value })} className="mt-2 block w-full rounded border bg-white p-3" /></label>;
}
