// Thin fetch wrappers for the two backends.
//   local  -> the developer's local agent daemon   (same origin, /api/*)
//   server -> the server agent                      (same origin or proxied, /api/v1/*, bearer token)

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(url, init);
  const text = await res.text();
  let data: unknown = undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

function jsonInit(method: string, body: unknown, headers: Record<string, string>): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

// ---------------------------------------------------------------- local agent
const LOCAL_HEADERS = { "X-DevOps-Agent": "1" }; // CSRF guard on the daemon

export const local = {
  get: <T>(path: string) => request<T>(`/api${path}`),
  post: <T>(path: string, body?: unknown) => request<T>(`/api${path}`, jsonInit("POST", body ?? {}, LOCAL_HEADERS)),
  del: <T>(path: string) => request<T>(`/api${path}`, { method: "DELETE", headers: LOCAL_HEADERS }),
};

// ---------------------------------------------------------------- server agent
const TOKEN_KEY = "dxa.serverToken";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token lives for this page only */
  }
}

let memoryToken: string | null = null;

function auth(): Record<string, string> {
  const t = memoryToken ?? getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export function setServerToken(token: string | null) {
  memoryToken = token;
}

export const server = {
  get: <T>(path: string) => request<T>(`/api/v1${path}`, { headers: auth() }),
  post: <T>(path: string, body?: unknown) => request<T>(`/api/v1${path}`, jsonInit("POST", body ?? {}, auth())),
  put: <T>(path: string, body?: unknown) => request<T>(`/api/v1${path}`, jsonInit("PUT", body ?? {}, auth())),
};

export async function probe(url: string): Promise<boolean> {
  try {
    const res = await fetch(url, { headers: auth() });
    return res.ok;
  } catch {
    return false;
  }
}
