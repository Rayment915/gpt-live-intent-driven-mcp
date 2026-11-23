import { useCallback, useEffect, useRef, useState } from 'react';
import { createSession, eventsUrl, sendCommand } from './api';
import { AudioEngine } from './audio/engine';
import { prepareSession } from './config';
import { appendBounded, debugFrame, eventTime, nestedEvent } from './protocol';
import { initialData, receiveFrame } from './state';
import { message, object, type CatalogStatus, type DebugEvent, type JsonObject, type Phase, type StudioConfig, type Transport } from './types';

interface Runtime {
  generation: number;
  audio: AudioEngine;
  socket?: WebSocket;
  peer?: RTCPeerConnection;
  channel?: RTCDataChannel;
  id?: string;
  transport: Transport;
  closing: boolean;
  released: boolean;
  final: boolean;
  timeout?: ReturnType<typeof setTimeout>;
  limit?: ReturnType<typeof setTimeout>;
  inputMuted: boolean;
}

async function gathered(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === 'complete') return;
  await new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer);
      peer.removeEventListener('icegatheringstatechange', changed);
    };
    const changed = () => {
      if (peer.iceGatheringState === 'complete') { cleanup(); resolve(); }
    };
    const timer = setTimeout(() => { cleanup(); reject(new Error('WebRTC ICE 收集超时，请检查网络或改用 WebSocket')); }, 10000);
    peer.addEventListener('icegatheringstatechange', changed);
  });
}

