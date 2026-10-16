import { NextResponse } from "next/server";
export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const base = new URL(process.env.BACKEND_URL ?? "http://127.0.0.1:8000");
    if (!["http:", "https:"].includes(base.protocol) || base.username || base.password) throw new Error("Invalid backend URL");
    const response = await fetch(new URL("/health", base), { cache: "no-store", signal: AbortSignal.timeout(4000), redirect: "error" });
    const body: unknown = await response.json();
    if (!response.ok || typeof body !== "object" || body === null || !("status" in body) || body.status !== "healthy") throw new Error("Unhealthy");
    return NextResponse.json({ status: "healthy" });
  } catch {
    return NextResponse.json({ status: "unavailable" }, { status: 503 });
  }
}
