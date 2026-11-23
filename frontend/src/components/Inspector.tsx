import { useState } from 'react';
import { spanDuration } from '../protocol';
import type { LiveData } from '../state';
import { object, type Span } from '../types';
import { Icon } from './Icon';
import { milliseconds } from './Waterfall';

function KeyValues({ value }: { value: unknown }) {
  const entries = Object.entries(object(value));
  return entries.length ? <dl className="key-values">{entries.map(([key, item]) => <div key={key}><dt>{key}</dt><dd>{item !== null && typeof item === 'object' ? <KeyValues value={item} /> : <span className="mono">{String(item)}</span>}</dd></div>)}</dl> : <span className="subtle">暂无数据</span>;
}

export function Inspector({ data, span, now, threshold }: { data: LiveData; span?: Span; now: number; threshold?: number }) {
  const [tab, setTab] = useState('trace');
  const route = span ? [...data.routes].reverse().find(item => item.delegation_id === span.attributes?.delegation_id || item.delegation_id === span.trace_id) ?? data.routes.at(-1) : data.routes.at(-1);
  const probabilities = route && (Array.isArray(route.probabilities)
    ? route.probabilities.map((value, index) => {
      const item = object(value);
      return [String(item.intent ?? item.label ?? index), Number(item.probability ?? item.confidence ?? value)] as const;
    }) : Object.entries(object(route.probabilities)).map(([key, value]) => [key, Number(value)] as const));
  return <section className="panel inspector-panel">
    <div className="tabs panel-tabs" aria-label="检查器视图">{[['trace', '调用详情'], ['route', '委派路由'], ['usage', '用量']].map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)} aria-pressed={tab === key}>{label}{key === 'route' && data.routes.length > 0 && <span className="count">{data.routes.length}</span>}</button>)}</div>
    <div className="inspector-content">
      {tab === 'trace' && (span ? <>
        <div className="detail-title"><i className={`status-dot ${span.status}`} /><h3>{span.name}</h3></div>
        <div className="detail-duration">{milliseconds(spanDuration(span, now))}<span>{span.status}</span></div>
        <dl className="key-values">
          <div><dt>start_ms</dt><dd className="mono">{span.start_ms.toFixed(1)}</dd></div>
          <div><dt>end_ms</dt><dd className="mono">{span.end_ms?.toFixed(1) ?? '—'}</dd></div>
          <div><dt>trace_id</dt><dd className="mono break">{span.trace_id}</dd></div>
          <div><dt>parent_id</dt><dd className="mono break">{span.parent_id ?? '根调用'}</dd></div>
        </dl>
        <details open><summary>attributes</summary><pre className="code json-view">{JSON.stringify(span.attributes, null, 2)}</pre></details>
      </> : <div className="empty small-empty"><Icon name="trace" size={24} /><h3>点击一条调用</h3><p>查看起止时间、父调用与属性。<br />运行中的耗时持续增长，结束后固定。</p></div>)}
      {tab === 'route' && (route ? <>
        <div className="route-heading"><span className="eyebrow">DECISION → ALLOWED TOOLS</span><span className="badge real">真实模型</span></div>
        <h3>{route.intent}</h3><p className="route-score">{(route.confidence * 100).toFixed(1)}% <span>置信度 · 阈值 {threshold === undefined ? '未提供' : `${(threshold * 100).toFixed(0)}%`}</span></p>
        <dl className="key-values"><div><dt>confidence</dt><dd className="mono">{Number.isFinite(route.confidence) ? route.confidence.toFixed(4) : '未提供'}</dd></div><div><dt>top_probability</dt><dd className="mono">{route.top_probability === undefined ? '未提供' : route.top_probability.toFixed(4)}</dd></div></dl>
        <div className="probabilities">{probabilities?.map(([label, probability]) => <div key={label}><div><span>{label}</span><b>{Number.isFinite(probability) ? `${(probability * 100).toFixed(1)}%` : '—'}</b></div><progress max={1} value={Number.isFinite(probability) ? Math.max(0, Math.min(1, probability)) : 0} /></div>)}</div>
        <h4>允许的工具</h4><div className="tool-tags">{route.allowed_tools?.length ? route.allowed_tools.map(tool => <span className="mono badge" key={tool}>{tool}</span>) : <span className="subtle">没有允许的工具</span>}</div>
        {data.workflow && <p className="help">这里是 Decision 的允许范围；实际已开放的工具以动态阶段面板为准。</p>}
        <p className="help mono break">{route.delegation_id}</p>
      </> : <div className="empty small-empty"><Icon name="shield" size={24} /><h3>等待一次委派</h3><p>这里展示 Decision 的真实概率、<br />路由意图与工具白名单。</p></div>)}
      {tab === 'usage' && <>
        <div className={`usage-confirmation ${data.finalConfirmed ? 'confirmed' : ''}`}><Icon name={data.finalConfirmed ? 'check' : 'alert'} size={16} />{data.finalConfirmed ? '最终用量已确认' : '累计快照 · 非最终确认'}</div>
        <p className="help">Live 用量是累计快照，不叠加各次 usage 事件。Responses token 用量独立显示。</p>
        <h4>Live · 音频累计</h4><KeyValues value={data.usage} />
        <h4>Responses · 每个响应</h4>{Object.entries(data.responsesUsage).length ? Object.entries(data.responsesUsage).map(([id, usage]) => <details key={id}><summary className="mono break">{id}</summary><KeyValues value={usage} /></details>) : <p className="subtle">尚未收到完成响应的 token 用量。</p>}
      </>}
    </div>
  </section>;
}
