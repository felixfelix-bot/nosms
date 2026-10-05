import { defineConfig, devices } from '@playwright/test';

/**
 * E2E config for the nosms napplet.
 *
 * The napplet only does anything inside a REAL shell, so this config drives
 * Kehto's Paja workshop (`kehto paja`, runtime on :5197) against LIVE relays and
 * a locally-run CVM wire server. Nothing is stubbed: the free tools answer from
 * the server over `wss://relay.contextvm.org` / `relay2` / `relay.primal.net`.
 *
 * Start the environment first:
 *   ../scripts/start_e2e_env.sh
 * then:
 *   pnpm exec playwright test --config playwright.config.ts
 */
const PAJA_URL = process.env.PAJA_URL ?? 'http://127.0.0.1:5197/';

export default defineConfig({
  testDir: './e2e',
  timeout: 180_000,
  expect: { timeout: 90_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
    ['json', { outputFile: 'playwright-report/results.json' }],
  ],
  use: {
    baseURL: PAJA_URL,
    // System Chrome: Playwright's bundled Chromium cannot install on this host
    // (Ubuntu 26.04) and `channel: 'chrome'` also avoids the ETXTBSY race when
    // another worker is installing browsers concurrently.
    channel: 'chrome',
    headless: true,
    viewport: { width: 1280, height: 900 },
    // Video is the deliverable: one coherent recording of the whole journey.
    video: { mode: 'on', size: { width: 1280, height: 900 } },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    launchOptions: { args: ['--no-sandbox', '--disable-dev-shm-usage'] },
  },
  projects: [{ name: 'chrome', use: { ...devices['Desktop Chrome'], channel: 'chrome' } }],
});
