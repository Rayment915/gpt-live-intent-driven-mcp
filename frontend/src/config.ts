import { object, type JsonObject, type Transport } from './types';

export type DelegationMode = 'omitted' | 'null' | 'client' | 'responses';

export function at(root: unknown, path: string[]): unknown {
  return path.reduce<unknown>((current, key) => object(current)[key], root);
}

export function setAt(root: JsonObject, path: string[], value: unknown): JsonObject {
  const [key, ...tail] = path;
  if (!key || ['__proto__', 'prototype', 'constructor'].includes(key)) throw new Error('参数路径无效');
  const next = { ...root };
  if (tail.length) next[key] = setAt(object(root[key]), tail, value);
  else if (value === undefined) delete next[key];
  else next[key] = value;
  return next;
}

export function delegationMode(session: JsonObject): DelegationMode {
  if (!Object.hasOwn(session, 'delegation')) return 'omitted';
  if (session.delegation === null) return 'null';
  return object(session.delegation).type === 'responses' ? 'responses' : 'client';
}

export function changeDelegation(session: JsonObject, mode: DelegationMode, defaults: JsonObject): JsonObject {
  if (mode === 'omitted') return setAt(session, ['delegation'], undefined);
  if (mode === 'null') return { ...session, delegation: null };
  if (mode === 'client') return { ...session, delegation: { type: 'client' } };
  const current = object(object(session.delegation).responses);
  const responses = Object.keys(current).length ? current : object(object(defaults.delegation).responses);
  return { ...session, delegation: { type: 'responses', responses: structuredClone(responses) } };
}

export function parseSession(text: string): JsonObject {
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('会话 JSON 必须是对象');
  return parsed as JsonObject;
}

export function validateSession(session: JsonObject, transport: Transport): void {
  if (typeof session.model !== 'string' || !session.model.trim()) throw new Error('请填写 Live 模型部署名');
  if (session.instructions !== undefined && session.instructions !== null && typeof session.instructions !== 'string') throw new Error('instructions 必须为字符串或 null');
  const audio = object(session.audio);
  if (audio.format !== undefined) throw new Error(transport === 'webrtc'
    ? 'WebRTC 通过 SDP 协商音频格式；请移除 audio.format'
    : '请移除 audio.format：Studio 创建接口不接收该字段，WebSocket 音频由服务端固定为 PCM16LE · 24 kHz · mono');
  if (session.delegation !== undefined && session.delegation !== null) {
    const delegation = object(session.delegation);
    if (!['client', 'responses'].includes(String(delegation.type))) throw new Error('请选择 client 或 responses 委派，或省略 / null');
    if (delegation.type === 'responses') {
      const responses = object(delegation.responses);
      if (typeof responses.model !== 'string' || !responses.model.trim()) throw new Error('请填写 Responses 模型部署名');
      if (responses.tools !== undefined && !Array.isArray(responses.tools)) throw new Error('Responses tools 必须为 JSON 数组');
      if (responses.max_output_tokens !== undefined && (!Number.isInteger(responses.max_output_tokens) || Number(responses.max_output_tokens) <= 0)) throw new Error('max_output_tokens 必须为正整数');
      if (responses.parallel_tool_calls !== undefined && typeof responses.parallel_tool_calls !== 'boolean') throw new Error('parallel_tool_calls 必须为布尔值');
      if (responses.tool_choice !== undefined && typeof responses.tool_choice !== 'string' && !Object.keys(object(responses.tool_choice)).length) throw new Error('tool_choice 必须为字符串或非空对象');
    }
  }
}

export function prepareSession(session: JsonObject, transport: Transport): JsonObject {
  validateSession(session, transport);
  return structuredClone(session);
}

/** Updates deliberately include the entire Responses object, never startup-only fields. */
export function responsesUpdate(session: JsonObject): JsonObject {
  if (delegationMode(session) !== 'responses') throw new Error('运行中仅支持更新 Responses 委派参数');
  const responses = object(object(session.delegation).responses);
  if (!Object.keys(responses).length) throw new Error('Responses 参数不能为空');
  return { type: 'session.update', session: { delegation: { type: 'responses', responses: structuredClone(responses) } } };
}

export function assertStartupUnchanged(candidate: JsonObject, original: JsonObject): void {
  const withoutResponses = (session: JsonObject) => {
    const copy = structuredClone(session);
    if (object(copy.delegation).type === 'responses') {
      copy.delegation = { ...object(copy.delegation), responses: null };
    }
    return copy;
  };
  if (stableJson(withoutResponses(candidate)) !== stableJson(withoutResponses(original))) {
    throw new Error('运行中仅能修改 delegation.responses；模型、音色、初始提示和其他启动参数已锁定');
  }
}

export function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value !== null && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${stableJson(v)}`).join(',')}}`;
  return JSON.stringify(value);
}
