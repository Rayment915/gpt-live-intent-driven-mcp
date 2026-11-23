import { useMemo, useState } from 'react';
import { spanDuration } from '../protocol';
import type { Span } from '../types';
import { Icon } from './Icon';

export const milliseconds = (value: number) => value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;

function family(span: Span): string {
  const label = `${span.kind} ${span.name}`.toLowerCase();
  if (/mcp/.test(label)) return 'mcp';
  if (/decision|route|classify_intent/.test(label)) return 'decision';
  if (/tool|function|mock/.test(label)) return 'tool';
  if (/response|delegat/.test(label)) return 'delegation';
  return 'audio';
}

export function Waterfall({ spans, now, live, selected, select }: {
  spans: Span[]; now: number; live: boolean; selected?: string; select: (id: string) => void;
}) {
  const [filter, setFilter] = useState('all');
  const [windowSize, setWindowSize] = useState(0);
  const [offset, setOffset] = useState(0);
  const max = Math.max(1000, ...spans.map(span => span.start_ms + spanDuration(span, now)));
  const width = windowSize || Math.ceil(max / 1000) * 1000;
  const left = windowSize ? Math.min(offset, Math.max(0, max - width)) : 0;
  const ordered = useMemo(() => [...spans].sort((a, b) => a.start_ms - b.start_ms || a.id.localeCompare(b.id)), [spans]);
  const visible = ordered.filter(span => filter === 'all' || family(span) === filter);
  const ancestors = (span: Span) => {
    let depth = 0;
    let parent = span.parent_id;
    const seen = new Set([span.id]);
    while (parent && depth < 4 && !seen.has(parent)) {
      seen.add(parent);
      depth++;
      parent = spans.find(item => item.id === parent)?.parent_id;
    }
    return depth;
  };
  const ticks = Array.from({ length: 6 }, (_, index) => left + index / 5 * width);

  return <section className="panel waterfall-panel" aria-labelledby="waterfall-title">
    <div className="panel-heading">
      <div><div className="eyebrow">SHARED SESSION CLOCK</div><h2 id="waterfall-title">调用瀑布 <span className="count">{spans.length}</span></h2></div>
      <div className="heading-actions"><span className={`live-marker ${live ? 'is-live' : ''}`}>{live ? '实时' : '待机'}</span>
        <label className="sr-only" htmlFor="window-size">时间窗口</label>
        <select id="window-size" className="compact" value={windowSize} onChange={event => { setWindowSize(Number(event.target.value)); setOffset(0); }}>
          <option value={0}>全局时间</option><option value={5000}>5 秒窗口</option><option value={15000}>15 秒窗口</option><option value={30000}>30 秒窗口</option>
        </select>
      </div>
    </div>
    <div className="trace-filters" aria-label="调用类型筛选">
      {[['all', '全部'], ['audio', 'Live / 音频'], ['delegation', 'Responses'], ['decision', 'Decision'], ['tool', 'Mock 工具'], ['mcp', 'MCP']].map(([key, label]) =>
        <button key={key} className={filter === key ? 'chip active' : 'chip'} aria-pressed={filter === key} onClick={() => setFilter(key)}><i className={`lane-dot ${key}`} />{label}</button>)}
    </div>
    <div className="waterfall-scroll">
      <div className="waterfall-axis"><span>调用 / 父子关系</span><div>{ticks.map((tick, index) => <span key={index} style={{ left: `${index * 20}%` }}>{(tick / 1000).toFixed(width < 10000 ? 1 : 0)}s</span>)}</div></div>
      {visible.length ? <div className="trace-rows">{visible.map(span => {
        const duration = spanDuration(span, now);
        const start = (span.start_ms - left) / width * 100;
        const end = (span.start_ms + duration - left) / width * 100;
        const inRange = end >= 0 && start <= 100;
        return <button className={`trace-row ${selected === span.id ? 'selected' : ''}`} key={span.id} onClick={() => select(span.id)} aria-pressed={selected === span.id}>
          <span className="trace-name" style={{ paddingLeft: 16 + ancestors(span) * 12 }}>
            <i className={`lane-dot ${family(span)}`} /><span><b title={span.name}>{span.name}</b><small>{span.kind} · {span.status}</small></span>
          </span>
          <span className="trace-track">
            {[0, 20, 40, 60, 80, 100].map(tick => <i className="gridline" key={tick} style={{ left: `${tick}%` }} />)}
            {inRange && <span className={`trace-bar ${family(span)} ${span.status}`} style={{ left: `${Math.max(0, start)}%`, width: `${Math.max(0.7, Math.min(100, end) - Math.max(0, start))}%` }}>
              <span>{milliseconds(duration)}{span.status === 'running' ? ' …' : ''}</span>
            </span>}
          </span>
        </button>;
      })}</div> : <div className="trace-empty">
        <div className="empty-lanes" aria-hidden="true"><i /><i /><i /><i /></div>
        <Icon name="trace" size={30} />
        <h3>{spans.length ? '这个筛选下还没有调用' : '让一次对话，展开成一条时间线'}</h3>
        <p>{spans.length ? '选择其他调用类型查看已收到的追踪。' : '开始会话后，Live、委派、Decision 和工具调用会沿同一时钟展开。'}</p>
        <span className="mono subtle">等待真实 studio.span · 不显示模拟数据</span>
      </div>}
    </div>
    {windowSize > 0 && <label className="timeline-range"><span>定位 {(left / 1000).toFixed(1)} s</span><input aria-label="移动时间窗口" type="range" min={0} max={Math.max(0, max - width)} step={100} value={left} onChange={event => setOffset(Number(event.target.value))} /><button className="text-button" onClick={() => setOffset(Math.max(0, max - width))}>最新</button></label>}
    <div className="panel-footnote"><span><Icon name="link" size={13} /> 后端单调时钟 · 父子调用可重叠</span><span>条长 = 调用耗时 · 非播放延迟</span></div>
  </section>;
}
