import { NextRequest, NextResponse } from "next/server";

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context) {
  const { path } = await context.params;
  if (path[0] !== "v1" || path.some(part => !/^[a-zA-Z0-9_-]+$/.test(part))) {
    return NextResponse.json({ detail: "Invalid API path" }, { status: 404 });
  }
  if (request.method === "POST") {
    // Standalone Next.js rewrites nextUrl's host to its container bind address.
    // The browser-controlled request authority is Host, not that internal URL.
    const host = request.headers.get("host");
    if (!host || !/^[a-zA-Z0-9.:[\]-]+$/.test(host) ||
        request.headers.get("origin") !== `${request.nextUrl.protocol}//${host}`) {
      return NextResponse.json({ detail: "Same-origin request required" }, { status: 403 });
    }
  }
  const key = request.headers.get("x-api-key");
  if (!key || key.length > 4096) return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  if (Number(request.headers.get("content-length") || 0) > 65536) {
    return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  }
  let body: string | undefined;
  if (request.method === "POST" && request.body) {
    const reader = request.body.getReader(); const chunks: Uint8Array[] = []; let bytes = 0;
    while (true) {
      const next = await reader.read(); if (next.done) break;
      bytes += next.value.byteLength;
      if (bytes > 65536) { await reader.cancel(); return NextResponse.json({ detail: "Request too large" }, { status: 413 }); }
      chunks.push(next.value);
    }
    body = Buffer.concat(chunks).toString("utf8");
  }
  try {
    const headers: Record<string, string> = { "X-API-Key": key, "Content-Type": "application/json" };
    const identity = request.headers.get("idempotency-key");
    if (identity) {
      if (!/^[0-9a-f-]{36}$/i.test(identity)) return NextResponse.json({ detail: "Invalid command identity" }, { status: 422 });
      headers["Idempotency-Key"] = identity;
    }
    const upstream = await fetch(`http://api:8001/${path.join("/")}${request.nextUrl.search}`, {
      method: request.method, headers,
      body, cache: "no-store", signal: AbortSignal.timeout(60000), redirect: "error"
    });
    return new Response(upstream.body, { status: upstream.status, headers: {
      "Content-Type": upstream.headers.get("content-type") || "application/octet-stream",
      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      ...(upstream.headers.get("content-disposition") ? { "Content-Disposition": upstream.headers.get("content-disposition")! } : {})
    } });
  } catch {
    return NextResponse.json({ detail: "Control API unavailable" }, { status: 503 });
  }
}

export async function GET(request: NextRequest, context: Context) { return proxy(request, context); }
export async function POST(request: NextRequest, context: Context) { return proxy(request, context); }
