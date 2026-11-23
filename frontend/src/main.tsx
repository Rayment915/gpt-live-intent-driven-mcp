import { Component, type ErrorInfo, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

class ErrorBoundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {};
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('Studio UI error', error, info); }
  render() {
    if (this.state.error) return <main className="fatal-error"><h1>工作台界面发生错误</h1><p>{this.state.error.message}</p><p>刷新会结束当前浏览器音频连接；若会话仍在服务端运行，请等待配置的会话上限。</p><button onClick={() => location.reload()}>重新载入</button></main>;
    return this.props.children;
  }
}

createRoot(document.getElementById('root')!).render(<ErrorBoundary><App /></ErrorBoundary>);
