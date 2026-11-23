import type { CSSProperties } from 'react';

const paths = {
  play: 'm9 5 10 7-10 7V5Z',
  stop: 'M6 6h12v12H6z',
  mic: 'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0V5Zm-3 6v1a6 6 0 0 0 12 0v-1M12 18v4m-4 0h8',
  volume: 'm11 5-6 5H2v4h3l6 5V5Zm4 3a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14',
  upload: 'M12 16V3m-5 5 5-5 5 5M3 16v5h18v-5',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0-6v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1',
  moon: 'M20 14A8 8 0 0 1 10 4 8.5 8.5 0 1 0 20 14Z',
  download: 'M12 3v13m-5-5 5 5 5-5M3 17v4h18v-4',
  refresh: 'M20 7v5h-5M4 17v-5h5m-4-3a8 8 0 0 1 13-4l2 3M4 16l2 3a8 8 0 0 0 13-4',
  close: 'm6 6 12 12M6 18 18 6',
  chevron: 'm9 5 7 7-7 7',
  code: 'm8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18',
  link: 'm10 13 4-4m-6 6-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 3 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
  check: 'm4 12 5 5L20 6',
  alert: 'm12 3 10 18H2L12 3Zm0 6v5m0 3v1',
  settings: 'M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-6 0v6',
  trace: 'M3 6h7v4h11M3 12h5m4 0h9M3 18h11v-4h7',
  shield: 'm12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4Zm-4 10 3 3 5-6',
  search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6',
};

export function Icon({ name, size = 18, style }: { name: keyof typeof paths; size?: number; style?: CSSProperties }) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" style={style}><path d={paths[name]} /></svg>;
}
