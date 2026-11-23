import { appendBounded, mergeSpan, nestedEvent, transcriptFragment } from './protocol';
import { object, type CatalogStatus, type JsonObject, type Route, type Span, type StartupTiming, type ToolCall, type TranscriptFragment, type WorkflowState } from './types';

export interface LiveData {
  spans: Span[];
  routes: Route[];
  calls: ToolCall[];
  fragments: TranscriptFragment[];
  transcriptCount: number;
  usage: JsonObject;
  responsesUsage: Record<string, JsonObject>;
  finalConfirmed: boolean;
  workflow?: WorkflowState;
  effectiveResponses: JsonObject;
  startup: StartupTiming[];
  catalogs: CatalogStatus[];
}

export const initialData = (): LiveData => ({
  spans: [], routes: [], calls: [], fragments: [], transcriptCount: 0,
  usage: {}, responsesUsage: {}, finalConfirmed: false, effectiveResponses: {}, startup: [], catalogs: [],
});

export function receiveFrame(state: LiveData, frame: JsonObject): LiveData {
  const type = String(frame.type);
  let next = state;
  if (type === 'studio.workflow' && typeof frame.phase === 'string') {
    const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
    next = { ...next, workflow: {
      phase: frame.phase,
      active_delegation: typeof frame.active_delegation === 'string' ? frame.active_delegation : null,
      queued: strings(frame.queued), visible_tools: strings(frame.visible_tools),
      tool_choice: String(frame.tool_choice ?? ''), config_version: Number(frame.config_version ?? 0),
    }, catalogs: Array.isArray(frame.catalogs) ? frame.catalogs.map(value => {
      const item = object(value);
      return { server: String(item.server ?? ''), state: String(item.state ?? ''), version: typeof item.version === 'string' ? item.version : null, expires_in_seconds: Number(item.expires_in_seconds ?? 0), tools: strings(item.tools) };
    }) : state.catalogs };
  }
  if (type === 'session.started' || type === 'session.updated') {
    next = { ...next, effectiveResponses: object(object(object(frame.session).delegation).responses) };
  }
  if (type === 'studio.browser_startup' && typeof frame.stage === 'string' && typeof frame.duration_ms === 'number') {
    next = { ...next, startup: appendBounded(state.startup, {
      stage: frame.stage, duration_ms: frame.duration_ms,
      status: frame.status === 'completed' ? 'completed' : 'failed',
    }, 30) };
  }
  if (type === 'studio.span' && typeof frame.id === 'string' && typeof frame.start_ms === 'number') {
    const span = frame as unknown as Span;
    const previous = state.spans.find(item => item.id === span.id);
    next = { ...next, spans: previous ? state.spans.map(item => item.id === span.id ? mergeSpan(item, span) : item) : appendBounded(state.spans, mergeSpan(undefined, span), 3000) };
  }
  if (type === 'studio.route') next = { ...next, routes: appendBounded(state.routes, frame as unknown as Route, 400) };
  if (type === 'studio.tools' && Array.isArray(frame.calls)) next = { ...next, calls: (frame.calls as ToolCall[]).slice(-800) };
  const fragment = transcriptFragment(frame, state.transcriptCount + 1);
  if (fragment) next = { ...next, fragments: appendBounded(state.fragments, fragment, 3000), transcriptCount: state.transcriptCount + 1 };
  if (type === 'session.usage.updated' || type === 'session.usage' || type === 'session.closed') {
    const usage = object(frame.usage ?? object(frame.session).usage);
    if (Object.keys(usage).length) next = { ...next, usage };
  }
  const event = nestedEvent(frame);
  const response = object(event.response);
  if (event.type === 'response.completed' && response.usage) {
    next = { ...next, responsesUsage: { ...state.responsesUsage, [String(response.id ?? frame.delegation_id ?? 'last')]: object(response.usage) } };
  }
  if (type === 'session.closed') next = { ...next, finalConfirmed: true };
  if (type === 'studio.disconnected') next = { ...next, finalConfirmed: frame.final_usage_confirmed === true || state.finalConfirmed };
  return next;
}