export function useStudio(config: StudioConfig | undefined) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [id, setId] = useState('');
  const [data, setData] = useState(initialData);
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [muted, setMuted] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [untimedAudio, setUntimedAudio] = useState(false);
  const [upload, setUpload] = useState<{ name: string; progress: number; active: boolean }>();
  const [busy, setBusy] = useState('');
  const [now, setNow] = useState(0);
  const runtime = useRef<Runtime | undefined>(undefined);
  const sequence = useRef(0);
  const generation = useRef(0);
  const uploadGeneration = useRef(0);
  const clock = useRef({ ms: 0, received: performance.now() });
  const configRef = useRef(config);
  configRef.current = config;

  const log = useCallback((frame: JsonObject, direction: DebugEvent['direction'] = 'in') => {
    const entry: DebugEvent = { seq: ++sequence.current, received: Date.now(), direction, type: String(frame.type ?? 'unknown'), frame: debugFrame(frame) };
    setEvents(previous => appendBounded(previous, entry, 800));
  }, []);

  const report = useCallback((cause: unknown) => {
    const text = message(cause);
    setError(text);
    log({ type: 'studio.browser_error', message: text }, 'local');
  }, [log]);

  const release = useCallback(async (rt: Runtime) => {
    if (rt.released) return;
    rt.released = true;
    clearTimeout(rt.timeout);
    clearTimeout(rt.limit);
    rt.channel?.close();
    rt.peer?.close();
    rt.socket?.close(1000, 'studio cleanup');
    await rt.audio.stop();
    if (runtime.current === rt) setUpload(previous => previous ? { ...previous, active: false } : undefined);
  }, []);

  const close = useCallback(async () => {
    const rt = runtime.current;
    const currentConfig = configRef.current;
    if (!rt || rt.released || rt.closing) return;
    rt.closing = true;
    setPhase('closing');
    await rt.audio.stop().catch(report);
    if (!rt.id || !currentConfig) {
      generation.current++;
      await release(rt).catch(report);
      setPhase('closed');
      setNotice('连接已取消；没有已确认的最终用量。');
      return;
    }
    rt.timeout = setTimeout(() => {
      if (rt.released) return;
      setNotice('结束等待超时：未收到 session.closed，最终用量尚未确认。');
      setPhase('error');
      void release(rt).catch(report);
    }, 10000);
    try {
      const event = { type: 'session.close' };
      await sendCommand(rt.id, event, currentConfig.csrf);
      log(event, 'out');
      if (!rt.released && runtime.current === rt) setNotice('已请求结束，等待 session.closed 最终用量。');
    } catch (cause) {
      if (rt.final || runtime.current !== rt) return;
      report(cause);
      setNotice('结束命令失败，最终用量未确认；后端会话可能仍在运行。');
      setPhase('error');
      await release(rt).catch(report);
    }
  }, [log, release, report]);

  const start = useCallback(async (session: JsonObject, transport: Transport, fileOnly: boolean, input: string, output: string) => {
    const cfg = configRef.current;
    if (!cfg?.configured) { report(new Error('后端尚未配置模型凭据；请先完成服务端配置')); return; }
    if (runtime.current && !runtime.current.released) return;
    let prepared: JsonObject;
    try { prepared = prepareSession(session, transport); }
    catch (cause) { report(cause); return; }
    setError(''); setNotice(''); setData(initialData()); setEvents([]); setId('');
    setMuted(false); setSpeakerMuted(false); setUntimedAudio(false); setUpload(undefined); setPhase('connecting'); setNow(0);
    clock.current = { ms: 0, received: performance.now() };
    const run = ++generation.current;
    const audio = new AudioEngine(cause => {
      report(cause);
      if (/发送|AudioWorklet/.test(message(cause))) void close();
    });
    const rt: Runtime = { audio, generation: run, transport, closing: false, released: false, final: false, inputMuted: false };
    runtime.current = rt;
    const started = performance.now();
    const timing = (stage: string, start: number, status: 'completed' | 'failed') => {
      const frame = { type: 'studio.browser_startup', stage, duration_ms: performance.now() - start, status };
      log(frame, 'local');
      setData(previous => receiveFrame(previous, frame));
    };
    const measure = async <T,>(stage: string, action: () => Promise<T>): Promise<T> => {
      const start = performance.now();
      try {
        const value = await action();
        timing(stage, start, 'completed');
        return value;
      } catch (cause) {
        timing(stage, start, 'failed');
        throw cause;
      }
    };
    const check = () => {
      if (run !== generation.current || rt.released || rt.closing) throw new Error('连接已取消');
    };
    try {
      await measure('音频初始化', () => audio.initialize());
      check();
      if (output) await audio.outputDevice(output);
      check();
      let stream: MediaStream | undefined;
      if (transport === 'webrtc' || !fileOnly) stream = await measure('麦克风权限与设备', () => audio.microphone(input));
      check();
      let sdp: string | undefined;
      if (transport === 'webrtc') {
        if (!window.RTCPeerConnection) throw new Error('浏览器不支持 WebRTC，请选择 WebSocket');
        const peer = new RTCPeerConnection();
        rt.peer = peer;
        for (const track of stream!.getTracks()) peer.addTrack(track, stream!);
        // The channel is negotiated natively; only the backend sideband supplies events.
        rt.channel = peer.createDataChannel('live-events');
        peer.ontrack = event => {
          if (!rt.released) void audio.remoteStream(event.streams[0] ?? new MediaStream([event.track])).catch(report);
        };
        peer.onconnectionstatechange = () => {
          if (peer.connectionState === 'failed' && !rt.released) {
            report(new Error('WebRTC 媒体连接失败；正在结束会话，请检查网络或改用 WebSocket'));
            void close();
          }
        };
        await peer.setLocalDescription(await peer.createOffer());
        await measure('WebRTC ICE 收集', () => gathered(peer));
        check();
        sdp = peer.localDescription?.sdp;
        if (!sdp) throw new Error('未能生成 WebRTC SDP offer');
      }
      const created = await measure('创建会话 HTTP', () => createSession(prepared, transport, cfg.csrf, sdp));
      if (!created.id) throw new Error('后端未返回会话 ID');
      rt.id = created.id;
      check();
      setId(created.id);
      log({ type: 'studio.session_created', id: created.id, transport }, 'local');
      const socket = new WebSocket(eventsUrl(created.id));
      rt.socket = socket;
      const sendAudio = (payload: string) => {
        if (rt.released || rt.closing) return;
        if (socket.readyState !== WebSocket.OPEN) throw new Error('音频发送失败：事件 WebSocket 已断开');
        if (socket.bufferedAmount > 1024 * 1024) throw new Error('音频发送积压超过 1 MB；网络速度不足，正在结束会话');
        socket.send(JSON.stringify({ type: 'session.input_audio.append', audio: payload }));
      };
      socket.onmessage = ({ data: raw }: MessageEvent) => {
        if (rt.released) return;
        try {
          const parsed: unknown = JSON.parse(String(raw));
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('事件帧不是 JSON 对象');
          const frame = parsed as JsonObject;
          if (typeof frame.type !== 'string') throw new Error('事件帧缺少字符串 type');
          if (frame.type === 'response.event' && typeof object(frame.event).type !== 'string') throw new Error('response.event 的嵌套 event 缺少有效 type；请检查后端 Responses 事件封装');
          log(frame);
          setData(previous => receiveFrame(previous, frame));
          // Service audio offsets and Python span offsets have different origins.
          const ms = frame.type === 'studio.span' ? eventTime(frame) : undefined;
          if (ms !== undefined && ms >= clock.current.ms) {
            clock.current = { ms, received: performance.now() };
            setNow(ms);
          }
          const event = nestedEvent(frame);
          if (frame.type === 'error' || frame.type === 'studio.error' || event.type === 'error') {
            report(new Error(String(frame.message ?? object(event.error).message ?? event.message ?? JSON.stringify(event.error ?? frame))));
          }
          if (event.type === 'response.failed' || event.type === 'response.incomplete') {
            const response = object(event.response);
            report(new Error(`Responses ${event.type === 'response.failed' ? '响应失败' : '响应未完成'}：${String(object(response.error).message ?? object(response.incomplete_details).reason ?? JSON.stringify(response.error ?? response.incomplete_details ?? {}))}`));
          }
          if (frame.type === 'session.input_audio.muted' || frame.type === 'session.input_audio.unmuted') {
            rt.inputMuted = frame.type === 'session.input_audio.muted';
            setMuted(rt.inputMuted);
            setNotice(rt.inputMuted ? '模型输入已静音；设备仍可能向服务端传输媒体。结束会话才会释放麦克风。' : '模型输入已取消静音。');
          }
          if (frame.type === 'studio.closing') {
            rt.closing = true;
            setPhase('closing');
            setNotice('服务端正在结束会话，等待最终用量确认。');
            void audio.stop().catch(report);
            rt.timeout ??= setTimeout(() => {
              if (rt.released) return;
              setNotice('服务端结束超时：最终用量未确认。');
              setPhase('error');
              void release(rt).catch(report);
            }, 10000);
          }
          if (transport === 'websocket' && /(^|\.)output_audio\.delta$/.test(String(frame.type)) && typeof frame.delta === 'string') {
            if (frame.start_ms !== undefined && (typeof frame.start_ms !== 'number' || !Number.isFinite(frame.start_ms))) throw new Error('输出音频 start_ms 不是有效数值');
            if (frame.end_ms !== undefined && (typeof frame.end_ms !== 'number' || !Number.isFinite(frame.end_ms))) throw new Error('输出音频 end_ms 不是有效数值');
            if (frame.start_ms === undefined && frame.end_ms === undefined) setUntimedAudio(true);
            audio.play(frame.delta, frame.start_ms as number | undefined, frame.end_ms as number | undefined);
          }
          if (frame.type === 'session.closed' || frame.type === 'studio.disconnected') {
            rt.final = frame.type === 'session.closed' || frame.final_usage_confirmed === true;
            setNotice(rt.final ? '会话已结束，最终用量已确认。' : '服务端已断开，但最终用量未确认。');
            setPhase('closed');
            void release(rt).catch(report);
          }
        } catch (cause) { report(cause); }
      };
      socket.onclose = event => {
        if (rt.released) return;
        setNotice(rt.final ? '会话已结束，最终用量已确认。' : `事件连接已关闭（${event.code}），最终用量未确认。`);
        if (!rt.final) report(new Error('事件源意外断开；不自动重连，以避免重复建会话。请结束后重新开始。'));
        setPhase(rt.final ? 'closed' : 'error');
        void release(rt).catch(report);
        if (!rt.final && rt.id) void sendCommand(rt.id, { type: 'session.close' }, cfg.csrf).catch(cause => {
          report(new Error(`事件连接断开后，服务端会话清理失败：${message(cause)}`));
        });
      };
      socket.onerror = () => { if (!rt.released) report(new Error('事件 WebSocket 连接失败；请检查后端与 /api WebSocket 代理')); };
      await measure('浏览器事件 WebSocket', () => new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => { socket.close(); reject(new Error('等待事件 WebSocket 超时')); }, 15000);
        socket.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
        socket.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('无法连接后端事件 WebSocket')); }, { once: true });
        socket.addEventListener('close', () => { clearTimeout(timeout); reject(new Error('事件 WebSocket 在就绪前关闭')); }, { once: true });
      }));
      check();
      if (transport === 'webrtc') {
        if (!created.sdp) throw new Error('后端未返回 WebRTC SDP answer');
        await measure('WebRTC SDP answer', () => rt.peer!.setRemoteDescription({ type: 'answer', sdp: created.sdp! }));
        await measure('WebRTC 媒体 connected', () => new Promise<void>((resolve, reject) => {
          const peer = rt.peer!;
          const cleanup = () => { clearTimeout(timer); peer.removeEventListener('connectionstatechange', changed); };
          const changed = () => {
            if (peer.connectionState === 'connected') { cleanup(); resolve(); }
            if (['failed', 'closed'].includes(peer.connectionState)) { cleanup(); reject(new Error('WebRTC 媒体未能连接')); }
          };
          const timer = setTimeout(() => { cleanup(); reject(new Error('WebRTC 媒体连接超时')); }, 20000);
          peer.addEventListener('connectionstatechange', changed);
          changed();
        }));
      } else if (fileOnly) audio.fileOnlySilence(sendAudio);
      else await audio.capture(sendAudio);
      check();
      setPhase('live');
      timing('启动总计（浏览器）', started, 'completed');
      setNotice(fileOnly ? '仅文件模式：未申请麦克风权限，文件之间发送 20 ms 静音帧。' : '音频已连接；所有协议事件来自服务端 sideband。');
      if (cfg.maxSessionSeconds > 0) rt.limit = setTimeout(() => {
        setNotice('已达到会话时长上限，正在请求结束。');
        void close();
      }, cfg.maxSessionSeconds * 1000);
    } catch (cause) {
      if (rt.closing && !rt.released && rt.id) return;
      if (!rt.closing) report(cause);
      if (rt.id && !rt.final) {
        try { await sendCommand(rt.id, { type: 'session.close' }, cfg.csrf); }
        catch (cleanupError) { report(new Error(`${message(cause)}；清理服务端会话失败：${message(cleanupError)}`)); }
      }
      await release(rt).catch(report);
      if (run === generation.current) setPhase(rt.closing ? 'closed' : 'error');
    }
  }, [close, log, release, report]);

  const command = useCallback(async (event: JsonObject, experimental = false) => {
    const rt = runtime.current;
    const cfg = configRef.current;
    if (!rt?.id || rt.released || rt.closing || !cfg) throw new Error('请先开始会话');
    const result = await sendCommand(rt.id, event, cfg.csrf, experimental);
    log(event, 'out');
    return result;
  }, [log]);

  const toggleMute = async () => {
    const rt = runtime.current;
    if (!rt || busy) return;
    setBusy('mute');
    try {
      const target = !rt.inputMuted;
      if (!rt.inputMuted && rt.audio.uploading) { rt.audio.cancelUpload(); setUpload(previous => previous ? { ...previous, active: false } : undefined); }
      await command({ type: rt.inputMuted ? 'session.input_audio.unmute' : 'session.input_audio.mute' });
      if (rt.inputMuted !== target) setNotice('静音命令已发送；等待服务端 muted / unmuted 确认。');
    } catch (cause) { report(cause); }
    finally { setBusy(''); }
  };

  const uploadFile = async (file: File) => {
    const rt = runtime.current;
    if (!rt || rt.released || rt.closing || rt.transport !== 'websocket') { report(new Error('请先开始 WebSocket 会话再发送文件')); return; }
    if (rt.inputMuted) { report(new Error('模型输入已静音；请先取消输入静音再发送文件')); return; }
    if (rt.audio.uploading) { report(new Error('已有文件正在发送')); return; }
    const run = ++uploadGeneration.current;
    setUpload({ name: file.name, progress: 0, active: true });
    try {
      const completed = await rt.audio.upload(file, audio => {
        if (rt.socket?.readyState !== WebSocket.OPEN || rt.socket.bufferedAmount > 1024 * 1024) throw new Error('文件音频发送失败：连接中断或发送积压');
        rt.socket.send(JSON.stringify({ type: 'session.input_audio.append', audio }));
      }, progress => { if (uploadGeneration.current === run) setUpload({ name: file.name, progress, active: true }); });
      if (runtime.current === rt && !rt.released && uploadGeneration.current === run) setNotice(completed ? '文件已按实时节奏发送；已恢复会话原有输入。' : '文件发送已取消。');
    } catch (cause) { if (uploadGeneration.current === run) report(cause); }
    finally { if (uploadGeneration.current === run) setUpload(previous => previous ? { ...previous, active: false } : undefined); }
  };

  const changeInput = async (deviceId: string) => {
    const rt = runtime.current;
    if (!rt || rt.released || rt.closing) return;
    const stream = await rt.audio.microphone(deviceId);
    if (rt.peer) await rt.peer.getSenders().find(sender => sender.track?.kind === 'audio')?.replaceTrack(stream.getAudioTracks()[0]);
  };

  const changeOutput = async (deviceId: string) => {
    const rt = runtime.current;
    if (rt && !rt.released) await rt.audio.outputDevice(deviceId);
  };

  useEffect(() => {
    if (phase !== 'live' && phase !== 'closing') return;
    const timer = setInterval(() => setNow(clock.current.ms + performance.now() - clock.current.received), 100);
    return () => clearInterval(timer);
  }, [phase]);

  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (runtime.current && !runtime.current.released) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', unload);
    return () => {
      window.removeEventListener('beforeunload', unload);
      const rt = runtime.current;
      if (rt) void release(rt).catch(cause => console.error('音频清理失败', cause));
    };
  }, [release]);

  return {
    phase, id, data, events, error, notice, muted, speakerMuted, untimedAudio, upload, busy, now,
    start, close, command, toggleMute, uploadFile, changeInput, changeOutput, report,
    clearError: () => setError(''),
    updateCatalogs: (catalogs: CatalogStatus[]) => setData(previous => ({ ...previous, catalogs })),
    muteSpeaker: () => {
      const next = !speakerMuted;
      runtime.current?.audio.muteSpeaker(next);
      setSpeakerMuted(next);
    },
    resume: async () => {
      try { await runtime.current?.audio.resume(); setNotice('已恢复播放。'); }
      catch (cause) { report(cause); }
    },
    cancelUpload: () => { uploadGeneration.current++; runtime.current?.audio.cancelUpload(); setUpload(previous => previous ? { ...previous, active: false } : undefined); setNotice('文件发送已取消；已恢复会话原有输入。'); },
  };
}

export type Studio = ReturnType<typeof useStudio>;
