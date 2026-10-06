# S5c nosms CVM E2E evidence

## Delivered

- `napplet/e2e/nosms-cvm.spec.ts`: one coherent Playwright journey through a real Kehto/Paja shell, live ContextVM relays, direct npub/hex resolution, public discovery, live capabilities/pricing/docs, and a real paid-send attempt.
- The paid leg is intentionally honest: this run reached the real server and reported `Send reported not accepted` rather than inventing an SMS delivery. The service transport in this run is `email_gateway`; no handset delivery or delivery receipt is claimed.
- `docs/e2e/s5c-nosms/nosms-cvm-happy-path.webm`: 13.76-second Playwright recording, 1,333,675 bytes.
- Five stills under the same directory document boot, server contract, live pricing, fetched docs, and send outcome.

## Verification

- `pnpm verify`: PASS (21 unit tests, TypeScript check, Vite build).
- `pnpm exec playwright test --config playwright.config.ts`: PASS (1/1, 24.3 seconds).
- Video was probed with ffprobe and has a non-zero 13.76-second duration.

## Remaining operator-level limitation

A real Cashu/CEP-8 paid settlement and JMP SMS rail are not available in this environment. The green test records and asserts the real refusal honestly; it does not claim that `(810) 294-4652` received a message.

## Untracked scratch outputs

The working tree still contains prior diagnostic scripts and Playwright report/test-result directories that were not included in the commit. They are intentionally excluded from the deliverable commit.
