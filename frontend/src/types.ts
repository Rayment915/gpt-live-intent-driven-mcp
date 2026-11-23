export type JsonObject = Record<string, unknown>;
export type Transport = 'webrtc' | 'websocket';
export type Phase = 'idle' | 'connecting' | 'live' | 'closing' | 'closed' | 'error';

export interface StudioConfig {
  csrf: string;
  configured: boolean;
  endpoint: string;
  deployments: { live: string; responses: string; decision: string };
  defaultConfig: JsonObject;
  mcp: { name: string; url: string; configured: boolean; tools: unknown[] }[];
  threshold: number;
  maxSessionSeconds: number;
  managed_tools?: boolean;
  catalogs?: CatalogStatus[];
}

export interface CatalogStatus {
  server: string;
  state: string;
  version: string | null;
  expires_in_seconds: number;
  tools: string[];
}

export interface WorkflowState {
  phase: string;
  active_delegation: string | null;
  queued: string[];
  visible_tools: string[];
  tool_choice: string;
  config_version: number;
}

export interface StartupTiming {
  stage: string;
  duration_ms: number;
  status: 'completed' | 'failed';
}

export interface Span {
  id: string;
  trace_id: string;
  parent_id?: string;
  name: string;
  kind: string;
  start_ms: number;
  end_ms?: number;
  duration_ms?: number;
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  attributes: JsonObject;
}

export interface Route {
  delegation_id: string;
  intent: string;
  confidence: number;
  top_probability?: number;
  probabilities: unknown;
  allowed_tools: string[];
}

export interface ToolCall {
  callId: string;
  name: string;
  arguments: unknown;
  state: string;
  delegationId?: string;
}

export interface TranscriptFragment {
  id: number;
  role: 'user' | 'assistant' | 'response';
  text: string;
  start_ms?: number;
  end_ms?: number;
  key: string;
}

export interface DebugEvent {
  seq: number;
  received: number;
  direction: 'in' | 'out' | 'local';
  type: string;
  frame: JsonObject;
}

export function object(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
