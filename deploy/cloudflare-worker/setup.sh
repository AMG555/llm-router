#!/usr/bin/env bash
# Provision a free, durable config store on Cloudflare Workers KV and print the
# env vars to set on the gateway host (e.g. Render).
#
# Requirements: Node + `npx wrangler login` (one-time).
#
#   ./setup.sh [worker-name]
set -euo pipefail
cd "$(dirname "$0")"
name="${1:-llm-router-config-store}"

echo "==> Creating KV namespace CONFIG"
out=$(npx wrangler kv namespace create CONFIG 2>&1) || { echo "$out"; exit 1; }
echo "$out"
id=$(printf '%s' "$out" | grep -oE '[0-9a-f]{32}' | head -1)
if [ -z "${id:-}" ]; then
  echo "!! Could not parse the KV namespace id from the output above."
  echo "   Put it into wrangler.toml manually, then run: npx wrangler deploy"
  exit 1
fi

sed "s/<paste-kv-namespace-id>/$id/" config-store.wrangler.toml.example > wrangler.toml
echo "==> wrangler.toml written (KV id $id)"

tok=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
echo "==> Setting AUTH_TOKEN secret"
printf '%s' "$tok" | npx wrangler secret put AUTH_TOKEN

echo "==> Deploying"
npx wrangler deploy

cat <<EOF

============================================================
Set these three env vars on the gateway host, then redeploy:

PERSIST_URL=https://$name.<your-subdomain>.workers.dev/config
BOOTSTRAP_URL=https://$name.<your-subdomain>.workers.dev/config
PERSIST_TOKEN=$tok

(replace <your-subdomain> with the workers.dev subdomain wrangler printed)
Now config auto-saves on every change and auto-restores on boot.
============================================================
EOF
