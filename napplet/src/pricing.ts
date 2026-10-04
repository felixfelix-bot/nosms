/**
 * Destination pricing.
 *
 * The price table is generated from the Python service's own table
 * (`app/pricing.py` -> `scripts/export_contract.py` -> `./contract/pricing.json`),
 * but the LIVE price is always read from the server via `sms.pricing`. This
 * module exists so the UI can show a price before a send and so the displayed
 * number is at least the same table the service meters with — never a
 * hardcoded literal.
 */
import table from './contract/pricing.json';

export type PricingTable = {
  unit: string;
  prefixes: Record<string, number>;
  default: number;
  min_e164_len: number;
};

export const PRICING: PricingTable = table as PricingTable;

/** Human label for the prefix that matched, e.g. "US/CA" or "international". */
const DOMESTIC_LABEL = 'US/CA';

export class InvalidDestination extends Error {
  readonly reason = 'bad_destination';
  constructor(message = 'Destination must be an E.164 phone number, e.g. +141****0100.') {
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

/** Sat price for one SMS to `dest`. Never returns 0 (unknown is never free). */
export function priceFor(dest: string): number {
  const number = normalizeE164(dest);
  if (number.length < PRICING.min_e164_len) return PRICING.default;
  for (const prefix of Object.keys(PRICING.prefixes)) {
    if (number.startsWith(prefix)) return PRICING.prefixes[prefix];
  }
  return PRICING.default;
}

/** True when the matched prefix is the domestic (US/CA) one. */
export function isDomestic(dest: string): boolean {
  try {
    const number = normalizeE164(dest);
    if (number.length < PRICING.min_e164_len) return false;
    return number.startsWith('+1');
  } catch {
    return false;
  }
}

export function zoneLabel(dest: string): string {
  return isDomestic(dest) ? DOMESTIC_LABEL : 'international';
}

/** Render a sat amount as "100 sats". */
export function formatSats(sats: number): string {
  return `${sats} sats`;
}
