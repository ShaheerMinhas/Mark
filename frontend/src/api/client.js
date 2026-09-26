const TOKEN_KEY = "mark_token";
const USER_KEY = "mark_user";
const API_BASE = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
export const AUTH_EXPIRED_EVENT = "mark:auth-expired";

export function getToken() {
  const raw = localStorage.getItem(TOKEN_KEY);
  if (!raw) return null;
  let token = raw.trim();
  if (
    (token.startsWith('"') && token.endsWith('"')) ||
    (token.startsWith("'") && token.endsWith("'"))
  ) {
    try {
      token = JSON.parse(token);
    } catch {
      token = token.slice(1, -1);
    }
  }
  if (typeof token !== "string") return null;
  token = token.trim();
  if (token.toLowerCase().startsWith("bearer ")) token = token.slice(7).trim();
  return token || null;
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

function errorMessage(data, status) {
  let msg = `Server error (${status})`;
  if (data && typeof data === "object") {
    if (Array.isArray(data.detail)) {
      msg = data.detail.map((d) => d.msg || String(d)).join(". ");
    } else if (data.detail) {
      msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } else if (data.message) {
      msg = data.message;
    }
  }
  return msg;
}

function isAuthFailure(status, message, path) {
  if (status !== 401) return false;
  if (String(path).includes("/api/auth/login")) return false;
  return /invalid token|not authenticated|user not found|session expired/i.test(message);
}

function expireSession() {
  clearSession();
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
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
    const msg = errorMessage(data, res.status);
    if (isAuthFailure(res.status, msg, path)) {
      expireSession();
      throw new Error("Your session expired. Please sign in again.");
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
  if (res.status === 401) {
    expireSession();
    throw new Error("Your session expired. Please sign in again.");
  }
  if (!res.ok) throw new Error(`Export failed (${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `mark-leads-${searchId}.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
