import { Account, AgentTemplate, Artifact, AvailableProfile, DiscoveredModel, Run, WorkflowConfig } from "./types";

let sessionToken: string | null = null;

export async function initSession(): Promise<string> {
  if (sessionToken) return sessionToken;
  try {
    const res = await fetch("/api/session");
    if (res.ok) {
      const data = await res.json();
      sessionToken = data.session_token;
      return sessionToken || "";
    }
  } catch (err) {
    console.warn("Could not handshake session token", err);
  }
  return "";
}

function generateIdempotencyKey(): string {
  return "key_" + Math.random().toString(36).substring(2, 15) + "_" + Date.now();
}

async function request(path: string, options: RequestInit = {}): Promise<any> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});

  if (!headers.has("Content-Type") && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  // Mutating requests require session token & idempotency key
  if (["POST", "PATCH", "PUT", "DELETE"].includes(method)) {
    if (!sessionToken) {
      await initSession();
    }
    if (sessionToken) {
      headers.set("X-Council-Session", sessionToken);
    }
    if (!headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", generateIdempotencyKey());
    }
  }

  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let errorDetail = res.statusText;
    try {
      const errJson = await res.json();
      errorDetail = errJson.detail || JSON.stringify(errJson);
    } catch (_) {}
    throw new Error(errorDetail);
  }

  if (res.status === 204) return null;
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res;
}

// ---------------------------------------------------------------------------
// Accounts & Profiles API
// ---------------------------------------------------------------------------
export async function getAccounts(enabledOnly = false): Promise<Account[]> {
  return request(`/api/accounts?enabled_only=${enabledOnly}`);
}

export async function getAvailableProfiles(): Promise<AvailableProfile[]> {
  return request("/api/profiles/available");
}

export async function linkAllProfiles(): Promise<{ linked_count: number; linked_profiles: string[]; message: string }> {
  return request("/api/accounts/link-all", { method: "POST" });
}

export async function getModels(): Promise<DiscoveredModel[]> {
  return request("/api/models");
}

export async function createAccount(data: { profile_ref: string; label: string; provider?: string; concurrency_limit?: number }): Promise<Account> {
  return request("/api/accounts", { method: "POST", body: JSON.stringify(data) });
}

export async function updateAccount(id: string, data: { label?: string; enabled?: boolean; concurrency_limit?: number }): Promise<Account> {
  return request(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify(data) });
}

export async function deleteAccount(id: string): Promise<any> {
  return request(`/api/accounts/${id}`, { method: "DELETE" });
}

export async function connectAccount(id: string): Promise<{ status: string; command: string; message: string }> {
  return request(`/api/accounts/${id}/connect`, { method: "POST" });
}

export async function checkAccount(id: string): Promise<any> {
  return request(`/api/accounts/${id}/check`, { method: "POST" });
}

export async function getAccountModels(id: string): Promise<DiscoveredModel[]> {
  return request(`/api/accounts/${id}/models`);
}

// ---------------------------------------------------------------------------
// Agents API
// ---------------------------------------------------------------------------
export async function getAgents(): Promise<AgentTemplate[]> {
  return request("/api/agents");
}

export async function createAgent(data: Partial<AgentTemplate>): Promise<AgentTemplate> {
  return request("/api/agents", { method: "POST", body: JSON.stringify(data) });
}

export async function updateAgent(id: string, data: Partial<AgentTemplate>): Promise<AgentTemplate> {
  return request(`/api/agents/${id}`, { method: "PATCH", body: JSON.stringify(data) });
}

export async function deleteAgent(id: string): Promise<any> {
  return request(`/api/agents/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Presets & Workflows API
// ---------------------------------------------------------------------------
export async function getPresets(): Promise<any[]> {
  return request("/api/presets");
}

export async function getPreset(id: string): Promise<any> {
  return request(`/api/presets/${id}`);
}

export async function getWorkflows(): Promise<any[]> {
  return request("/api/workflows");
}

export async function validateWorkflow(def: any): Promise<{ valid: boolean; errors: string[] }> {
  return request("/api/workflows/validate", { method: "POST", body: JSON.stringify(def) });
}

export async function createWorkflow(data: { name: string; goal: string; definition: any; description?: string }): Promise<any> {
  return request("/api/workflows", { method: "POST", body: JSON.stringify(data) });
}

export async function importWorkflow(data: { definition: any; name?: string; goal?: string }): Promise<any> {
  return request("/api/workflows/import", { method: "POST", body: JSON.stringify(data) });
}

export async function exportWorkflowTemplate(workflowId: string): Promise<any> {
  return request(`/api/workflows/${workflowId}/export`);
}

// ---------------------------------------------------------------------------
// Runs API
// ---------------------------------------------------------------------------
export async function getRuns(): Promise<Run[]> {
  return request("/api/runs");
}

export async function getRun(id: string): Promise<Run> {
  return request(`/api/runs/${id}`);
}

export async function createRun(data: {
  preset_id?: string;
  workflow_id?: string;
  workflow?: any;
  name?: string;
  goal?: string;
  inputs?: Record<string, string>;
  account_bindings?: Record<string, string>;
  model_bindings?: Record<string, string>;
}): Promise<Run> {
  return request("/api/runs", { method: "POST", body: JSON.stringify(data) });
}

export async function startRun(id: string): Promise<any> {
  return request(`/api/runs/${id}/start`, { method: "POST" });
}

export async function pauseRun(id: string): Promise<any> {
  return request(`/api/runs/${id}/pause`, { method: "POST" });
}

export async function resumeRun(id: string): Promise<any> {
  return request(`/api/runs/${id}/resume`, { method: "POST" });
}

export async function stopRun(id: string): Promise<any> {
  return request(`/api/runs/${id}/stop`, { method: "POST" });
}

export async function resolveRun(id: string, action = "retry", note = ""): Promise<any> {
  return request(`/api/runs/${id}/resolve`, { method: "POST", body: JSON.stringify({ action, resolution_note: note }) });
}

export async function getRunArtifacts(id: string): Promise<Artifact[]> {
  return request(`/api/runs/${id}/artifacts`);
}

export function getArtifactDownloadUrl(artifactId: string): string {
  return `/api/artifacts/${artifactId}`;
}

export async function exportRunZip(id: string, redact = true): Promise<Blob> {
  if (!sessionToken) await initSession();
  const res = await fetch(`/api/runs/${id}/export?redact=${redact}`, {
    method: "POST",
    headers: {
      "X-Council-Session": sessionToken || "",
      "Idempotency-Key": generateIdempotencyKey(),
    },
  });
  if (!res.ok) throw new Error("Export failed: " + res.statusText);
  return res.blob();
}
