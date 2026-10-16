"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import { useAuth } from "./auth-provider";
export default function LoginForm() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { refresh } = useAuth();
  const router = useRouter();
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const form = new FormData(event.currentTarget);
    try { await api("auth/login", { method: "POST", data: { email: form.get("email"), password: form.get("password") } }); await refresh(); router.replace("/"); }
    catch (error) { setError(error instanceof ApiError ? error.message : "Sign in is temporarily unavailable"); }
    finally { setBusy(false); }
  }
  return <main className="flex min-h-screen items-center justify-center bg-[#101e36] p-6"><section className="w-full max-w-md rounded-2xl bg-white p-8 shadow-xl"><p className="text-sm font-bold uppercase tracking-widest text-blue-600">Northstar</p><h1 className="mt-4 text-3xl font-bold">Sign in</h1><p className="mt-3 text-sm leading-6 text-slate-500">Access your organization’s integration workspace.</p><form onSubmit={submit} className="mt-6 space-y-5"><label className="block text-sm font-medium">Email<input required type="email" name="email" autoComplete="username" maxLength={254} className="mt-2 block w-full rounded-lg border p-3" /></label><label className="block text-sm font-medium">Password<input required type="password" name="password" autoComplete="current-password" maxLength={1024} className="mt-2 block w-full rounded-lg border p-3" /></label>{error && <p role="alert" className="text-sm text-red-700">{error}</p>}<button disabled={busy} className="w-full rounded-lg bg-blue-600 p-3 font-semibold text-white disabled:opacity-50">{busy ? "Signing in…" : "Sign in"}</button></form><Link href="/accept-invitation" className="mt-6 block text-sm text-blue-600">Accept an invitation</Link><p className="mt-6 text-xs text-slate-400">Synthetic portfolio workspace. First-run accounts are created with the local bootstrap command.</p></section></main>;
}
