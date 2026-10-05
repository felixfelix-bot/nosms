/**
 * nosms CVM — end-to-end happy path, recorded as the deliverable video.
 *
 * ONE test walks the whole journey so the recording is watchable from start to
 * finish (the user asked for the full flow, not pieced-together clips):
 *
 *   1. the Paja shell boots and injects the cvm domain into the napplet iframe;
 *   2. `Connect` resolves the nosms server BY NPUB and reads the live contract —
 *      `sms.capabilities` (best_effort / delivery_receipts=false), `sms.pricing`
 *      and `docs` come BACK from the CVM, and the panel renders them;
 *   3. `Contract` fetches `llms.txt` from the server and compares it to the
 *      bundled copy;
 *   4. a paid `sms.send` is attempted against the operator's handset;
 *   5. whatever happens at step 4 is reported HONESTLY — the rail cannot produce
 *      a delivery receipt and the UI must never imply it did.
 *
 * EVERY step is real. Relays are live, the server is a real process over real
 * Nostr, and there is no mock SMS anywhere. If the paid leg cannot complete
 * (no CEP-8 settlement is available to this shell), the test asserts the
 * HONEST failure rather than faking a success.
 */
import { expect, test, type Frame, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** Runtime facts written by scripts/start_e2e_env.sh — never hardcode these. */
function env(): { server_npub: string; paja_url: string; transport: string } {
  return JSON.parse(readFileSync(path.join(HERE, '.e2e-env.json'), 'utf8'));
}

const HANDSET = process.env.E2E_HANDSET ?? '+18102944652';
const MESSAGE = process.env.E2E_MESSAGE ?? 'nosms e2e — Playwright';

/** The napplet runs in a sandboxed iframe; Paja is the outer page. */
async function nappletFrame(page: Page): Promise<Frame> {
  await expect
    .poll(() => page.frames().find((f) => f !== page.mainFrame()) ?? null, {
      message: 'the Paja shell never mounted the napplet iframe',
      timeout: 60_000,
    })
    .not.toBeNull();
  return page.frames().find((f) => f !== page.mainFrame())!;
}

/** Approve the shell's ACL grant(s) so a real call may leave the sandbox. */
async function approveGrants(page: Page, ms = 8_000): Promise<void> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const clicked = await page
      .evaluate(() => {
        const dialog = document.querySelector('dialog[open]');
        if (!dialog) return null;
        for (const button of Array.from(dialog.querySelectorAll('button'))) {
          const label = (button.textContent ?? '').trim();
          if (['Publish', 'Approve', 'Allow', 'Accept'].includes(label)) {
            (button as HTMLButtonElement).click();
            return label;
          }
        }
        return null;
      })
      .catch(() => null);
    if (clicked) return;
    await page.waitForTimeout(400);
  }
}

/** Read the napplet's user-visible state, exactly as a human sees it. */
async function panel(frame: Frame) {
  return frame.evaluate(() => ({
    status: document.querySelector('#status')?.textContent?.trim() ?? '',
    rail: document.querySelector('#factRail')?.textContent?.trim() ?? '',
    bestEffort: document.querySelector('#factBestEffort')?.textContent?.trim() ?? '',
    receipts: document.querySelector('#factReceipts')?.textContent?.trim() ?? '',
    server: document.querySelector('#factServer')?.textContent?.trim() ?? '',
    price: document.querySelector('#priceValue')?.textContent?.trim() ?? '',
    priceSource: document.querySelector('#priceSource')?.textContent?.trim() ?? '',
    warning: document.querySelector('#capWarning')?.textContent?.trim() ?? '',
    output: document.querySelector('#output')?.textContent ?? '',
  }));
}

