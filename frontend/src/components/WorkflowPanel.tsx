import type { LiveData } from '../state';
import { milliseconds } from './Waterfall';

const labels: Record<string, string> = {
  classification: '等待意图分类', classifying: 'Decision 意图分类中',
  discovering: '按需准备 MCP 目录', updating: '等待工具配置确认',
  executing: '业务工具执行阶段', clarifying: '需要澄清或返回错误',
  restoring: '恢复仅分类工具', failed: '配置失败，禁止业务执行',
};

export function WorkflowPanel({ data, managed }: { data: LiveData; managed: boolean }) {
  if (!managed) return null;
  const workflow = data.workflow;
  return <section className="panel workflow-panel" aria-label="动态工具与启动阶段">
    <div className="workflow-heading"><strong>{labels[workflow?.phase ?? 'classification'] ?? workflow?.phase}</strong>
      <span className="badge">{workflow?.queued.length ?? 0} 项排队</span>
    </div>
    <p className="help">启动不等待业务 MCP；分类成功后按需开放工具。同一会话业务串行，语音继续。</p>
    <div className="tool-tags">{(workflow?.visible_tools ?? ['classify_intent']).map(name => <span className="badge mono" key={name}>{name}</span>)}</div>
    <p className="help mono">tool_choice · {workflow?.tool_choice ?? 'required'} / 配置版本 · {workflow?.config_version ?? 0}</p>
    {!!data.startup.length && <details><summary>浏览器启动阶段耗时</summary><dl className="key-values">
      {data.startup.map((item, index) => <div key={`${item.stage}-${index}`}><dt>{item.stage}</dt><dd className="mono">{milliseconds(item.duration_ms)} · {item.status}</dd></div>)}
    </dl><p className="help">后端凭据、Live 创建、sideband 耗时在瀑布中单独展示；不能与浏览器 HTTP 区间重复相加。</p></details>}
    {Array.isArray(data.effectiveResponses.tools) && <details><summary>当前服务有效 tools JSON</summary><pre className="code json-view">{JSON.stringify(data.effectiveResponses.tools, null, 2)}</pre></details>}
  </section>;
}
