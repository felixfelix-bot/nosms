import { themeGet, themeOnChanged, type Subscription, type Theme } from '@napplet/sdk';
import {
  cvmAvailability,
  discoverServers,
  probe,
  smsCapabilities,
  smsDocs,
  smsPricing,
  smsSend,
  smsStatus,
  type Capabilities,
  type Pricing,
} from './cvm.js';
import { InvalidDestination, formatSats, normalizeE164, priceFor, zoneLabel } from './pricing.js';
import { runtimeHasDomain } from './domain-availability.js';
import contractText from './contract/llms.txt?raw';
import contractFullText from './contract/llms-full.txt?raw';
import './styles.css';

type StatusKind = 'idle' | 'ok' | 'warn' | 'error';

// Build-time pinned identity and relays (vite `define`). Empty string means "no
// pin": the napplet then resolves the server by discovery alone.
//
// `__NOSMS_CVM_PUBKEY__` is HEX because that is what the shell's `cvm.callTool`
// accepts (measured 2026-10-05: an npub makes it fail with "Input string must
// contain hex characters in even length"). `__NOSMS_CVM_NPUB__` is the same
// identity in the form a human recognises, for display.
declare const __NOSMS_CVM_PUBKEY__: string;
declare const __NOSMS_CVM_NPUB__: string;
declare const __NOSMS_CVM_RELAYS__: string;

const DEFAULT_SERVER_PUBKEY = __NOSMS_CVM_PUBKEY__;
const DEFAULT_SERVER_NPUB = __NOSMS_CVM_NPUB__;
const DEFAULT_SERVER_RELAYS = (__NOSMS_CVM_RELAYS__
  ? __NOSMS_CVM_RELAYS__.split(/[\s,]+/).filter(Boolean)
  : ['wss://relay.primal.net', 'wss://nos.lol']);

const elements = {
  status: requireElement<HTMLOutputElement>('#status'),
  output: requireElement<HTMLPreElement>('#output'),
  toInput: requireElement<HTMLInputElement>('#toInput'),
  bodyInput: requireElement<HTMLTextAreaElement>('#bodyInput'),
  lookupInput: requireElement<HTMLInputElement>('#lookupInput'),
  priceValue: requireElement<HTMLSpanElement>('#priceValue'),
  priceSource: requireElement<HTMLSpanElement>('#priceSource'),
  capWarning: requireElement<HTMLParagraphElement>('#capWarning'),
  factRail: requireElement<HTMLElement>('#factRail'),
  factBestEffort: requireElement<HTMLElement>('#factBestEffort'),
  factReceipts: requireElement<HTMLElement>('#factReceipts'),
  factServer: requireElement<HTMLElement>('#factServer'),
  sendButton: requireElement<HTMLButtonElement>('#sendButton'),
  connectButton: requireElement<HTMLButtonElement>('#connectButton'),
  contractButton: requireElement<HTMLButtonElement>('#contractButton'),
  lookupButton: requireElement<HTMLButtonElement>('#lookupButton'),
};

/** The selected server. Populated by discovery, else the build-time default. */
let server: { pubkey: string; relays: string[]; name?: string } | null = null;
/** Live price table from the server, when it answers. */
let livePricing: Pricing | null = null;
let themeSubscription: Subscription | null = null;

