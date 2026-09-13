/**
 * The shop's service menu, transcribed from src/ctrl_a_jr/pricing.py.
 *
 * This is NOT run data and is never presented as any particular customer's
 * quote. It is the code-owned price list — the thing that makes "the model
 * never picks the number" a checkable statement rather than a slogan. The
 * Quote tab shows it as a reference panel, labelled with its source file, so a
 * reader can see WHAT the price would have been derived from without the page
 * inventing a price for a run whose arguments were never logged.
 *
 * Money is integer cents, and size multipliers are integer percents applied
 * with floor division, exactly as pricing.quote() does — a transcription that
 * rounded differently would be a lie about the code it claims to mirror.
 */

export const CURRENCY = "usd";

/** Above this the shop wants a human to see the job in Slack first. */
export const SLACK_THRESHOLD_CENTS = 50_000;

export const SERVICES = [
  { key: "interior_detail", label: "Interior detail", baseCents: 14_900 },
  { key: "exterior_detail", label: "Exterior detail", baseCents: 12_900 },
  { key: "full_detail", label: "Full detail (interior + exterior)", baseCents: 24_900 },
  { key: "ceramic_coating", label: "Ceramic coating (9H, 2 year)", baseCents: 69_900 },
] as const;

export const SIZES = [
  { key: "sedan", label: "Sedan / coupe", percent: 100 },
  { key: "suv", label: "SUV / crossover", percent: 125 },
  { key: "truck", label: "Truck / van", percent: 140 },
] as const;

export const ADDONS = [
  { key: "pet_hair", label: "Pet hair removal", cents: 7_500 },
  { key: "ozone", label: "Ozone odour treatment", cents: 4_500 },
  { key: "engine_bay", label: "Engine bay cleaning", cents: 6_000 },
] as const;

export function dollars(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** `pricing.quote`'s adjusted base: integer percent, floor division. */
export function adjustedBaseCents(baseCents: number, percent: number): number {
  return Math.floor((baseCents * percent) / 100);
}
