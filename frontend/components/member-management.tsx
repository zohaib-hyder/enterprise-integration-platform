"use client";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Invitation, type Member, type Page, type Role } from "../lib/api";
import { useAuth } from "./auth-provider";
const roles: Role[] = ["TENANT_ADMIN", "INTEGRATION_ENGINEER", "OPERATIONS_USER"];
export function RoleBadge({ role }: { role: Role }) { return <span className="rounded-full bg-blue-50 px-2 py-1 text-xs font-semibold text-blue-800">{role.replaceAll("_", " ")}</span>; }
export default function MemberManagement() {
  const { me, refresh } = useAuth();
  const tenant = me?.active_tenant_id;
  const allowed = me?.permissions.includes("members:manage") ?? false;
  const [members, setMembers] = useState<Page<Member> | null>(null);
  const [invitations, setInvitations] = useState<Page<Invitation> | null>(null);
  const [offset, setOffset] = useState(0);
  const [inviteOffset, setInviteOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);
  const [message, setMessage] = useState("");
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!tenant || !allowed) return;
    try {
      const [members, invitations] = await Promise.all([api<Page<Member>>(`tenants/${tenant}/members?offset=${offset}&limit=25`, { signal }), api<Page<Invitation>>(`tenants/${tenant}/invitations?offset=${inviteOffset}&limit=25`, { signal })]);
      if (!signal?.aborted) { setMembers(members); setInvitations(invitations); setError(""); }
    } catch (error) { if (!signal?.aborted) { setDenied(error instanceof ApiError && [401, 403].includes(error.status)); setError(error instanceof ApiError ? error.message : "Members could not be loaded"); } }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [tenant, allowed, offset, inviteOffset]);
  useEffect(() => { const controller = new AbortController(); const task = setTimeout(() => void load(controller.signal), 0); return () => { controller.abort(); clearTimeout(task); }; }, [load]);
  if (!tenant || !allowed || denied) return <section><h1 className="text-3xl font-bold">Member management unavailable</h1><p role="alert" className="mt-4 text-slate-600">An active tenant administrator membership is required.</p></section>;
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); await load(); await refresh(); }
    catch (error) { setError(error instanceof ApiError ? error.message : "Action failed. Please retry."); }
    finally { setBusy(false); }
  }
  async function invite(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); const element = event.currentTarget; const data = new FormData(element);
    await perform(async () => { const created = await api<Invitation>(`tenants/${tenant}/invitations`, { method: "POST", data: { email: data.get("email"), role: data.get("role") } }); element.reset(); setMessage(`Invitation ${created.id} created. Its delivery token is in the private local invitation directory. Ask the local operator to deliver it securely.`); });
  }
  return <section><h1 className="text-3xl font-bold">Members & invitations</h1><p className="mt-3 text-sm text-slate-500">Manage access for {me?.tenants.find(item => item.id === tenant)?.name}. The last active administrator is protected by the API.</p>{error && <p role="alert" className="mt-4 rounded bg-red-50 p-4 text-red-700">{error} <button onClick={() => void load()} className="underline">Retry</button></p>}{message && <p role="status" className="mt-4 rounded bg-emerald-50 p-4 text-sm text-emerald-800">{message}</p>}
    {loading ? <p role="status" className="mt-6">Loading members…</p> : <><div className="mt-6 overflow-x-auto rounded-xl border bg-white"><table className="w-full text-left text-sm"><caption className="sr-only">Tenant members</caption><thead className="bg-slate-50"><tr><th className="p-4">Member</th><th className="p-4">Role</th><th className="p-4">Status</th><th className="p-4">Actions</th></tr></thead><tbody>{members?.items.map(member => <tr key={member.id} className="border-t"><td className="p-4"><p className="font-medium">{member.display_name}</p><p className="text-slate-500">{member.email}</p></td><td className="p-4"><RoleBadge role={member.role} /></td><td className="p-4">{member.status}</td><td className="p-4"><select aria-label={`Role for ${member.display_name}`} disabled={busy} value={member.role} onChange={event => void perform(() => api(`tenants/${tenant}/members/${member.id}`, { method: "PATCH", data: { role: event.target.value } }))} className="rounded border p-2">{roles.map(role => <option key={role}>{role}</option>)}</select><button disabled={busy} className="ml-2 rounded border px-3 py-2" onClick={() => void perform(() => api(`tenants/${tenant}/members/${member.id}`, { method: "PATCH", data: { status: member.status === "ACTIVE" ? "INACTIVE" : "ACTIVE" } }))}>{member.status === "ACTIVE" ? "Deactivate" : "Reactivate"}</button></td></tr>)}</tbody></table>{members?.items.length === 0 && <p className="p-4 text-slate-500">No members on this page.</p>}</div><div className="mt-3 flex items-center gap-4 text-sm"><button disabled={busy || offset === 0} onClick={() => { setLoading(true); setOffset(Math.max(0, offset - 25)); }} className="rounded border p-2 disabled:opacity-40">Previous members</button><span>{members?.total ?? 0} members</span><button disabled={busy || offset + 25 >= (members?.total ?? 0)} onClick={() => { setLoading(true); setOffset(offset + 25); }} className="rounded border p-2 disabled:opacity-40">Next members</button></div></>}
    <h2 className="mt-10 text-xl font-semibold">Invite a member</h2><form onSubmit={invite} className="mt-4 flex flex-wrap items-end gap-3"><label className="text-sm">Email<input name="email" type="email" required maxLength={254} className="mt-2 block rounded border bg-white p-3" /></label><label className="text-sm">Role<select name="role" defaultValue="OPERATIONS_USER" className="mt-2 block rounded border bg-white p-3">{roles.map(role => <option key={role}>{role}</option>)}</select></label><button disabled={busy || loading} className="rounded bg-blue-600 px-4 py-3 text-sm text-white disabled:opacity-50">Create invitation</button></form>
    <h2 className="mt-8 text-xl font-semibold">Invitation history</h2><div className="mt-4 space-y-3">{invitations?.items.map(invitation => <div key={invitation.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-white p-4"><div><p className="text-sm font-medium">{invitation.email}</p><p className="mt-2 text-xs text-slate-500">{invitation.status} · expires {new Date(invitation.expires_at).toLocaleString()}</p><p className="mt-2 break-all text-xs text-slate-400">ID: {invitation.id}</p></div><RoleBadge role={invitation.role} />{invitation.status === "PENDING" && <button disabled={busy} className="rounded border px-3 py-2 text-sm" onClick={() => void perform(() => api(`tenants/${tenant}/invitations/${invitation.id}`, { method: "DELETE" }))}>Revoke invitation</button>}</div>)}{!loading && invitations?.items.length === 0 && <p className="text-sm text-slate-500">No invitations yet.</p>}</div><div className="mt-3 flex items-center gap-4 text-sm"><button disabled={busy || inviteOffset === 0} onClick={() => setInviteOffset(Math.max(0, inviteOffset - 25))} className="rounded border p-2 disabled:opacity-40">Previous invitations</button><span>{invitations?.total ?? 0} invitations</span><button disabled={busy || inviteOffset + 25 >= (invitations?.total ?? 0)} onClick={() => setInviteOffset(inviteOffset + 25)} className="rounded border p-2 disabled:opacity-40">Next invitations</button></div>
  </section>;
}