function requireElement<T extends HTMLElement>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Missing required element: ${selector}`);
  return element;
}

function setStatus(kind: StatusKind, message: string): void {
  elements.status.className = `status status-${kind}`;
  elements.status.textContent = message;
}

function setOutput(value: unknown): void {
  elements.output.textContent =
    typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function shortKey(value: string): string {
  if (!value) return 'none';
  return value.length <= 18 ? value : `${value.slice(0, 10)}…${value.slice(-6)}`;
}

/**
 * Render the capability warning FROM THE SERVER'S OWN FLAGS. When the server has
 * not answered yet the warning says exactly that, instead of asserting a promise
 * the rail may not keep.
 */
function renderCapabilities(caps: Capabilities | null, reason?: string): void {
  if (!caps) {
    elements.factRail.textContent = '–';
    elements.factBestEffort.textContent = '–';
    elements.factReceipts.textContent = '–';
    elements.capWarning.textContent = reason
      ? `Capabilities unknown — ${reason}. Nothing is assumed about delivery.`
      : 'Capabilities unknown — connect to read them from the server.';
    elements.capWarning.dataset.state = 'unknown';
    return;
  }
  elements.factRail.textContent = String(caps.rail ?? '–');
  elements.factBestEffort.textContent = caps.best_effort === true ? 'yes' : 'no';
  elements.factReceipts.textContent = caps.delivery_receipts === true ? 'yes' : 'no';
  if (caps.delivery_receipts === false) {
    elements.capWarning.textContent =
      'Accepted by the rail means accepted — NOT delivered. No delivery receipt is possible'
      + (caps.best_effort === true ? ', and the rail is best-effort.' : '.');
    elements.capWarning.dataset.state = 'on';
  } else {
    elements.capWarning.textContent = 'The server reports delivery receipts are available.';
    elements.capWarning.dataset.state = 'off';
  }
}

/**
 * Live price from the server's own table, or null when the server did not answer.
 *
 * The server returns the whole table (`prefixes`) plus, when it was given a
 * destination, the price for that destination. The bundled table is the
 * fallback; it is generated from the same Python source, but the server's answer
 * always wins — a price shown to a user must be the price the service meters.
 */
function livePrice(dest: string, live: Pricing | null): number | null {
  if (!live) return null;
  // `price` is authoritative when present: the server already resolved it.
  if (typeof live.price === 'number') return live.price;
  let number: string;
  try {
    number = normalizeE164(dest);
  } catch {
    return null;
  }
  const prefixes = live.prefixes;
  if (prefixes && typeof prefixes === 'object') {
    const match = Object.keys(prefixes)
      .filter((prefix) => number.startsWith(prefix))
      .sort((a, b) => b.length - a.length)[0];
    if (match !== undefined) return prefixes[match];
  }
  return typeof live.default === 'number' ? live.default : null;
}

/** Price shown before a send. Uses the server's live table when available. */
function renderPrice(): void {
  const raw = elements.toInput.value.trim();
  if (!raw) {
    elements.priceValue.textContent = 'enter a number';
    elements.priceSource.textContent = 'bundled table';
    elements.sendButton.disabled = true;
    return;
  }

  let bundled: number;
  let zone: string;
  try {
    bundled = priceFor(raw);
    zone = zoneLabel(raw);
  } catch (error) {
    elements.priceValue.textContent =
      error instanceof InvalidDestination ? 'invalid number' : 'invalid';
    elements.priceSource.textContent = 'bundled table';
    elements.sendButton.disabled = true;
    return;
  }

  const live = livePrice(raw, livePricing);
  const sats = live ?? bundled;
  elements.priceValue.textContent = `${formatSats(sats)} · ${zone}`;
  elements.priceSource.textContent = live !== null ? 'live from server' : 'bundled table';
  elements.sendButton.disabled = !server?.pubkey;
}

function renderServer(): void {
  const npub = server && server.pubkey === DEFAULT_SERVER_PUBKEY ? DEFAULT_SERVER_NPUB : undefined;
  elements.factServer.textContent = server
    ? `${displayId(server.pubkey, npub)}${server.name ? ` (${server.name})` : ''}`
    : 'none';
  elements.connectButton.disabled = !cvmAvailability().domain;
  renderPrice();
}

function contractDocument(): string {
  return [
    '# bundled contract — llms.txt',
    '# (the server returns the same body verbatim from its `docs` tool)',
    '',
    contractText.trim(),
    '',
    '---',
    '',
    contractFullText.trim(),
  ].join('\n');
}

/** True when the build pinned a server identity (direct resolution). */
function hasPinnedServer(): boolean {
  return /^[0-9a-f]{64}$/i.test(DEFAULT_SERVER_PUBKEY);
}

/**
 * Render an identity for a human: npub when we have it, else the short hex.
 */
function displayId(pubkeyHex: string, npub?: string): string {
  if (!pubkeyHex) return 'none';
  return npub && npub.length > 12 ? npub : shortKey(pubkeyHex);
}

/**
 * Read the server's live facts. Every step is fallible and degrades visibly.
 *
 * Resolution is shown as the card asks: FIRST the direct path (the pinned npub,
 * if the build has one), then the public path (discovery, CEP-6 announcements).
 * Whichever answered first is the one reported.
 */
async function connect(): Promise<void> {
  const availability = cvmAvailability();
  if (!availability.domain) {
    setStatus('error', 'This shell does not expose the cvm domain.');
    setOutput('NAP-CVM (cvm) is a hard requirement: the napplet asks the shell to call the server.');
    return;
  }
  setStatus('idle', 'Discovering servers');

  const resolution: {
    direct: { npub: string; pubkey: string; relays: string[] } | null;
    discovered: Array<{ pubkey: string; name?: string }>;
    chosen: 'direct' | 'discovery' | 'none';
    error?: string;
  } = { direct: null, discovered: [], chosen: 'none' };

  // --- path 1: direct, by the pinned npub --------------------------------
  if (hasPinnedServer()) {
    resolution.direct = {
      npub: DEFAULT_SERVER_NPUB,
      pubkey: DEFAULT_SERVER_PUBKEY,
      relays: DEFAULT_SERVER_RELAYS,
    };
  }

  // --- path 2: public, via the CEP-6 announcement cache -------------------
  const discovery = await discoverServers('sms');
  resolution.discovered = discovery.servers.map((s) => ({ pubkey: s.pubkey, name: s.name }));
  if (discovery.error) resolution.error = discovery.error;

  if (resolution.direct) {
    // The pin wins: it is the identity this build was made for. But never blind
    // to discovery — say so when the relays disagree.
    server = { pubkey: DEFAULT_SERVER_PUBKEY, relays: DEFAULT_SERVER_RELAYS, name: 'nosms' };
    resolution.chosen = 'direct';
  } else {
    const found = discovery.servers[0];
    if (found?.pubkey) {
      server = { pubkey: found.pubkey, relays: found.relays ?? [], name: found.name };
      resolution.chosen = 'discovery';
    } else {
      renderServer();
      setStatus('warn', 'No nosms server found');
      setOutput(
        [
          'Discovery found no server with an `sms` capability, and this build pins none.',
          discovery.error ? `discovery error: ${discovery.error}` : '',
          `cvm methods visible: ${availability.methods.join(', ') || 'none'}`,
        ].filter(Boolean).join('\n'),
      );
      return;
    }
  }

  renderServer();
  setStatus('idle', 'Reading the live contract');

  const [caps, pricing] = await Promise.all([
    smsCapabilities(server),
    smsPricing(server),
  ]);

  livePricing = pricing.ok ? (pricing.value as Pricing) : null;
  if (caps.ok) {
    renderCapabilities(caps.value as Capabilities);
  } else {
    renderCapabilities(null, caps.error);
  }
  renderPrice();

  setStatus(caps.ok || pricing.ok ? 'ok' : 'warn', caps.ok ? 'Contract read from server' : 'Server did not answer');
  setOutput({
    resolved_by: resolution.chosen,
    direct: resolution.direct,
    discovery: {
      servers: resolution.discovered,
      error: resolution.error ?? null,
    },
    server,
    cvm: availability,
    capabilities: caps.ok ? caps.value : { error: caps.error },
    pricing: pricing.ok ? pricing.value : { error: pricing.error },
  });
}

async function send(): Promise<void> {
  if (!server?.pubkey) {
    setStatus('warn', 'No server');
    return;
  }
  let to = '';
  try {
    to = normalizeE164(elements.toInput.value);
  } catch {
    setStatus('error', 'Enter an E.164 number');
    return;
  }
  const body = elements.bodyInput.value.trim();
  if (!body) {
    setStatus('error', 'Message is empty');
    return;
  }

  setStatus('idle', 'Sending — the shell may prompt to pay');
  elements.sendButton.disabled = true;
  try {
    const result = await smsSend(server, to, body);
    if (!result.ok) {
      setStatus('error', 'Send failed');
      setOutput({ error: result.error, note: 'A shell-side payment refusal is expected when the fee exceeds the allowance.' });
      return;
    }
    const outcome = result.value as Record<string, unknown>;
    if (typeof outcome.id === 'string') elements.lookupInput.value = outcome.id;
    // "Accepted" is the only claim this rail can support; never say "delivered".
    const accepted = outcome.accepted === true;
    setStatus(accepted ? 'ok' : 'warn', accepted ? 'Accepted by the rail' : 'Send reported not accepted');
    setOutput({
      ...outcome,
      _note: 'accepted means accepted by the rail; this rail cannot confirm delivery',
    });
  } finally {
    elements.sendButton.disabled = !server?.pubkey;
  }
}

async function lookup(): Promise<void> {
  if (!server?.pubkey) {
    setStatus('warn', 'No server');
    return;
  }
  const id = elements.lookupInput.value.trim();
  if (!id) {
    setStatus('warn', 'Enter a send id');
    return;
  }
  setStatus('idle', 'Looking up');
  const result = await smsStatus(server, id);
  setStatus(result.ok ? 'ok' : 'error', result.ok ? 'Lookup complete' : 'Lookup failed');
  setOutput(result.ok ? result.value : { error: result.error });
}

async function showContract(): Promise<void> {
  const bundled = contractDocument();
  if (!server?.pubkey) {
    setStatus('warn', 'No server — showing the bundled contract');
    setOutput(bundled);
    return;
  }
  setStatus('idle', 'Fetching the contract from the server');
  const live = await smsDocs(server);
  if (!live.ok) {
    setStatus('warn', 'Server contract unavailable — showing the bundled copy');
    setOutput(`${bundled}\n\n---\n# server said: ${live.error}`);
    return;
  }
  const liveText = typeof live.value === 'string' ? live.value : JSON.stringify(live.value, null, 2);
  const identical = liveText.trim() === contractText.trim();
  setStatus('ok', identical ? 'Server contract matches the bundled copy' : 'Server contract differs from the bundled copy');
  setOutput(
    [
      identical ? 'server contract == bundled llms.txt (verbatim)' : 'server contract != bundled llms.txt',
      '',
      liveText,
    ].join('\n'),
  );
}

