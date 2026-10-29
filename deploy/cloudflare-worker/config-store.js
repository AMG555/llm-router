// Durable, free config store backed by Workers KV.
//
// Purpose: give the router a PERSIST_URL / BOOTSTRAP_URL that survives the
// ephemeral disk on hosts like Render free.
//
//   PUT /config   (Authorization: Bearer <AUTH_TOKEN>)  -> store the JSON body
//   GET /config   (Authorization: Bearer <AUTH_TOKEN>)  -> return the JSON
//
// The stored payload contains provider API keys, so BOTH verbs require the
// bearer token. Create the KV namespace and set the secret per the README.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/config") {
      return new Response(JSON.stringify({ error: "not found" }), {
        status: 404, headers: { "content-type": "application/json" },
      });
    }
    const auth = request.headers.get("authorization") || "";
    if (!env.AUTH_TOKEN || auth !== `Bearer ${env.AUTH_TOKEN}`) {
      return new Response(JSON.stringify({ error: "unauthorized" }), {
        status: 401, headers: { "content-type": "application/json" },
      });
    }
    if (request.method === "GET") {
      const v = await env.CONFIG.get("config");
      if (!v) return new Response("null", { status: 404, headers: { "content-type": "application/json" } });
      return new Response(v, { headers: { "content-type": "application/json" } });
    }
    if (request.method === "PUT") {
      const body = await request.text();
      try { JSON.parse(body); } catch (e) { return new Response("invalid json", { status: 400 }); }
      await env.CONFIG.put("config", body);
      return new Response(JSON.stringify({ ok: true }), {
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("method not allowed", { status: 405 });
  },
};
