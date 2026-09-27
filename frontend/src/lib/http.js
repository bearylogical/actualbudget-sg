// Parse a fetch Response as JSON, turning every failure into a readable Error.
// Covers: FastAPI errors ({detail}), bridge errors ({error}), nginx 502/504 HTML
// pages and plain-text 500s — which used to surface as "JSON.parse: unexpected character".
export async function readJson(res) {
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch { /* not JSON */ }
  if (res.ok && data !== null) return data;
  const detail = data?.detail || data?.error;
  if (detail) throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  if (res.status === 502 || res.status === 503 || text.trim().startsWith('<')) {
    throw new Error(`${res.status} ${res.statusText || ''} — the backend isn't responding. Check \`docker compose ps\` / \`docker compose logs backend actual-bridge\`.`.trim());
  }
  throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 160) || 'empty response'}`);
}
