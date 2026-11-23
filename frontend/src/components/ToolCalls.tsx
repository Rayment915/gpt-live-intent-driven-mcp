import type { Studio } from '../useStudio';
import { Icon } from './Icon';

export function ToolCalls({ studio, mcpNames, mockNames }: { studio: Studio; mcpNames: Set<string>; mockNames: Set<string> }) {
  if (!studio.data.calls.length) return null;
  const toolBadge = (name: string) => {
    const mock = mockNames.has(name) || (name !== 'classify_intent' && !mcpNames.has(name));
    return <span className={`badge ${mock ? 'mock' : 'real'}`}>{mock ? 'Mock 业务工具' : name === 'classify_intent' ? '真实 Decision' : '真实 MCP'}</span>;
  };
  return <section className="panel tool-calls-panel" aria-label="工具执行记录">
    <div className="tool-calls-heading"><Icon name="shield" size={20} /><div><h2>工具执行记录</h2><p>当前 Mock 业务自动执行，仍进行意图、参数和客户资料校验；无需额外口头确认或页面点击。</p></div></div>
    {!!studio.data.calls.length && <details className="tool-records"><summary>查看 {studio.data.calls.length} 个工具调用</summary><div>{studio.data.calls.map(call => <details key={call.callId}><summary><span className="mono">{call.name}</span> {toolBadge(call.name)} <span className="subtle">{call.state}</span></summary><pre className="code json-view">{JSON.stringify(call, null, 2)}</pre></details>)}</div></details>}
  </section>;
}