test.describe('nosms CVM over ContextVM — real relays, real server, no mocks', () => {
  test('resolves by npub, answers the free tools, and reports the paid send honestly', async ({
    page,
  }) => {
    const facts = env();
    test.info().annotations.push(
      { type: 'server-npub', description: facts.server_npub },
      { type: 'transport', description: facts.transport },
      { type: 'handset', description: HANDSET },
    );

    // ---- 1. boot the shell -------------------------------------------------
    await page.goto(facts.paja_url, { waitUntil: 'domcontentloaded' });
    const app = await nappletFrame(page);
    await expect(app.locator('#connectButton')).toBeVisible({ timeout: 60_000 });
    await expect(app.locator('#status')).toHaveText(/Idle|Ready|connect/i, { timeout: 30_000 });
    await page.screenshot({ path: 'e2e/stills/01-boot.png' });

    // ---- 2. resolve by npub + read the live contract ------------------------
    // The build-time default pubkey is the server we actually started, so the
    // panel resolves without a discovery round-trip; discovery is still exercised
    // by the shell (cvm.discover) during Connect.
    await Promise.all([approveGrants(page, 20_000), app.click('#connectButton')]);

    await expect(app.locator('#factRail')).not.toHaveText('–', { timeout: 90_000 });
    const afterConnect = await panel(app);

    // The server's OWN flags must come back — the UI renders them, it never
    // hardcodes them. This is the "resolves & answers" proof.
    expect(afterConnect.server).toContain('nosms');
    expect(afterConnect.rail).toBe('email_gateway');
    expect(afterConnect.bestEffort).toBe('yes');
    expect(afterConnect.receipts).toBe('no');
    expect(afterConnect.warning).toMatch(/NOT delivered/i);
    expect(afterConnect.status).toMatch(/Contract read from server/i);

    // The raw JSON the server returned, straight from the panel.
    const live = JSON.parse(afterConnect.output) as {
      resolved_by: string;
      direct: { pubkey: string } | null;
      discovery: { servers: Array<{ pubkey: string }>; error: string | null };
      server: { pubkey: string; relays: string[] };
      capabilities: Record<string, unknown>;
      pricing: Record<string, unknown>;
    };

    // --- resolution, as the card requires: direct (npub) AND public ---------
    // Direct: this build pins the server npub, so it resolves without waiting on
    // a relay round-trip. The pinned key is exactly the one we started.
    expect(live.resolved_by).toBe('direct');
    expect(live.direct?.pubkey).toBe(facts.server_npub);
    expect(live.server.pubkey).toBe(facts.server_npub);
    // Public: discovery still ran against the CEP-6 announcement cache and found
    // the same capability advertised (possibly by other instances too).
    expect(Array.isArray(live.discovery.servers)).toBe(true);
    expect(live.discovery.servers.length).toBeGreaterThan(0);

    expect(live.capabilities.delivery_receipts).toBe(false);
    expect(live.capabilities.best_effort).toBe(true);
    expect(live.pricing.unit).toBe('sats');
    await page.screenshot({ path: 'e2e/stills/02-contract.png' });

    // A real price for the operator's handset, computed from the live table.
    await app.fill('#toInput', HANDSET);
    await expect(app.locator('#priceValue')).toContainText('sats', { timeout: 15_000 });
    const priced = await panel(app);
    expect(priced.priceSource).toMatch(/live from server/i);
    const sats = Number(priced.price.match(/(\d+)/)?.[1] ?? '0');
    expect(sats).toBe(100); // +1 domestic, from the server's own table
    await page.screenshot({ path: 'e2e/stills/03-priced.png' });

    // ---- 3. fetch the contract FROM the server ------------------------------
    await Promise.all([approveGrants(page, 15_000), app.click('#contractButton')]);
    await expect(app.locator('#status')).toHaveText(/contract/i, { timeout: 60_000 });
    const contract = await panel(app);
    expect(contract.output).toContain('llms.txt');
    expect(contract.output).toMatch(/nosms — SMS for agents/);
    await page.screenshot({ path: 'e2e/stills/04-contract-fetched.png' });

    // ---- 4. the paid send (real attempt) ------------------------------------
    await app.fill('#bodyInput', MESSAGE);
    await Promise.all([approveGrants(page, 30_000), app.click('#sendButton')]);
    await expect(app.locator('#status')).not.toHaveText(/Sending/i, { timeout: 90_000 });
    const sent = await panel(app);
    await page.screenshot({ path: 'e2e/stills/05-send.png' });

    // ---- 5. honest outcome --------------------------------------------------
    // Two legitimate outcomes, both REAL:
    //   (a) the shell has no CEP-8 settlement → the server answers
    //       "payment required" and the panel says Send failed. This is the
    //       honest red the card explicitly asks for.
    //   (b) the shell paid → the rail accepted the message, and the panel must
    //       say "accepted", never "delivered".
    const accepted = /Accepted by the rail/i.test(sent.status);
    const refused = /Send failed|No server|Action failed/i.test(sent.status);
    expect(accepted || refused, `unexpected send status: ${sent.status}`).toBe(true);

    if (accepted) {
      const outcome = JSON.parse(sent.output) as Record<string, unknown>;
      expect(outcome.accepted).toBe(true);
      // The rail cannot confirm delivery; the result must carry that, not hide it.
      expect(outcome.delivery_confirmed).toBe(false);
      expect(sent.output).toMatch(/cannot confirm delivery/i);
    } else {
      // The honest failure must name the real reason, not a generic error.
      expect(sent.output).toMatch(/payment required|payment denied|no server/i);
    }
    test.info().attach('panel-after-send', {
      body: JSON.stringify(sent, null, 2),
      contentType: 'application/json',
    });
  });
});
