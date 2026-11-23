import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { getAuth, login, logout, type AuthInfo } from '../api';
import { message } from '../types';
import { Icon } from './Icon';

type Access = { username: string | null; logout: () => Promise<void>; loggingOut: boolean };

export function AuthGate({ children }: { children: (access: Access) => ReactNode }) {
  const [auth, setAuth] = useState<AuthInfo>();
  const [error, setError] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const mutating = useRef(false);

  const check = useCallback(async () => {
    if (mutating.current) return;
    const current = generation.current;
    try {
      const info = await getAuth();
      if (typeof info.authenticated !== 'boolean' || typeof info.csrf !== 'string') {
        throw new Error('登录接口返回格式错误');
      }
      if (current === generation.current && !mutating.current) setAuth(info);
    } catch (cause) {
      if (current === generation.current) setError(message(cause));
    }
  }, []);

  useEffect(() => {
    void check();
    const expired = () => {
      generation.current++;
      setAuth(undefined); setPassword(''); setError('登录已过期，请重新登录。');
      void check();
    };
    window.addEventListener('studio:login-required', expired);
    const timer = setInterval(() => { void check(); }, 30000);
    return () => {
      window.removeEventListener('studio:login-required', expired);
      clearInterval(timer);
    };
  }, [check]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!auth || busy) return;
    generation.current++; mutating.current = true;
    setBusy(true); setError('');
    try { setAuth(await login(username, password, auth.csrf)); }
    catch (cause) { setError(message(cause)); }
    finally { setPassword(''); setBusy(false); mutating.current = false; }
  };

  const leave = async () => {
    if (!auth || busy) return;
    generation.current++; mutating.current = true;
    setBusy(true); setError('');
    setAuth(undefined);
    let left = false;
    try {
      await logout(auth.csrf);
      setAuth(undefined); setPassword('');
      left = true;
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); mutating.current = false; }
    if (left) await check();
  };

  if (auth?.authenticated) {
    return <>{error && <div className="alert error-alert" role="alert">{error}</div>}
      {children({ username: auth.username, logout: leave, loggingOut: busy })}</>;
  }
  return <div className="app-shell">
    <header className="app-header">
      <a className="brand" href="./">
        <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /><i /></span>
        <span><strong>GPT-Live <em>Studio</em></strong><small>语音协议 / 委派调试工作台</small></span>
      </a>
      <span className="badge"><Icon name="shield" size={14} />受保护的演示环境</span>
    </header>
    <main className="login-layout">
      <section className="login-intro">
        <div className="eyebrow">VOICE PROTOCOL LAB / SIGN IN</div>
        <h1>先登录，再让对话<br />沿时间展开<span className="accent">。</span></h1>
        <p>登录后使用实时语音、意图分类和动态 MCP 工具，查看每一段调用与等待。</p>
        <div className="login-chain mono">GPT-Live → Luna → Decision → MCP</div>
        <p className="help">售后业务为模拟；模型推理与 MCP 调用是真实请求。</p>
      </section>
      <section className="panel login-card" aria-label="登录工作台">
        <Icon name="shield" size={28} />
        <h2>登录工作台</h2>
        <p className="subtle">使用管理员提供的账号与密码。</p>
        {error && <div className="alert error-alert" role="alert">{error}</div>}
        {auth ? <form onSubmit={submit}>
          <label className="field"><span>用户名</span><input required autoComplete="username" maxLength={128} value={username} onChange={e => setUsername(e.target.value)} disabled={busy} /></label>
          <label className="field"><span>密码</span><input required type="password" autoComplete="current-password" maxLength={128} value={password} onChange={e => setPassword(e.target.value)} disabled={busy} /></label>
          <button className="button primary login-submit" type="submit" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
        </form> : <button className="button" disabled={busy} onClick={() => { setError(''); void check(); }}>{busy ? '正在退出…' : '检查登录服务'}</button>}
        <p className="help">登录前不会启动麦克风。退出登录会结束当前语音连接。</p>
      </section>
    </main>
  </div>;
}
