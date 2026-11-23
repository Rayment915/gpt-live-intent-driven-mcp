import { useEffect, useState } from 'react';
import { at, changeDelegation, delegationMode, setAt, type DelegationMode } from '../config';
import { object, message, type JsonObject } from '../types';

interface Props {
  session: JsonObject;
  defaults: JsonObject;
  locked: boolean;
  change: (session: JsonObject) => void;
  validity: (path: string, error: string) => void;
  managed?: boolean;
}

function JsonField({ label, value, change, validity, disabled = false, hint, kind = 'json' }: {
  label: string; value: unknown; change: (value: unknown) => void;
  validity: Props['validity']; disabled?: boolean; hint?: string; kind?: 'json' | 'choice';
}) {
  const serialized = value === undefined ? '' : kind === 'choice' && typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const [text, setText] = useState(serialized);
  const [error, setError] = useState('');
  useEffect(() => {
    setText(serialized); setError(''); validity(label, '');
    return () => validity(label, '');
  }, [serialized, label, validity]);
  return <label className="field">
    <span>{label}</span>
    <textarea className="code small-json" aria-label={label} aria-invalid={!!error} value={text} disabled={disabled}
      placeholder={kind === 'choice' ? 'auto / none / required / {"type":…}' : '未设置 · 使用服务端默认'}
      rows={Array.isArray(value) && value.length ? 6 : 3}
      onChange={event => {
        const next = event.target.value;
        setText(next);
        try {
          const parsed: unknown = !next.trim() ? undefined : kind === 'choice' && !/^[{["]/.test(next.trim()) ? next.trim() : JSON.parse(next);
          if (kind === 'choice' && parsed !== undefined && typeof parsed !== 'string' && (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object')) throw new Error('必须为字符串或对象');
          setError(''); validity(label, ''); change(parsed);
        } catch (cause) {
          const text = message(cause);
          setError(text); validity(label, text);
        }
      }} />
    {hint && <small>{hint}</small>}
    {error && <small className="danger-text">{error}</small>}
  </label>;
}

export function ConfigEditor({ session, defaults, locked, change, validity, managed = false }: Props) {
  const patch = (path: string, value: unknown) => change(setAt(session, path.split('.'), value));
  const value = (path: string) => at(session, path.split('.'));
  const stringField = (path: string, label: string, options: { multiline?: boolean; disabled?: boolean; hint?: string; list?: string } = {}) => {
    const current = value(path);
    return <label className="field" key={path}>
      <span>{label}</span>
      {options.multiline ? <textarea aria-label={label} rows={4} disabled={options.disabled} value={typeof current === 'string' ? current : ''} placeholder={current === null ? 'null · 使用服务端默认' : '未设置 · 使用服务端默认'} onChange={event => patch(path, event.target.value)} />
        : <input aria-label={label} list={options.list} disabled={options.disabled} value={typeof current === 'string' ? current : ''} placeholder="使用服务端默认" onChange={event => patch(path, event.target.value || undefined)} />}
      {options.hint && <small>{options.hint}</small>}
    </label>;
  };
  const selectField = (path: string, label: string, options: string[], disabled = false) => <label className="field" key={path}>
    <span>{label}</span>
    <select aria-label={label} value={value(path) === null ? '__null__' : String(value(path) ?? '')} disabled={disabled} onChange={event => patch(path, event.target.value === '' ? undefined : event.target.value === '__null__' ? null : event.target.value)}>
      <option value="">未设置</option><option value="__null__">null</option>{options.map(option => <option key={option}>{option}</option>)}
    </select>
  </label>;
  const jsonField = (path: string, label: string, disabled: boolean, hint?: string, kind?: 'json' | 'choice') =>
    <JsonField key={path} label={label} value={value(path)} disabled={disabled} validity={validity} change={next => patch(path, next)} hint={hint} kind={kind} />;
  const mode = delegationMode(session);
  const responsePrefix = 'delegation.responses.';
  const known = new Set(['model', 'instructions', 'audio', 'delegation', 'input', 'client', 'triggers', 'store', 'safety_identifier']);
  const extras = Object.keys(session).filter(key => !known.has(key));
  const responses = object(object(session.delegation).responses);
  const responseKeys = new Set(['model', 'instructions', 'max_output_tokens', 'service_tier', 'reasoning', 'text', 'tools', 'tool_choice', 'parallel_tool_calls']);

  return <div className="config-fields">
    <div className="section-caption"><span>LIVE · 启动参数</span><span className="subtle">{locked ? '已锁定' : '仅启动时设置'}</span></div>
    {stringField('model', 'Live 模型部署名', { disabled: locked })}
    {typeof value('audio.output.voice') === 'object' && value('audio.output.voice') !== null
      ? jsonField('audio.output.voice', '音色 · voice 对象', locked)
      : stringField('audio.output.voice', '音色 · voice', { disabled: locked, list: 'voices' })}
    <datalist id="voices">{['marin', 'cedar', 'alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse'].map(voice => <option key={voice} value={voice} />)}</datalist>
    {stringField('instructions', '初始提示 · instructions', { multiline: true, disabled: locked, hint: '定义语音风格与委派时机；业务规则放在 Responses 提示中。' })}

    <div className="section-caption"><span>DELEGATION · 委派</span><span className="badge real">真实模型</span></div>
    <label className="field"><span>委派启动模式</span>
      <select value={mode} disabled={locked} onChange={event => change(changeDelegation(session, event.target.value as DelegationMode, defaults))}>
        <option value="responses">responses · 模型托管</option>
        <option value="client">client · 应用处理</option>
        <option value="omitted">省略 delegation · 应用处理</option>
        <option value="null">null · 应用处理</option>
      </select>
      <small>省略、null 与显式 client 会原样保留，不互相归一化。</small>
    </label>
    {mode === 'responses' && <>
      {stringField(`${responsePrefix}model`, 'Responses 模型部署名')}
      {stringField(`${responsePrefix}instructions`, 'Responses 业务提示', { multiline: true })}
      <div className="field-pair">
        <label className="field"><span>max_output_tokens</span>
          <input type="number" min={1} step={1} value={String(responses.max_output_tokens ?? '')} placeholder="未设置" onChange={event => patch(`${responsePrefix}max_output_tokens`, event.target.value === '' ? undefined : Number(event.target.value))} />
        </label>
        {stringField(`${responsePrefix}service_tier`, 'service_tier', { list: 'tiers' })}
      </div>
      <datalist id="tiers">{['auto', 'default', 'flex', 'priority', 'scale'].map(tier => <option key={tier}>{tier}</option>)}</datalist>
      <div className="field-pair">
        {selectField(`${responsePrefix}reasoning.effort`, 'reasoning.effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'])}
        {selectField(`${responsePrefix}reasoning.summary`, 'reasoning.summary', ['auto', 'concise', 'detailed'])}
      </div>
      {selectField(`${responsePrefix}text.verbosity`, 'text.verbosity', ['low', 'medium', 'high'])}
      {jsonField(`${responsePrefix}tools`, 'tools · JSON', managed, managed ? '服务器托管：启动仅 classify_intent，分类后动态开放；当前有效工具见阶段面板。' : '初始值来自后端。业务工具为 Mock；MCP 连接真实服务。')}
      {jsonField(`${responsePrefix}tool_choice`, 'tool_choice · 字符串 / 对象', managed, managed ? '服务器按分类/业务阶段切换，不能手动绕过。' : undefined, 'choice')}
      <label className="field"><span>parallel_tool_calls</span>
        <select value={responses.parallel_tool_calls === undefined ? '' : String(responses.parallel_tool_calls)} onChange={event => patch(`${responsePrefix}parallel_tool_calls`, event.target.value === '' ? undefined : event.target.value === 'true')}>
          <option value="">未设置</option><option value="true">true · 允许并行</option><option value="false">false · 顺序执行</option>
        </select>
      </label>
      <details className="advanced"><summary>其他 Responses 参数（无损保留）</summary>
        {Object.keys(responses).filter(key => !responseKeys.has(key)).map(key => jsonField(`${responsePrefix}${key}`, key, false))}
        {jsonField(`${responsePrefix}reasoning`, '完整 reasoning 对象', false)}
        {jsonField(`${responsePrefix}text`, '完整 text 对象', false)}
        <p className="help">通过完整 JSON 添加任意 Responses 参数；更新发送完整 Responses 对象，而不是局部补丁。</p>
      </details>
    </>}
    <details className="advanced">
      <summary>全部 Live 基线参数</summary>
      <p className="help">遵循 /openai/v1/live；不使用 Realtime 的 turn_detection / modalities 参数。</p>
      {jsonField('audio', '完整 audio 对象（含自定义音色）', locked)}
      {jsonField('input', 'input · 初始文本历史', locked)}
      {jsonField('client', 'client · 数据通道权限', locked)}
      {jsonField('triggers', 'triggers · Decision 触发器', locked)}
      {stringField('safety_identifier', 'safety_identifier', { disabled: locked })}
      <label className="field"><span>store · 服务端保存会话</span>
        <select value={session.store === undefined ? '' : String(session.store)} disabled={locked} onChange={event => patch('store', event.target.value === '' ? undefined : event.target.value === 'true')}>
          <option value="">未设置 · 默认 false</option><option value="false">false · 不保存</option><option value="true">true · 允许服务端保存</option>
        </select>
      </label>
      {session.delegation !== undefined && session.delegation !== null && jsonField('delegation.acknowledgement_delay_ms', 'acknowledgement_delay_ms', locked)}
      {extras.map(key => jsonField(key, key, locked))}
    </details>
  </div>;
}
