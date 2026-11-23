import { describe, expect, it } from 'vitest';
import { appendBounded, debugFrame, groupTranscripts, nestedEvent, redacted, spanDuration, transcriptFragment } from './protocol';
import { initialData, receiveFrame } from './state';
import type { Span } from './types';

const span: Span = { id: 'tool-1', trace_id: 'trace-1', name: 'save_customer', kind: 'tool', start_ms: 100, status: 'running', attributes: {} };

describe('shared session clock', () => {
  it('uses end timestamps for completed spans and grows only running spans', () => {
    expect(spanDuration(span, 600)).toBe(500);
    expect(spanDuration({ ...span, duration_ms: 0 }, 600)).toBe(500);
    const completed = { ...span, status: 'completed' as const, end_ms: 350, duration_ms: 999 };
    expect(spanDuration(completed, 100000)).toBe(250);
    expect(spanDuration({ ...span, status: 'failed', duration_ms: 25 }, 100000)).toBe(25);
    expect(spanDuration({ ...span, status: 'cancelled' }, 100000)).toBe(0);
    expect(spanDuration({ ...span, end_ms: 50 }, 100000)).toBe(0);
  });

  it('upserts spans without losing previously received attributes', () => {
    let data = receiveFrame(initialData(), { type: 'studio.span', ...span, attributes: { delegation_id: 'd1' } });
    data = receiveFrame(data, { type: 'studio.span', ...span, status: 'completed', end_ms: 450, attributes: { bytes: 42 } });
    expect(data.spans).toHaveLength(1);
    expect(data.spans[0].attributes).toEqual({ delegation_id: 'd1', bytes: 42 });
    expect(spanDuration(data.spans[0], 2000)).toBe(350);
  });
});

