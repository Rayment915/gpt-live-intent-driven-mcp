import { describe, expect, it } from 'vitest';
import { assertStartupUnchanged, changeDelegation, delegationMode, parseSession, prepareSession, responsesUpdate, setAt, validateSession } from './config';
import { object } from './types';

const defaults = {
  model: 'live-deployment', instructions: '前端提示',
  audio: { output: { voice: 'marin' } },
  delegation: {
    type: 'responses',
    responses: {
      model: 'responses-deployment', instructions: '业务规则',
      max_output_tokens: 800, service_tier: 'auto',
      reasoning: { effort: 'low', summary: 'auto', future_setting: 7 },
      text: { verbosity: 'low', format: { type: 'json_object' } },
      tools: [{ type: 'function', name: 'save_customer', parameters: { type: 'object' } }],
      tool_choice: { type: 'function', name: 'save_customer' },
      parallel_tool_calls: false,
      future_response_parameter: { enabled: true },
    },
  },
};

describe('parameters round-trip', () => {
  it('preserves unknown Responses fields and server tools through form edits and JSON', () => {
    const edited = setAt(defaults, ['delegation', 'responses', 'reasoning', 'effort'], 'high');
    const parsed = parseSession(JSON.stringify(edited));
    const update = responsesUpdate(parsed);
    const responses = object(object(object(update.session).delegation).responses);
    expect(responses).toEqual({ ...defaults.delegation.responses, reasoning: { ...defaults.delegation.responses.reasoning, effort: 'high' } });
    expect(update).not.toHaveProperty('session.model');
    expect(update).not.toHaveProperty('session.audio');
    expect(defaults.delegation.responses.reasoning.effort).toBe('low');
  });

  it('retains the four distinct startup delegation representations', () => {
    const omitted = changeDelegation(defaults, 'omitted', defaults);
    expect(JSON.stringify(omitted)).not.toContain('"delegation"');
    expect(delegationMode(omitted)).toBe('omitted');
    expect(changeDelegation(defaults, 'null', defaults).delegation).toBeNull();
    expect(changeDelegation(defaults, 'client', defaults).delegation).toEqual({ type: 'client' });
    const restored = changeDelegation(omitted, 'responses', defaults);
    expect(restored.delegation).toEqual(defaults.delegation);
    expect(restored.delegation).not.toBe(defaults.delegation);
  });

  it('locks startup fields even when editing raw JSON, but accepts reordered keys', () => {
    const reordered = { ...defaults, audio: { output: { voice: 'marin' } } };
    expect(() => assertStartupUnchanged(reordered, defaults)).not.toThrow();
    expect(() => assertStartupUnchanged(setAt(defaults, ['audio', 'output', 'voice'], 'cedar'), defaults)).toThrow('启动参数已锁定');
    expect(() => assertStartupUnchanged(setAt(defaults, ['instructions'], 'new prompt'), defaults)).toThrow();
    expect(() => assertStartupUnchanged(setAt(defaults, ['delegation', 'responses', 'max_output_tokens'], 4096), defaults)).not.toThrow();
    expect(() => responsesUpdate(changeDelegation(defaults, 'client', defaults))).toThrow();
  });

  it('leaves audio encoding to the Studio transport instead of injecting unsupported schema fields', () => {
    expect(prepareSession(defaults, 'webrtc').audio).toEqual(defaults.audio);
    expect(prepareSession(defaults, 'websocket').audio).toEqual(defaults.audio);
    const wrongFormat = setAt(defaults, ['audio', 'format'], { type: 'audio/pcmu', rate: 8000 });
    expect(() => prepareSession(wrongFormat, 'websocket')).toThrow('请移除');
    expect(() => prepareSession(setAt(defaults, ['audio', 'format'], { type: 'audio/pcm', rate: 24000 }), 'websocket')).toThrow('不接收');
    expect(() => prepareSession(wrongFormat, 'webrtc')).toThrow('移除');
    expect(defaults.audio).not.toHaveProperty('format');
  });

  it('rejects malformed configuration instead of falling back silently', () => {
    expect(() => parseSession('[]')).toThrow();
    expect(() => parseSession('{')).toThrow();
    expect(() => validateSession(setAt(defaults, ['delegation', 'responses', 'tools'], {}), 'webrtc')).toThrow('数组');
    expect(() => validateSession(setAt(defaults, ['delegation', 'responses', 'max_output_tokens'], -1), 'webrtc')).toThrow('正整数');
    expect(() => setAt({}, ['__proto__', 'polluted'], true)).toThrow();
  });
});
