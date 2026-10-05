/**
 * Unit tests for the nosms napplet.
 *
 * These are the RED-first tests for the two pure modules the UI depends on:
 * destination pricing (bundled table == the service's table) and the CVM
 * wrapper's honest degradation when the shell is partial or absent.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  InvalidDestination,
  PRICING,
  formatSats,
  normalizeE164,
  priceFor,
  zoneLabel,
} from './pricing.js';
import {
  callTool,
  cvmAvailability,
  discoverServers,
  probe,
  smsCapabilities,
  smsDocs,
  smsPricing,
  smsSend,
  smsStatus,
} from './cvm.js';

const SERVER = { pubkey: 'a'.repeat(64), relays: ['wss://relay.primal.net'] };

type NappletWindow = Window & { napplet?: Record<string, unknown> };

function installShell(cvm: Record<string, unknown> | undefined): void {
  const win = window as NappletWindow;
  if (cvm === undefined) delete win.napplet;
  else win.napplet = { cvm };
}

afterEach(() => {
  installShell(undefined);
  vi.restoreAllMocks();
});

// --- pricing (ADR-0002: flat) ----------------------------------------------

// Destination numbers are CONSTRUCTED, never written as long digit runs.
const e164 = (cc: string, national: string) => `+${cc}${national}`;
const US = e164('1', '415' + '555' + '0100');
const DE = e164('49', '151' + '1234' + '5678');
const IN = e164('91', '98765' + '43210');
const UNKNOWN = e164('999', '123456789');

describe('pricing', () => {
  it('prices every destination at the same flat ADR-0002 default', () => {
    expect(priceFor(US)).toBe(2900);
    expect(priceFor(DE)).toBe(2900);
    expect(priceFor(IN)).toBe(2900);
    expect(PRICING.default).toBe(2900);
    expect(PRICING.model).toBe('flat');
    // no per-prefix table survives
    expect((PRICING as unknown as { prefixes?: unknown }).prefixes).toBeUndefined();
  });

  it('never prices an unknown destination at zero — unknown takes the documented default', () => {
    const price = priceFor(UNKNOWN);
    expect(price).toBe(PRICING.default);
    expect(price).toBeGreaterThan(0);
  });

  it('still validates a destination before pricing it', () => {
    expect(priceFor('+1')).toBe(PRICING.default);
    expect(() => priceFor('not a number')).toThrow(InvalidDestination);
  });

  it('strips formatting', () => {
    expect(normalizeE164(`+1 (${'415'}) ${'555'}-${'0100'}`)).toBe(US);
  });

  it('rejects input with no digits', () => {
    expect(() => normalizeE164('not a number')).toThrow(InvalidDestination);
  });

  it('labels one zone: the rail is flat worldwide', () => {
    expect(zoneLabel(US)).toBe('flat (worldwide)');
    expect(zoneLabel(DE)).toBe('flat (worldwide)');
  });

  it('formats sat amounts with the unit', () => {
    expect(formatSats(100)).toBe('100 sats');
    expect(formatSats(2900)).toBe('2900 sats');
  });
});

// --- cvm degradation -------------------------------------------------------

describe('cvm wrapper degradation', () => {
  it('reports the cvm domain absent when the shell injected nothing', () => {
    installShell(undefined);
    expect(cvmAvailability()).toEqual({ domain: false, methods: [] });
  });

  it('feature-detects METHODS, not just the domain (a partial shell)', () => {
    installShell({ discover: () => Promise.resolve([]) });
    const availability = cvmAvailability();
    expect(availability.domain).toBe(true);
    expect(availability.methods).toEqual(['discover']);
  });

  it('returns a reason instead of throwing when the domain is absent', async () => {
    installShell(undefined);
    const result = await smsPricing(SERVER);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/cvm domain not injected/);
  });

  it('returns a reason when the method is missing from a partial shell', async () => {
    installShell({ discover: () => Promise.resolve([]) });
    const result = await smsSend(SERVER, '+14155550100', 'hi');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/callTool is not implemented/);
  });

  it('never calls into the shell without a server pubkey', async () => {
    const callToolMock = vi.fn();
    installShell({ callTool: callToolMock });
    const result = await callTool({ pubkey: '' }, 'sms.pricing');
    expect(result.ok).toBe(false);
    expect(callToolMock).not.toHaveBeenCalled();
  });

  it('surfaces a shell refusal (payment denied) as an error, not a crash', async () => {
    installShell({
      callTool: () => Promise.reject(new Error('payment denied')),
    });
    const result = await smsSend(SERVER, '+14155550100', 'hi');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toBe('payment denied');
  });

  it('never throws on boot probe — a broken shell yields a diagnosis only', async () => {
    installShell(undefined);
    await expect(probe(null)).resolves.toMatchObject({ ok: false });
  });
});

// --- MCP text payloads -----------------------------------------------------

describe('MCP tool results are text content blocks', () => {
  it('parses a JSON text block into an object', async () => {
    installShell({
      callTool: () =>
        Promise.resolve({ content: [{ type: 'text', text: '{"best_effort":true,"delivery_receipts":false}' }] }),
    });
    const caps = await smsCapabilities(SERVER);
    expect(caps.ok).toBe(true);
    if (caps.ok) {
      expect(caps.value.best_effort).toBe(true);
      expect(caps.value.delivery_receipts).toBe(false);
    }
  });

  it('hands back prose verbatim (the docs tool returns a document)', async () => {
    installShell({
      callTool: () => Promise.resolve({ content: [{ type: 'text', text: '# nosms — SMS for agents' }] }),
    });
    const docs = await smsDocs(SERVER);
    expect(docs.ok).toBe(true);
    if (docs.ok) expect(docs.value).toContain('# nosms — SMS for agents');
  });

  it('reports an error when the server returns no text content', async () => {
    installShell({ callTool: () => Promise.resolve({ content: [] }) });
    const result = await smsStatus(SERVER, 'abc');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/no text content/);
  });

  it('translates a transport error into a reason', async () => {
    installShell({ callTool: () => Promise.reject(new Error('relay timeout')) });
    const result = await smsPricing(SERVER);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toBe('relay timeout');
  });
});

// --- discovery -------------------------------------------------------------

describe('discovery', () => {
  it('returns an empty list with a reason when cvm is absent', async () => {
    installShell(undefined);
    const result = await discoverServers('sms');
    expect(result.servers).toEqual([]);
    expect(result.error).toMatch(/cvm domain not injected/);
  });

  it('returns the servers the shell resolved', async () => {
    installShell({
      discover: () => Promise.resolve([{ pubkey: 'b'.repeat(64), name: 'nosms', paymentRequired: true }]),
    });
    const result = await discoverServers('sms');
    expect(result.error).toBeUndefined();
    expect(result.servers[0].name).toBe('nosms');
  });
});
