/* Ambient module declarations for build-time inlined assets.
 *
 * NIP-5D napplets have no ambient network authority, so every external document
 * is bundled at build time (`?raw`) rather than fetched at runtime. */
declare module '*.txt?raw' {
  const content: string;
  export default content;
}

declare module '*.md?raw' {
  const content: string;
  export default content;
}
