import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getConfig, refreshCatalog } from './api';
import { assertStartupUnchanged, parseSession, responsesUpdate, stableJson, validateSession } from './config';
import { redacted } from './protocol';
import { useStudio } from './useStudio';
import { message, object, type JsonObject, type StudioConfig, type Transport } from './types';
import { ToolCalls } from './components/ToolCalls';
import { ConfigEditor } from './components/ConfigEditor';
import { Conversation } from './components/Conversation';
import { Debugger } from './components/Debugger';
import { Icon } from './components/Icon';
import { Inspector } from './components/Inspector';
import { Waterfall } from './components/Waterfall';
import { WorkflowPanel } from './components/WorkflowPanel';
import { AuthGate } from './components/AuthGate';
import './styles.css';

const phaseLabels = { idle: '未连接', connecting: '建立连接中', live: '会话进行中', closing: '等待最终确认', closed: '已结束', error: '连接异常' };
const stringify = (value: unknown) => JSON.stringify(value, null, 2);

function StudioApp({ username, logout, loggingOut }: {
  username: string | null; logout: () => Promise<void>; loggingOut: boolean;
}) {
  const [config, setConfig] = useState<StudioConfig>();
  const [configError, setConfigError] = useState('');
  const [loading, setLoading] = useState(true);
  const [session, setSession] = useState<JsonObject>({});
  const [json, setJson] = useState('{}');
  const [jsonError, setJsonError] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [editorRevision, setEditorRevision] = useState(0);
  const [editorTab, setEditorTab] = useState('form');
  const [transport, setTransport] = useState<Transport>('webrtc');
  const [fileOnly, setFileOnly] = useState(false);
  const [input, setInput] = useState('');
  const [output, setOutput] = useState('');
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceError, setDeviceError] = useState('');
  const [selectedSpan, setSelectedSpan] = useState<string>();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [refreshingCatalog, setRefreshingCatalog] = useState('');
  const [theme, setTheme] = useState(document.documentElement.getAttribute('data-theme') ?? 'light');
  const startup = useRef<JsonObject>({});
  const fileInput = useRef<HTMLInputElement>(null);
  const studio = useStudio(config);
  const active = ['connecting', 'live', 'closing'].includes(studio.phase);
  const live = studio.phase === 'live';
  const invalid = !!jsonError || Object.values(fieldErrors).some(Boolean);

  const load = useCallback(async () => {
    setLoading(true); setConfigError('');
    try {
      const value = await getConfig(AbortSignal.timeout(15000));
      if (typeof value.csrf !== 'string' || typeof value.configured !== 'boolean' || !Object.keys(object(value.defaultConfig)).length) throw new Error('/api/config 缺少 csrf、configured 或 defaultConfig');
      value.mcp ??= [];
      setConfig(value);
      setSession(structuredClone(value.defaultConfig));
      setJson(stringify(value.defaultConfig));
      if (object(value.defaultConfig.audio).format) setTransport('websocket');
      setJsonError(''); setFieldErrors({}); setEditorRevision(value => value + 1);
    } catch (cause) { setConfigError(message(cause)); }
    finally { setLoading(false); }
  }, []);

  const enumerate = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) {
      setDeviceError('设备枚举不可用；请在 HTTPS / localhost 打开。仅文件模式不需要麦克风。');
      return;
    }
    try { setDevices(await navigator.mediaDevices.enumerateDevices()); setDeviceError(''); }
    catch (cause) { setDeviceError(`设备读取失败：${message(cause)}`); }
  }, []);

  useEffect(() => { void load(); void enumerate(); }, [load, enumerate]);
  useEffect(() => {
    navigator.mediaDevices?.addEventListener('devicechange', enumerate);
    return () => navigator.mediaDevices?.removeEventListener('devicechange', enumerate);
  }, [enumerate]);
  useEffect(() => { if (live) void enumerate(); }, [live, enumerate]);

  const validity = useCallback((path: string, error: string) => setFieldErrors(previous => previous[path] === error ? previous : { ...previous, [path]: error }), []);
  const validateManagedTools = (next: JsonObject) => {
    if (!config?.managed_tools || object(next.delegation).type !== 'responses') return;
    const responses = object(object(next.delegation).responses);
    const defaults = object(object(config.defaultConfig.delegation).responses);
    if (stableJson(responses.tools) !== stableJson(defaults.tools) || responses.tool_choice !== defaults.tool_choice) {
      throw new Error('tools 和 tool_choice 由分类工作流托管；当前有效工具请查看阶段面板。');
    }
  };
  const changeForm = (next: JsonObject) => {
    try {
      if (active) assertStartupUnchanged(next, startup.current);
      validateManagedTools(next);
      setSession(next); setJson(stringify(next)); setJsonError('');
    } catch (cause) { setJsonError(message(cause)); }
  };
  const changeJson = (text: string) => {
    setJson(text);
    try {
      const next = parseSession(text);
      if (active) assertStartupUnchanged(next, startup.current);
      validateManagedTools(next);
      setSession(next); setJsonError(''); setFieldErrors({}); setEditorRevision(value => value + 1);
    } catch (cause) { setJsonError(message(cause)); }
  };

  const mcpNames = useMemo(() => new Set(config?.mcp.flatMap(server => server.tools?.map(tool => typeof tool === 'string' ? tool : String(object(tool).name ?? '')) ?? []) ?? []), [config]);
  const mockTools = useMemo(() => {
    return config?.mcp.flatMap(server => server.tools?.map(tool => typeof tool === 'string' ? tool : String(object(tool).name ?? '')) ?? []) ?? [];
  }, [config]);
  const mockNames = useMemo(() => new Set(mockTools), [mockTools]);

  const exportTrace = () => {
    const exported = {
      version: 1, exported_at: new Date().toISOString(), protocol: '/openai/v1/live', transport,
      session: redacted(session), session_id: studio.id, spans: redacted(studio.data.spans),
      routes: redacted(studio.data.routes),
      workflow: redacted(studio.data.workflow), startup: redacted(studio.data.startup), catalogs: redacted(studio.data.catalogs),
      usage: redacted(studio.data.usage), responses_usage: redacted(studio.data.responsesUsage),
      final_usage_confirmed: studio.data.finalConfirmed,
      events: studio.events.map(event => ({ ...event, frame: redacted(event.frame) })),
      redaction: 'No audio, SDP, transcripts, prompts, tool arguments, URLs, credentials or arbitrary string attributes.',
    };
    const blob = new Blob([stringify(exported)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = `gpt-live-trace-${new Date().toISOString().replaceAll(':', '-')}.json`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return <div className="app-shell">
    <header className="app-header">
      <a className="brand" href="./" aria-label="GPT-Live Studio 首页" onClick={event => { if (active) event.preventDefault(); }}>
        <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /><i /></span>
        <span><strong>GPT-Live <em>Studio</em></strong><small>语音协议 / 委派调试工作台</small></span>
      </a>
      <div className="header-right"><span className="protocol-tag mono">/openai/v1/live</span><span className="header-divider" />
        <button className="icon-button" title={theme === 'dark' ? '切换到浅色' : '切换到深色'} aria-label={theme === 'dark' ? '切换到浅色' : '切换到深色'} onClick={() => {
          const next = theme === 'dark' ? 'light' : 'dark'; setTheme(next); document.documentElement.setAttribute('data-theme', next);
        }}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button>
        <button className="button export-button" disabled={!studio.events.length} onClick={exportTrace}><Icon name="download" size={15} />导出脱敏追踪</button>
        {username && <button className="button" disabled={loggingOut} onClick={() => { void logout(); }}>{loggingOut ? '正在退出…' : `${username} · 退出登录`}</button>}
      </div>
    </header>

    <div className="workspace-title">
      <div><div className="eyebrow">VOICE PROTOCOL LAB</div><h1>把对话，沿时间展开<span className="accent">。</span></h1><p>追踪语音流、模型委派与工具执行之间的每一段等待。</p></div>
      <div className="workspace-status"><span className={`status-pill ${studio.phase}`}><i />{phaseLabels[studio.phase]}</span><span className="mono subtle">{studio.id ? studio.id : '尚未创建会话'}</span></div>
    </div>

    {configError && <div className="alert error-alert" role="alert"><Icon name="alert" /><div><b>无法读取服务端配置</b><p>{configError}</p></div><button className="button" disabled={loading || active} onClick={() => void load()}>重新连接</button></div>}
    {config && !config.configured && <div className="alert warning-alert" role="alert"><Icon name="alert" /><div><b>模型服务尚未配置</b><p>请在 Python 后端配置 endpoint、部署名与凭据，再重新读取配置。浏览器不接收或保存 API 密钥。</p></div><button className="button" disabled={active || loading} onClick={() => void load()}>重新读取</button></div>}
    {studio.error && <div className="alert error-alert" role="alert"><Icon name="alert" /><div><b>操作未完成</b><p>{studio.error}</p></div><button className="icon-button" aria-label="关闭错误提示" onClick={studio.clearError}><Icon name="close" /></button></div>}

    <button className="button mobile-settings" aria-expanded={sidebarOpen} aria-controls="session-settings" onClick={() => setSidebarOpen(value => !value)}><Icon name="settings" size={16} />{sidebarOpen ? '收起会话参数' : '展开会话参数'}<span className="mono">{transport.toUpperCase()}</span></button>
    <div className="workspace-grid">
      <aside className={`sidebar panel ${sidebarOpen ? 'is-open' : ''}`} id="session-settings" aria-label="会话设置">
        <div className="sidebar-heading"><Icon name="settings" size={18} /><h2>会话设置</h2><span className="badge">{active ? '运行中' : '启动前'}</span></div>
        <div className="sidebar-scroll">
          <div className="section-caption"><span>TRANSPORT · 传输</span><Icon name="link" size={13} /></div>
          <div className="segmented" aria-label="传输方式">{(['webrtc', 'websocket'] as const).map(type => <button className={transport === type ? 'active' : ''} disabled={active} key={type} aria-pressed={transport === type} onClick={() => { setTransport(type); if (type === 'webrtc') setFileOnly(false); }}>{type === 'webrtc' ? 'WebRTC' : 'WebSocket'}<small>{type === 'webrtc' ? '原生媒体' : 'PCM 流'}</small></button>)}</div>
          <p className="help transport-note">{transport === 'webrtc' ? 'SDP offer / answer + 媒体轨道；只读取服务端 sideband 事件。' : 'AudioWorklet → PCM16LE · 24 kHz · mono；输出保持服务端时间间隙。'}</p>
          {transport === 'websocket' && <label className="inline-check file-only"><input type="checkbox" checked={fileOnly} disabled={active} onChange={event => setFileOnly(event.target.checked)} />仅文件输入 · 不请求麦克风</label>}
          <div className="field-pair device-fields">
            <label className="field"><span>输入设备</span><select value={input} disabled={(transport === 'websocket' && fileOnly) || studio.phase === 'connecting' || studio.phase === 'closing' || !!studio.upload?.active} onChange={async event => { const next = event.target.value; try { await studio.changeInput(next); setInput(next); } catch (cause) { studio.report(cause); } }}><option value="">系统默认麦克风</option>{devices.filter(device => device.kind === 'audioinput' && device.deviceId).map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || `麦克风 ${index + 1}`}</option>)}</select></label>
            <label className="field"><span>输出设备</span><select value={output} disabled={studio.phase === 'connecting' || studio.phase === 'closing'} onChange={async event => { const next = event.target.value; try { await studio.changeOutput(next); setOutput(next); } catch (cause) { studio.report(cause); } }}><option value="">系统默认扬声器</option>{devices.filter(device => device.kind === 'audiooutput' && device.deviceId).map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || `扬声器 ${index + 1}`}</option>)}</select></label>
          </div>
          {deviceError && <p className="help danger-text">{deviceError}</p>}
          <button className="text-button refresh-devices" onClick={() => void enumerate()}><Icon name="refresh" size={12} />刷新设备</button>

          <div className="tabs settings-tabs"><button className={editorTab === 'form' ? 'active' : ''} onClick={() => setEditorTab('form')}>参数表单</button><button className={editorTab === 'json' ? 'active' : ''} onClick={() => setEditorTab('json')}>同步 JSON{invalid && <i className="error-dot" />}</button></div>
          <div hidden={editorTab !== 'form'}><ConfigEditor key={editorRevision} session={session} defaults={config?.defaultConfig ?? {}} locked={active} managed={config?.managed_tools} change={changeForm} validity={validity} /></div>
          {editorTab === 'json' && <div className="json-editor"><p className="help">{active ? '仅 delegation.responses 可修改；启动参数已锁定。' : '启动会话时完整发送此对象；表单与有效 JSON 即时同步。'}</p><textarea className="code" rows={30} value={json} spellCheck={false} aria-label="完整会话参数 JSON" aria-invalid={!!jsonError} onChange={event => changeJson(event.target.value)} /><button className="text-button" onClick={() => { try { changeJson(stringify(parseSession(json))); } catch (cause) { setJsonError(message(cause)); } }}>格式化 JSON</button></div>}
          {invalid && <div className="form-errors" role="alert">{jsonError || '存在无效 JSON 字段，请修正后再操作。'}</div>}
          {live && object(session.delegation).type === 'responses' && <button className="button primary update-button" disabled={invalid || updating} onClick={async () => {
            setUpdating(true);
            try {
              assertStartupUnchanged(session, startup.current); validateSession(session, transport);
              const update = responsesUpdate(session);
              if (config?.managed_tools) {
                const responses = object(object(object(update.session).delegation).responses);
                responses.tools = studio.data.effectiveResponses.tools ?? responses.tools;
                responses.tool_choice = studio.data.effectiveResponses.tool_choice ?? responses.tool_choice;
              }

              await studio.command(update);
            }
            catch (cause) { studio.report(cause); }
            finally { setUpdating(false); }
          }}><Icon name="refresh" size={15} />{updating ? '更新中…' : '更新完整 Responses 参数'}</button>}
        </div>
        <div className="resource-footer"><div className="section-caption"><span>连接资源</span><span className="badge real">模型 / MCP 真实</span></div>
          <div className="resource-line"><i className={`status-dot ${config?.configured ? 'completed' : 'failed'}`} /><span className="mono truncate" title={config?.endpoint}>{config?.endpoint || '等待 /api/config'}</span></div>
          {config?.deployments.decision && <div className="resource-line" title="classify_intent 调用真实 Decision 模型"><span className="badge real">真实 Decision</span><span className="mono truncate">{config.deployments.decision}</span></div>}
          {config?.mcp.map(server => {
            const catalog = (studio.data.catalogs.length ? studio.data.catalogs : config.catalogs)?.find(item => item.server === server.name);
            return <details className="resource-detail" key={server.name}><summary><i className={`status-dot ${server.configured ? 'completed' : 'failed'}`} />{server.name} <span className="badge real">真实 MCP</span></summary><p className="mono help break">{server.url}</p><p className="help">{server.configured ? '已配置，非健康检查' : '未配置'} · {server.tools?.length ?? 0} 个工具</p><p className="help mono">目录 · {catalog?.state ?? 'unloaded'} / {catalog?.version ?? '未发现'}</p>
              {config.managed_tools && <button className="text-button" disabled={!!refreshingCatalog} onClick={async () => {
                setRefreshingCatalog(server.name);
                try { const result = await refreshCatalog([server.name], config.csrf); studio.updateCatalogs(result.catalogs); }
                catch (cause) { studio.report(cause); }
                finally { setRefreshingCatalog(''); }
              }}>{refreshingCatalog === server.name ? '刷新目录中…' : '刷新目录'}</button>}
            </details>;
          })}
          <details className="resource-detail"><summary><span className="badge mock">Mock</span>业务工具 · 自动执行</summary><p className="help">{mockTools.length ? mockTools.join(' / ') : '工具定义由服务端默认配置提供。'}</p><p className="help">保留意图和参数校验，无需额外口头确认或页面点击。</p></details>
          <p className="help">会话上限 {config?.maxSessionSeconds ?? '—'} 秒 · 不在本地存储凭据、音频或对话</p>
        </div>
      </aside>

      <main className="studio-main">
        <section className="control-deck panel" aria-label="音频与会话控制">
          <div className="session-controls">{active ? <button className="button end-button" disabled={studio.phase === 'closing'} onClick={() => void studio.close()}><Icon name="stop" size={15} />{studio.phase === 'closing' ? '结束中…' : studio.phase === 'connecting' ? '取消连接' : '结束会话'}</button>
            : <button className="button primary start-button" disabled={loading || !config?.configured || invalid} onClick={() => {
              try { validateSession(session, transport); validateManagedTools(session); startup.current = structuredClone(session); void studio.start(session, transport, fileOnly, input, output); }
              catch (cause) { studio.report(cause); }
            }}><Icon name="play" size={16} />{loading ? '读取配置中…' : '开始会话'}</button>}
            <span className="transport-badge mono">{transport === 'webrtc' ? 'WEBRTC' : fileOnly ? 'WS / FILE' : 'WS / MIC'}</span>
          </div>
          <div className="audio-controls"><button className={`control-button ${studio.muted ? 'is-muted' : ''}`} disabled={!live || !!studio.busy} aria-pressed={studio.muted} onClick={() => void studio.toggleMute()}><Icon name="mic" size={17} /><span>{studio.muted ? '取消输入静音' : '输入静音'}</span></button>
            <button className={`control-button ${studio.speakerMuted ? 'is-muted' : ''}`} disabled={!live} aria-pressed={studio.speakerMuted} onClick={studio.muteSpeaker}><Icon name="volume" size={17} /><span>{studio.speakerMuted ? '打开扬声器' : '扬声器静音'}</span></button>
            <button className="control-button" disabled={!live} onClick={() => void studio.resume()} title="恢复被浏览器暂停的音频"><Icon name="refresh" size={16} /><span>恢复播放</span></button>
            <button className="control-button upload-button" disabled={!live || transport !== 'websocket' || !!studio.upload?.active || studio.muted} onClick={() => fileInput.current?.click()} title={transport === 'webrtc' ? '文件输入仅适用于 WebSocket' : '20 MB / 120 秒以内；自动解码和重采样'}><Icon name="upload" size={17} /><span>发送音频</span></button>
            <input ref={fileInput} type="file" accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac,.webm" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void studio.uploadFile(file); }} />
          </div>
        </section>
        <div className="session-notice" role="status"><i className={`status-dot ${live ? 'running' : ''}`} /><span>{studio.notice || '选择传输方式，检查启动参数，然后开始一次真实对话。'}</span><span className="mono session-clock">T+ {(studio.now / 1000).toFixed(1)}s</span></div>
        <WorkflowPanel data={studio.data} managed={config?.managed_tools === true} />
        {studio.untimedAudio && <p className="audio-timing-warning" role="status"><Icon name="alert" size={14} />原生输出未提供时间戳，兼容模式按到达顺序播放；无法精确还原服务端音频间隙。追踪仍使用后端共享时钟。</p>}
        {studio.upload && <div className="upload-progress panel"><Icon name="upload" size={17} /><div><b>{studio.upload.name}</b><small>{studio.upload.active ? '实时发送 · 麦克风暂时暂停' : studio.upload.progress === 100 ? '发送完成' : '发送已停止'}</small></div><progress max={100} value={studio.upload.progress} /><span className="mono">{studio.upload.progress}%</span>{studio.upload.active && <button className="text-button" onClick={studio.cancelUpload}>取消</button>}</div>}
        <ToolCalls studio={studio} mcpNames={mcpNames} mockNames={mockNames} />
        <div className="stage-grid">
          <Waterfall spans={studio.data.spans} now={studio.now} live={live} selected={selectedSpan} select={setSelectedSpan} />
          <Conversation fragments={studio.data.fragments} total={studio.data.transcriptCount} />
          <Debugger events={studio.events} live={live} send={studio.command} report={studio.report} />
          <Inspector data={studio.data} span={studio.data.spans.find(span => span.id === selectedSpan)} now={studio.now} threshold={config?.threshold} />
        </div>
        <footer className="workspace-footer"><span><Icon name="shield" size={13} />业务工具为 Mock；模型推理与 MCP 调用是真实请求</span><span>文件 ≤ 20 MB / 120 s · 调试缓存 800 帧 · 转写 3000 片段</span></footer>
      </main>
    </div>
  </div>;
}

export default function App() {
  return <AuthGate>{access => <StudioApp {...access} />}</AuthGate>;
}
