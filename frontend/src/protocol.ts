import { object, type JsonObject, type Span, type TranscriptFragment } from './types';

export function nestedEvent(frame: JsonObject): JsonObject {
  if (frame.type === 'response.event' && typeof frame.event === 'object') return { ...object(frame.event), delegation_id: frame.delegation_id ?? object(frame.event).delegation_id };
  if (object(frame.response).event) return { ...object(object(frame.response).event), delegation_id: frame.delegation_id };
  return frame;
}

export function eventTime(frame: JsonObject): number | undefined {
  const event = nestedEvent(frame);
  const candidates = [frame.end_ms, frame.start_ms, event.end_ms, event.start_ms];
  return candidates.find(value => typeof value === 'number' && Number.isFinite(value)) as number | undefined;
}

export function spanDuration(span: Span, now: number): number {
  if (Number.isFinite(span.end_ms)) return Math.max(0, span.end_ms! - span.start_ms);
  if (span.status === 'running') return Math.max(0, span.duration_ms ?? 0, now - span.start_ms);
  if (Number.isFinite(span.duration_ms)) return Math.max(0, span.duration_ms!);
  return 0;
}

export function mergeSpan(previous: Span | undefined, incoming: Span): Span {
  return { ...previous, ...incoming, attributes: { ...previous?.attributes, ...incoming.attributes } };
}

export function transcriptFragment(frame: JsonObject, id: number): TranscriptFragment | undefined {
  const event = nestedEvent(frame);
  const type = String(event.type ?? '');
  const isTranscript = /transcript.*delta$|transcription.*delta$/.test(type);
  const isResponseText = type === 'response.output_text.delta';
  if ((!isTranscript && !isResponseText) || typeof event.delta !== 'string') return undefined;
  const role = /input|user/.test(type) ? 'user' : isResponseText || frame.type === 'response.event' ? 'response' : 'assistant';
  return {
    id, role, text: event.delta,
    key: `${role}:${event.item_id ?? event.response_id ?? event.delegation_id ?? ''}`,
    ...(typeof event.start_ms === 'number' ? { start_ms: event.start_ms } : {}),
    ...(typeof event.end_ms === 'number' ? { end_ms: event.end_ms } : {}),
  };
}

export function groupTranscripts(fragments: TranscriptFragment[]): TranscriptFragment[] {
  const groups: TranscriptFragment[] = [];
  for (const fragment of fragments) {
    const previous = groups.at(-1);
    const gap = previous?.end_ms !== undefined && fragment.start_ms !== undefined ? fragment.start_ms - previous.end_ms : 0;
    if (previous?.key === fragment.key && gap < 1800) {
      previous.text += fragment.text;
      previous.end_ms = fragment.end_ms ?? previous.end_ms;
    } else groups.push({ ...fragment });
  }
  return groups;
}

export function appendBounded<T>(values: T[], next: T, limit: number): T[] {
  return [...values.slice(-(limit - 1)), next];
}

export function redacted(value: unknown, key = ''): unknown {
  if (/^(csrf|authorization|cookie|secret|password|credentials?|headers|api_?key|access_token|refresh_token|sdp|audio|delta|transcript|arguments|instructions|input|output|content|text|message|url|endpoint)$/i.test(key)) return '[REDACTED]';
  if (key === 'attributes') {
    const maskStrings = (item: unknown): unknown => {
      if (typeof item === 'string') return '[REDACTED]';
      if (Array.isArray(item)) return item.map(maskStrings);
      if (item !== null && typeof item === 'object') return Object.fromEntries(Object.entries(item).map(([k, v]) => [k, maskStrings(v)]));
      return item;
    };
    return maskStrings(value);
  }
  if (Array.isArray(value)) return value.map(item => redacted(item, key));
  if (value !== null && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, redacted(v, k)]));
  if (typeof value !== 'string') return value;
  if (/^(type|kind|status|state|phase|stage|server|version|tool_choice|visible_tools|queued|model|voice|name|intent|service_tier|effort|summary|verbosity|transport|direction|reason|.*_id|id|callId|delegationId|trace_id)$/.test(key) && !/bearer\s|sk-[a-zA-Z0-9]{12,}|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(value)) return value;
  return '[REDACTED]';
}

/** Keep private content out of the protocol console; transcripts have their own bounded view. */
export function debugFrame(frame: JsonObject): JsonObject {
  const sanitize = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(sanitize);
    if (value !== null && typeof value === 'object') {
      const current = value as JsonObject;
      return Object.fromEntries(Object.entries(current).map(([key, item]) => [
        key, /^(audio|sdp|delta|transcript|arguments|input|output|content|text|tool_outputs?|result)$/i.test(key)
          ? '[私密内容已省略]' : sanitize(item),
      ]));
    }
    return value;
  };
  return sanitize(frame) as JsonObject;
}
