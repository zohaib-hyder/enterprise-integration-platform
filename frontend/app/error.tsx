"use client";
export default function Error({ reset }: { reset: () => void }) { return <main className="p-10"><h1 className="text-2xl font-bold">Workspace could not load</h1><button className="mt-4 rounded bg-blue-600 px-4 py-2 text-white" onClick={reset}>Try again</button></main>; }
