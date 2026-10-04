import { defineConfig } from 'vite';
import { nip5aManifest } from '@napplet/vite-plugin';

export default defineConfig({
  // Vite's default dev CORS allowlist rejects the sandboxed napplet's opaque
  // `Origin: null`; Paja (dev + conformance) loads the entry module in CORS mode.
  server: { cors: { origin: '*' } },
  preview: { cors: { origin: '*' } },
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
