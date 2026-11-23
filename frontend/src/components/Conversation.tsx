import { useEffect, useMemo, useRef, useState } from 'react';
import { groupTranscripts } from '../protocol';
import type { TranscriptFragment } from '../types';
import { Icon } from './Icon';

export function Conversation({ fragments, total }: { fragments: TranscriptFragment[]; total: number }) {
  const [follow, setFollow] = useState(true);
  const [showResponses, setShowResponses] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => groupTranscripts(fragments.filter(item => showResponses || item.role !== 'response')), [fragments, showResponses]);
  useEffect(() => { if (follow) end.current?.scrollIntoView({ block: 'nearest' }); }, [fragments, follow]);
  return <section className="panel conversation-panel" aria-labelledby="conversation-title">
    <div className="panel-heading"><div><div className="eyebrow">VOICE TRANSCRIPT</div><h2 id="conversation-title">正在说什么</h2></div>
      <label className="inline-check"><input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)} />跟随</label>
    </div>
    <div className="conversation-toolbar"><label className="inline-check"><input type="checkbox" checked={showResponses} onChange={event => setShowResponses(event.target.checked)} />显示 Responses 文本</label><span className="subtle">{fragments.length} / 3000 片段</span></div>
    <div className="conversation-scroll" tabIndex={0} aria-label="对话转写">
      {groups.length ? groups.map(group => <article className={`utterance ${group.role}`} key={group.id}>
        <div className="utterance-meta"><b>{group.role === 'user' ? '你' : group.role === 'assistant' ? 'Live' : 'Responses'}</b><span>{group.start_ms === undefined ? '时间未提供' : `${(group.start_ms / 1000).toFixed(2)}s`}{group.end_ms === undefined ? '' : ` — ${(group.end_ms / 1000).toFixed(2)}s`}</span></div>
        <p>{group.text}</p>
      </article>) : <div className="empty conversation-empty"><Icon name="mic" size={26} /><h3>说话之后，文字留在这里</h3><p>输入与输出转写按到达顺序累积。<br />仅浏览器内存保留，不录制音频。</p></div>}
      {total > 3000 && <p className="help">仅保留最近 3000 个转写片段；更早片段已释放。</p>}
      <div ref={end} />
    </div>
  </section>;
}
