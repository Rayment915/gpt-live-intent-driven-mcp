import { useMemo, useState } from 'react';
import { parseSession } from '../config';
import { message, type DebugEvent, type JsonObject } from '../types';
import { Icon } from './Icon';

export function Debugger({ events, live, send, report }: {
  events: DebugEvent[]; live: boolean;
  send: (event: JsonObject, experimental: boolean) => Promise<unknown>;
  report: (cause: unknown) => void;
}) {
  const [search, setSearch] = useState('');
  const [audio, setAudio] = useState(false);
  const [frozen, setFrozen] = useState<DebugEvent[]>();
  const [selected, setSelected] = useState<number>();
  const [tab, setTab] = useState('events');
  const [text, setText] = useState('{\n  "type": "session.instructions.append",\n  "delegation_id": null,\n  "content": ""\n}');
  const [experimental, setExperimental] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState('');
  const source = frozen ?? events;
  const filtered = useMemo(() => source.filter(event =>
    (audio || !/audio.*(?:delta|append)$/.test(event.type)) && (!search || `${event.type} ${JSON.stringify(event.frame)}`.toLowerCase().includes(search.toLowerCase())),
  ).reverse(), [source, search, audio]);
  const event = source.find(item => item.seq === selected) ?? filtered[0];
  return <section className="panel debugger-panel" aria-labelledby="debug-title">
    <div className="debug-heading"><div className="tabs"><button className={tab === 'events' ? 'active' : ''} onClick={() => setTab('events')} aria-pressed={tab === 'events'} id="debug-title">协议事件 <span className="count">{events.length}/800</span></button><button className={tab === 'command' ? 'active' : ''} onClick={() => setTab('command')} aria-pressed={tab === 'command'}>发送命令</button></div><span className="mono subtle debug-source">SERVER SIDEBAND</span></div>
    {tab === 'events' ? <>
      <div className="debug-toolbar"><label className="search-field"><Icon name="search" size={15} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="筛选事件类型或内容" aria-label="筛选协议事件" /></label><label className="inline-check"><input type="checkbox" checked={audio} onChange={event => setAudio(event.target.checked)} />音频帧</label><button className={`chip ${frozen ? 'active' : ''}`} onClick={() => setFrozen(frozen ? undefined : [...events])}>{frozen ? '继续跟随' : '冻结视图'}</button></div>
      <div className="debug-split"><div className="event-list" tabIndex={0} aria-label="协议事件列表">{filtered.length ? filtered.map(item => <button className={`event-row ${event?.seq === item.seq ? 'selected' : ''}`} key={item.seq} onClick={() => setSelected(item.seq)}>
        <span className={`direction ${item.direction}`}>{item.direction === 'in' ? '↓' : item.direction === 'out' ? '↑' : '·'}</span>
        <span className="mono event-type">{item.type}</span><time className="mono" title={new Date(item.received).toISOString()}>{new Date(item.received).toLocaleTimeString('zh-CN', { hour12: false })}</time>
      </button>) : <div className="empty small-empty"><Icon name="code" size={24} /><p>{events.length ? '没有匹配的事件。音频帧默认隐藏。' : '等待真实协议事件；最近 800 帧会保留在内存中。'}</p></div>}</div><pre className="code event-json">{event ? JSON.stringify(event.frame, null, 2) : '// 选择事件，检查协议 JSON\n// 音频、转写、SDP 和工具输出已省略'}</pre></div>
    </> : <form className="command-form" onSubmit={async submit => {
      submit.preventDefault();
      setSending(true); setResult('');
      try {
        const command = parseSession(text);
        if (typeof command.type !== 'string') throw new Error('命令必须包含字符串 type');
        if (command.type === 'session.update') throw new Error('请使用参数面板的「更新 Responses」；仅允许发送完整 Responses 对象');
        if (['session.start', 'session.close', 'session.input_audio.append'].includes(command.type)) throw new Error('请通过会话控制按钮或音频输入执行此命令，以保持生命周期一致');
        await send(command, experimental);
        setResult('命令已发送；请在协议事件中检查服务端确认。');
      } catch (cause) { setResult(`发送失败：${message(cause)}`); report(cause); }
      finally { setSending(false); }
    }}>
      <p className="help">原生 Live 命令经服务端发送；model、voice、初始 instructions 不可在运行中替换。append 命令不关联具体委派时，请显式提供 delegation_id: null。</p>
      <textarea className="code" aria-label="原生命令 JSON" value={text} onChange={event => setText(event.target.value)} rows={7} spellCheck={false} />
      <div className="command-actions"><label className="inline-check"><input type="checkbox" checked={experimental} onChange={event => setExperimental(event.target.checked)} />experimental（显式实验命令）</label><button className="button" type="submit" disabled={!live || sending}>{sending ? '发送中…' : '发送命令'}</button></div>
      {result && <p className="help" role="status">{result}</p>}
    </form>}
  </section>;
}
