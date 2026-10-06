/**
 * NAP-CVM wrapper.
 *
 * Hard rule: the napplet never talks to relays itself and never handles keys or
 * ecash. It asks the SHELL to call the ContextVM server's MCP tools; the shell
 * owns relay routing, signing, encryption, JSON-RPC correlation and payment.
 *
 * Every call is feature-detected on the METHOD (not the domain) and wrapped in
 * try/catch with an explicit fallback: a partial shell must degrade, not crash.
 */
import { cvmCallTool, cvmDiscover, type CvmServerRef } from '@napplet/sdk';

export type Capabilities = {
  rail?: string;
  best_effort?: boolean;
  delivery_receipts?: boolean;
  countries?: string[];
  [key: string]: unknown;
};

export type Pricing = {
  unit?: string;
  /** prefix -> sats. The server's own table; the authority for a live price. */
  prefixes?: Record<string, number>;
  /** price for the prefix that matched, when a destination was supplied. */
  price?: number;
  default?: number;
  [key: string]: unknown;
};

export type SendOutcome = {
  id?: string;
  accepted?: boolean;
  rail?: string;
  best_effort?: boolean;
  [key: string]: unknown;
};

export type Diagnosis = { ok: boolean; message: string };

type CvmWindow = Window & {
  napplet?: { cvm?: Record<string, unknown> };
};

/** Feature-detect the cvm DOMAIN and its METHODS (a domain can be partial). */
export function cvmAvailability(): { domain: boolean; methods: string[] } {
  const cvm = (window as CvmWindow).napplet?.cvm as Record<string, unknown> | undefined;
  if (!cvm) return { domain: false, methods: [] };
  const methods = ['discover', 'request', 'listTools', 'callTool', 'close'].filter(
    (name) => typeof cvm[name] === 'function',
  );
  return { domain: true, methods };
}

/** Discovery result, or an empty list with a reason when the shell cannot answer. */
export async function discoverServers(search?: string): Promise<{
  servers: Array<CvmServerRef & { name?: string; description?: string; paymentRequired?: boolean }>;
  error?: string;
}> {
  const { domain, methods } = cvmAvailability();
  if (!domain) return { servers: [], error: 'cvm domain not injected by this shell' };
  if (!methods.includes('discover')) {
    return { servers: [], error: 'cvm.discover is not implemented by this shell' };
  }
  try {
    const servers = await cvmDiscover(search ? { search, limit: 10 } : { limit: 10 });
    return { servers };
  } catch (error) {
    return { servers: [], error: error instanceof Error ? error.message : String(error) };
  }
}

/** Result of an MCP tool call: parsed object, or a shown reason. */
export type ToolResult<T> = { ok: true; value: T } | { ok: false; error: string };

function parseToolPayload<T>(result: unknown): ToolResult<T> {
  // MCP tool results are content blocks, so the boundary is TEXT.
  const blocks = (result as { content?: Array<{ type?: string; text?: string }> })?.content;
  const text = Array.isArray(blocks)
    ? blocks.filter((b) => b?.type === 'text' && typeof b.text === 'string').map((b) => b.text as string).join('\n')
    : '';
  if (!text) return { ok: false, error: 'server returned no text content' };
  try {
    return { ok: true, value: JSON.parse(text) as T };
  } catch {
    // A tool may legitimately return prose (e.g. `docs`); hand back the string.
    return { ok: true, value: text as unknown as T };
  }
}

/** Call one MCP tool on the server. Never throws; returns a reason instead. */
export async function callTool<T>(
  server: CvmServerRef,
  name: string,
  args?: Record<string, unknown>,
): Promise<ToolResult<T>> {
  const { domain, methods } = cvmAvailability();
  if (!domain) return { ok: false, error: 'cvm domain not injected by this shell' };
  if (!methods.includes('callTool')) {
    return { ok: false, error: 'cvm.callTool is not implemented by this shell' };
  }
  if (!server?.pubkey) return { ok: false, error: 'no server pubkey — select or discover a server first' };
  try {
    const result = await cvmCallTool(server, name, args, { timeoutMs: 45000 });
    return parseToolPayload<T>(result);
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

export async function smsCapabilities(server: CvmServerRef): Promise<ToolResult<Capabilities>> {
  return callTool<Capabilities>(server, 'sms.capabilities');
}

export async function smsPricing(server: CvmServerRef): Promise<ToolResult<Pricing>> {
  return callTool<Pricing>(server, 'sms.pricing');
}

export async function smsStatus(server: CvmServerRef, id: string): Promise<ToolResult<SendOutcome>> {
  return callTool<SendOutcome>(server, 'sms.status', { id });
}

/** The paid path. The shell answers CEP-8 `payment_required` and pays; we never hold a token. */
export async function smsSend(
  server: CvmServerRef,
  to: string,
  body: string,
): Promise<ToolResult<SendOutcome>> {
  return callTool<SendOutcome>(server, 'sms.send', { to, body });
}

/** The contract tool. Returns the llms.txt body verbatim. */
export async function smsDocs(server: CvmServerRef): Promise<ToolResult<string>> {
  return callTool<string>(server, 'docs');
}

/**
 * A cheap reachability probe used on boot. It must be incapable of breaking
 * boot: every failure is returned, never thrown.
 */
export async function probe(server: CvmServerRef | null): Promise<Diagnosis> {
  const { domain, methods } = cvmAvailability();
  if (!domain) return { ok: false, message: 'cvm domain not injected' };
  if (methods.length === 0) return { ok: false, message: 'cvm domain present but has no methods' };
  if (!server?.pubkey) return { ok: false, message: 'no server selected' };
  const caps = await smsCapabilities(server);
  return caps.ok
    ? { ok: true, message: 'server answered sms.capabilities' }
    : { ok: false, message: caps.error };
}
