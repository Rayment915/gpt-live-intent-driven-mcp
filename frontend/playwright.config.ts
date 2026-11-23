import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:4179',
    browserName: 'chromium',
    channel: process.env.STUDIO_BROWSER_CHANNEL || 'chrome',
    headless: true,
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required'] },
    viewport: { width: 1512, height: 1100 },
  },
  webServer: {
    command: 'npm exec vite preview -- --host 127.0.0.1 --port 4179 --strictPort',
    url: 'http://127.0.0.1:4179',
    reuseExistingServer: false,
    timeout: 20000,
  },
});
