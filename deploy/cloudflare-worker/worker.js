// Optional Cloudflare Worker in front of the LLM Router.
//
//   * Streams API traffic through unchanged (SSE passes through).
//   * Caches only PUBLIC liveness endpoints at the edge (/health, /healthz).
//     It must NOT cache /v1/* — those are per-key and cache keys would leak
//     one caller's response to another.
//
// Set ORIGIN (wrangler var) to your deployment, e.g.
//   https://llm-router-tjdk.onrender.com
//
// Note: this adds one network hop to every request. Only route /v1/* through
// it if you specifically want Cloudflare in front (WAF, bot protection). See
// the README for when this is worth it.

const CACHEABLE = new Set(["/health", "/healthz"]);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const origin = env.ORIGIN;
    if (!origin) return new Response("ORIGIN not configured", { status: 500 });

    const cacheable = request.method === "GET" && CACHEABLE.has(url.pathname);
    const cache = caches.default;
    if (cacheable) {
      const hit = await cache.match(request);
      if (hit) return hit;
    }

    const target = new URL(url.pathname + url.search, origin);
    const init = {
      method: request.method,
      headers: request.headers,
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    let upstream;
    try {
      upstream = await fetch(target, init);
    } catch (e) {
      return new Response(JSON.stringify({ error: { message: "origin unavailable" } }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }

    // Pass the body through as a stream (works for SSE).
    const response = new Response(upstream.body, upstream);

    if (cacheable && upstream.ok) {
      const copy = response.clone();
      copy.headers.set("Cache-Control", "public, max-age=15");
      ctx.waitUntil(cache.put(request, copy));
    }
    return response;
  },
};