function applyTheme(theme: Theme): void {
  const root = document.documentElement.style;
  root.setProperty('--bg', theme.colors.background);
  root.setProperty('--fg', theme.colors.text);
  root.setProperty('--primary', theme.colors.primary);
  document.body.style.backgroundColor = theme.colors.background;
  document.body.style.color = theme.colors.text;
}

function subscribeToTheme(): void {
  if (!runtimeHasDomain('theme')) return;
  try {
    themeGet().then(applyTheme).catch(() => undefined);
    themeSubscription = themeOnChanged(applyTheme);
  } catch {
    // Theme is optional; the stylesheet fallback palette stands.
  }
}

function handle(action: () => Promise<void>): void {
  action().catch((error: unknown) => {
    setStatus('error', 'Action failed');
    setOutput(error instanceof Error ? error.message : error);
  });
}

elements.toInput.addEventListener('input', renderPrice);
elements.sendButton.addEventListener('click', () => handle(send));
elements.connectButton.addEventListener('click', () => handle(connect));
elements.contractButton.addEventListener('click', () => handle(showContract));
elements.lookupButton.addEventListener('click', () => handle(lookup));

window.addEventListener('beforeunload', () => {
  themeSubscription?.close();
});

// --- boot ------------------------------------------------------------------
// Boot-time side effects must be incapable of breaking boot: a failure here
// degrades visibly instead of throwing.
renderCapabilities(null);
renderServer();
subscribeToTheme();

if (hasPinnedServer()) {
  server = { pubkey: DEFAULT_SERVER_PUBKEY, relays: DEFAULT_SERVER_RELAYS, name: 'nosms' };
  renderServer();
}

if (!cvmAvailability().domain) {
  setStatus('warn', 'cvm domain absent in this shell');
  setOutput('This napplet needs NAP-CVM (cvm). Open it in a shell that implements it (Kehto/Paja).');
} else {
  setOutput('Ready. Connect to read the server’s live pricing and capability flags.');
  void probe(server)
    .then((diagnosis) => {
      if (!diagnosis.ok && !server?.pubkey) setStatus('idle', 'Ready — connect to read the live contract');
    })
    .catch(() => undefined);
}
