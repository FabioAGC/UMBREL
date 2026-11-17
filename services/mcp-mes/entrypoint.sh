#!/bin/sh
# Gera config/config.json a partir das variáveis de ambiente (as credenciais nunca ficam no git)
# e publica o servidor MCP (stdio) como Streamable HTTP em :8000/mcp.
# O token exigido dos clientes vem de SUPERGATEWAY_API_KEY (Authorization: Bearer <token>).
set -eu

: "${MES_BASE_URL:?}" "${MES_TOKEN_URL:?}" "${MES_USERNAME:?}" "${MES_PASSWORD:?}" "${SUPERGATEWAY_API_KEY:?}"

node -e '
const e = process.env;
const url = new URL(e.MES_BASE_URL);
const cfg = {
  baseUrl: e.MES_BASE_URL,
  tokenUrl: e.MES_TOKEN_URL,
  host: e.MES_HOST || url.host,
  clientId: e.MES_CLIENT_ID || "AIM_DefaultManagementClient",
  clientSecret: e.MES_CLIENT_SECRET || "",
  username: e.MES_USERNAME,
  password: e.MES_PASSWORD,
};
require("fs").writeFileSync("/app/config/config.json", JSON.stringify(cfg, null, 2), { mode: 0o600 });
'

exec supergateway \
  --stdio "node /app/server.js" \
  --outputTransport streamableHttp \
  --stateful \
  --port 8000 \
  --streamableHttpPath /mcp \
  --healthEndpoint /healthz \
  --logLevel info
