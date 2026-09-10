const TOKEN_KEY = "mark_token";
const API_BASE = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

export async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let body = opts.body;
  if (body && typeof body === "object" && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }

  const res = await fetch(apiUrl(path), { ...opts, headers, body });
  const isJson = res.headers.get("content-type")?.includes("json");
  const data = isJson ? await res.json() : await res.text();

  if (!res.ok) {
    let msg = `Server error (${res.status})`;
    if (isJson) {
      if (Array.isArray(data.detail)) {
        msg = data.detail.map((d) => d.msg || String(d)).join(". ");
      } else if (data.detail) {
        msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } else if (data.message) {
        msg = data.message;
      }
    }
    throw new Error(msg);
  }

  return data;
}

export async function downloadExport(searchId, format) {
  const token = getToken();
  const res = await fetch(apiUrl(`/api/searches/${searchId}/export?format=${format}`), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`Export failed (${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `mark-leads-${searchId}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
