import { defineConfig } from 'vite';
import { nip5aManifest } from '@napplet/vite-plugin';
import { nip19 } from 'nostr-tools';

// A deployment (or an E2E run) can pin the server it is meant to reach, so the
// napplet resolves that npub DIRECTLY instead of taking whatever discovery
// returns first. Unset, the placeholder below means "resolve by discovery only".
//
// The address a human writes down is an npub, but NAP-CVM's `cvm.callTool` takes
// a HEX pubkey (measured 2026-10-05: passing the npub makes the shell fail with
// "Input string must contain hex characters in even length"). So accept either
// form here and hand the napplet both: the hex it must call, and the npub it
// should show.
const pinned = (process.env.VITE_NOSMS_CVM_PUBKEY ?? '').trim();
const pinnedRelays = (process.env.VITE_NOSMS_CVM_RELAYS ?? '').trim();

function toHex(value: string): string {
  if (!value) return '';
  if (/^[0-9a-f]{64}$/i.test(value)) return value.toLowerCase();
  if (value.startsWith('npub1')) return nip19.decode(value).data as string;
  throw new Error(`VITE_NOSMS_CVM_PUBKEY must be an npub1… or 64-char hex, got ${value.slice(0, 16)}…`);
}

const pinnedHex = toHex(pinned);

export default defineConfig({
  // Vite's default dev CORS allowlist rejects the sandboxed napplet's opaque
  // `Origin: null`; Paja (dev + conformance) loads the entry module in CORS mode.
  server: { cors: { origin: '*' } },
  preview: { cors: { origin: '*' } },
  define: {
    // hex: what the shell's cvm.callTool accepts. npub: what a human shows.
    __NOSMS_CVM_PUBKEY__: JSON.stringify(pinnedHex),
    __NOSMS_CVM_NPUB__: JSON.stringify(pinned ? nip19.npubEncode(pinnedHex) : ''),
    __NOSMS_CVM_RELAYS__: JSON.stringify(pinnedRelays),
  },
  build: {
    // Vite's module-preload polyfill calls `fetch`; one inlined entry needs no
    // preload graph, and NIP-5D napplet code has no ambient network authority.
    modulePreload: { polyfill: false },
  },
  plugins: [
    // Produce one self-contained `/index.html` for NIP-5D `srcdoc` loading,
    // then content-address it with the NIP-5A tag/hash schema.
    nip5aManifest({
      nappletType: 'nosms-sender',
      // The napplet asks the SHELL to call the CVM server's MCP tools. It never
      // talks to relays, never holds a key and never holds an ecash token, so
      // `cvm` is the only hard requirement; everything else is optional.
      requires: ['cvm'],
      artifactMode: 'single-file',
    }),
  ],
});