describe('Live event normalization and bounds', () => {
  it('dispatches nested response.event and retains delegation association', () => {
    const frame = { type: 'response.event', delegation_id: 'd1', event: { type: 'response.output_text.delta', delta: '正在处理', item_id: 'r1' } };
    expect(nestedEvent(frame)).toMatchObject({ type: 'response.output_text.delta', delegation_id: 'd1' });
    expect(transcriptFragment(frame, 1)).toMatchObject({ role: 'response', text: '正在处理' });
    expect(transcriptFragment({ type: 'response.function_call_arguments.delta', delta: '{"name":' }, 2)).toBeUndefined();
  });

  it('keeps fragment timing and gap boundaries without inventing turn-done events', () => {
    const first = transcriptFragment({ type: 'session.input_transcript.delta', delta: '你', start_ms: 0, end_ms: 100 }, 1)!;
    const second = transcriptFragment({ type: 'session.input_transcript.delta', delta: '好', start_ms: 100, end_ms: 200 }, 2)!;
    const third = transcriptFragment({ type: 'session.input_transcript.delta', delta: '下一句', start_ms: 2500, end_ms: 3000 }, 3)!;
    const grouped = groupTranscripts([first, second, third]);
    expect(grouped.map(item => item.text)).toEqual(['你好', '下一句']);
    expect(grouped[0]).toMatchObject({ start_ms: 0, end_ms: 200 });
    expect(first.text).toBe('你');
  });

  it('bounds rings and replaces cumulative usage snapshots instead of summing them', () => {
    expect(appendBounded([1, 2, 3], 4, 3)).toEqual([2, 3, 4]);
    let data = receiveFrame(initialData(), { type: 'session.usage.updated', usage: { audio_seconds: 3 } });
    data = receiveFrame(data, { type: 'session.usage.updated', usage: { audio_seconds: 5 } });
    expect(data.usage.audio_seconds).toBe(5);
    expect(data.finalConfirmed).toBe(false);
    data = receiveFrame(data, { type: 'session.closed', usage: { audio_seconds: 7 } });
    expect(data.usage.audio_seconds).toBe(7);
    expect(data.finalConfirmed).toBe(true);
    data = receiveFrame(data, { type: 'studio.disconnected', final_usage_confirmed: false });
    expect(data.finalConfirmed).toBe(true);
  });

  it('does not treat automatically allowing a tool as successful MCP execution', () => {
    const data = receiveFrame(initialData(), { type: 'studio.tool.auto_approved', call_id: 'c1', name: 'createRepair', mode: 'auto_mock' });
    expect(data.calls).toEqual([]);
    expect(data.spans).toEqual([]);
    const completed = receiveFrame(data, { type: 'studio.tools', calls: [{ callId: 'c1', name: 'createRepair', arguments: '[redacted]', state: 'submitted' }] });
    expect(completed.calls[0].state).toBe('submitted');
  });

  it('tracks dynamic tool phases independently of startup and effective response settings', () => {
    const data = receiveFrame(initialData(), {
      type: 'studio.workflow', phase: 'executing', active_delegation: 'd1',
      queued: ['d2'], visible_tools: ['classify_intent', 'faqSearch'], tool_choice: 'auto', config_version: 2,
      catalogs: [{ server: 'faq', state: 'valid', version: 'v1', expires_in_seconds: 300, tools: ['faqSearch'] }],
    });
    expect(data.workflow?.queued).toEqual(['d2']);
    expect(data.workflow?.visible_tools).toEqual(['classify_intent', 'faqSearch']);
    const updated = receiveFrame(data, { type: 'session.updated', session: {
      delegation: { type: 'responses', responses: { tools: [{ name: 'faqSearch' }], tool_choice: 'auto' } },
    } });
    expect(updated.effectiveResponses.tool_choice).toBe('auto');
    const measured = receiveFrame(updated, { type: 'studio.browser_startup', stage: '创建会话 HTTP', duration_ms: 123, status: 'completed' });
    expect(measured.startup[0].duration_ms).toBe(123);
    expect(measured.workflow?.config_version).toBe(2);
  });

  it('keeps PCM, transcripts and nested tool outputs out of the protocol console', () => {
    const audio = { type: 'response.event', event: { type: 'response.output_audio.delta', delta: 'AAAA' } };
    expect(JSON.stringify(debugFrame(audio))).not.toContain('AAAA');
    const transcript = { type: 'session.output_transcript.delta', delta: 'hello' };
    expect(debugFrame(transcript).delta).not.toBe('hello');
    expect(debugFrame({ type: 'session.input_audio.append', audio: 'BBBB' }).audio).not.toBe('BBBB');
    expect(JSON.stringify(debugFrame({ type: 'response.event', event: { type: 'response.output_item.done', item: { type: 'function_call_output', output: 'PRIVATE_RESULT' } } }))).not.toContain('PRIVATE_RESULT');
    expect(transcriptFragment(transcript, 1)?.text).toBe('hello');
  });

  it('redacts prompts, personal data, credentials and arbitrary trace strings on export', () => {
    const serialized = JSON.stringify(redacted({
      type: 'studio.span', name: 'save_customer', start_ms: 17,
      arguments: { name: 'PRIVATE_PERSON' }, audio: 'AUDIO_DATA', sdp: 'SDP_DATA',
      csrf: 'CSRF_SECRET', instructions: 'PRIVATE_PROMPT', password: 'PASSWORD',
      attributes: { name: 'PRIVATE_ATTRIBUTE', nested: { content: 'PRIVATE_CONTENT' }, duration_ms: 30 },
    }));
    for (const secret of ['PRIVATE_PERSON', 'AUDIO_DATA', 'SDP_DATA', 'CSRF_SECRET', 'PRIVATE_PROMPT', 'PRIVATE_ATTRIBUTE', 'PRIVATE_CONTENT', 'PASSWORD']) expect(serialized).not.toContain(secret);
    expect(serialized).toContain('save_customer');
    expect(serialized).toContain('"duration_ms":30');
  });
});
