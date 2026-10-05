import { defineConfig } from 'vite';
import { nip5aManifest } from '@napplet/vite-plugin';

// A deployment (or an E2E run) can pin the server it is meant to reach, so the
// napplet resolves that npub DIRECTLY instead of taking whatever discovery
// returns first. Unset, the placeholder below means "resolve by discovery only".
const pinnedPubkey = process.env.VITE_NOSMS_CVM_PUBKEY ?? '';
const pinnedRelays = process.env.VITE_NOSMS_CVM_RELAYS ?? '';

export default defineConfig({
  // Vite's default dev CORS allowlist rejects the sandboxed napplet's opaque
  // `Origin: null`; Paja (dev + conformance) loads the entry module in CORS mode.
  server: { cors: { origin: '*' } },
  preview: { cors: { origin: '*' } },
  define: {
    __NOSMS_CVM_PUBKEY__: JSON.stringify(pinnedPubkey),
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
