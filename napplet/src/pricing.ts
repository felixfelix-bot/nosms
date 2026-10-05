/**
 * Destination pricing.
 *
 * ADR-0002: ONE flat, risk-premium price per SMS. The v1 rail is a single
 * JMP/Cheogram line whose plan is unlimited including international, so the
 * destination no longer maps to cost and there is no per-prefix table — the
 * previous "domestic 100 / international 500" labels advertise a distinction
 * the rail does not have.
 *
 * The number is generated from the Python service's own source of truth
 * (`app/pricing.py` -> `scripts/export_contract.py` -> `./contract/pricing.json`),
 * and the LIVE price is always read from the server via `sms.pricing`. This
 * module exists so the UI can show a price before a send and so the displayed
 * number is the same number the service meters with — never a hardcoded literal.
 */
import table from './contract/pricing.json';

export type PricingTable = {
  unit: string;
  model: string;
  default: number;
  min_e164_len: number;
  formula: string;
  risk_multiplier: number;
  rail_replacement_usd: number;
};

export const PRICING: PricingTable = table as PricingTable;

export class InvalidDestination extends Error {
  readonly reason = 'bad_destination';
  // An EXAMPLE, not a number: the 555-01xx range is reserved for fictional use.
  constructor(message = 'Destination must be an E.164 phone number, e.g. +1 415 555 0100.') {
    super(message);
    this.name = 'InvalidDestination';
  }
}

/** Strip formatting; return '+<digits>'. Throws when there are no digits. */
export function normalizeE164(dest: string): string {
  const digits = (dest ?? '').replace(/\D/g, '');
  if (!digits) throw new InvalidDestination();
  return `+${digits}`;
}

/**
 * Sat price for one SMS. ADR-0002: flat — the destination does NOT change the
 * price, but it is still validated (a string with no digits is a caller bug).
 * Never returns 0 (unknown is never free).
 */
export function priceFor(dest: string): number {
  normalizeE164(dest);
  return PRICING.default;
}

/** The rail's plan is unlimited including international, so there is one zone. */
export function zoneLabel(_dest: string): string {
  return 'flat (worldwide)';
}

/** Render a sat amount as "2900 sats". */
export function formatSats(sats: number): string {
  return `${sats} sats`;
}
