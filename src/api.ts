type ApiRequestOptions = {
  method?: string;
  token?: string | null;
  body?: unknown;
  query?: Record<string, string | number | null | undefined>;
};

const BACKEND_PROXY_TARGET = "http://127.0.0.1:3001";

function buildQueryString(query?: ApiRequestOptions["query"]): string {
  if (!query) {
    return "";
  }

  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    params.set(key, String(value));
  });

  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

function buildBackendUnavailableMessage(path: string, reason?: string) {
  const suffix = reason ? ` (${reason})` : "";
  return `Unable to reach the backend API at ${path}. Check the backend service listening on ${BACKEND_PROXY_TARGET}.${suffix}`;
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${path}${buildQueryString(options.query)}`, {
      method: options.method || "GET",
      headers: {
        "Content-Type": "application/json",
        ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (error) {
    const reason = error instanceof Error && error.message ? error.message : undefined;
    throw new Error(buildBackendUnavailableMessage(path, reason));
  }

  if (!response.ok) {
    if (response.status === 502 || response.status === 503) {
      throw new Error(buildBackendUnavailableMessage(path));
    }

    let message = `Request failed with ${response.status}`;
    const rawBody = await response.text();
    if (rawBody.trim()) {
      try {
        const payload = JSON.parse(rawBody) as { detail?: string };
        if (payload.detail) {
          message = payload.detail;
        }
      } catch {
        message = rawBody.trim();
      }
    }
    throw new Error(message);
  }

  // 204 responses (e.g. a delete) carry no body, so there is nothing to parse.
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
