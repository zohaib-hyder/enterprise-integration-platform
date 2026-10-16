"use client";
import Link from "next/link";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { useAuth } from "./auth-provider";
export default function AcceptInvitationForm() {
  const { me, loading, refresh } = useAuth();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  if (loading) return <main className="p-10" role="status">Checking account…</main>;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); const element = event.currentTarget; const form = new FormData(element); setBusy(true); setError(""); setMessage("");
    const data = { token: form.get("token"), ...(me ? {} : { display_name: form.get("display_name"), password: form.get("password") }) };
    try { const result = await api<{ message: string }>("invitations/accept", { method: "POST", data }); element.reset(); setMessage(result.message); await refresh(); }
    catch (error) { setError(error instanceof ApiError ? error.message : "Invitation could not be accepted"); }
    finally { setBusy(false); }
  }
  return <main className="mx-auto max-w-xl p-8"><Link href={me ? "/" : "/login"} className="text-sm text-blue-600">{me ? "Back to workspace" : "Sign in to an existing account"}</Link><h1 className="mt-6 text-3xl font-bold">Accept invitation</h1><p className="mt-4 text-sm leading-6 text-slate-600">Use the token delivered by your administrator. Existing accounts must sign in with the invited email before accepting. A new account is activated by possession of this invitation.</p><form className="mt-6 space-y-5" onSubmit={submit}><label className="block">Invitation token<input name="token" type="password" required autoComplete="off" minLength={40} maxLength={128} className="mt-2 block w-full rounded border p-3" /></label>{!me && <><label className="block">Display name<input name="display_name" required maxLength={120} autoComplete="name" className="mt-2 block w-full rounded border p-3" /></label><label className="block">New password<input name="password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" className="mt-2 block w-full rounded border p-3" /></label></>}{me && <p className="text-sm">Accepting as {me.user.email}</p>}{error && <p role="alert" className="text-red-700">{error}</p>}{message && <p role="status" className="text-emerald-700">{message}</p>}<button disabled={busy} className="rounded bg-blue-600 px-4 py-3 text-white disabled:opacity-50">{busy ? "Accepting…" : "Accept invitation"}</button></form></main>;
}
