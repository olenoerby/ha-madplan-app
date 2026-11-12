import type { Locale } from "./i18n";

const tag = (locale: Locale) => (locale === "en" ? "en-GB" : "da-DK");

/** Whole kroner are shown without decimals, since that is how people say and read prices. */
export function kr(value: number | null | undefined, locale: Locale): string {
  if (value == null) return "–";
  const hasDecimals = Math.abs(value - Math.round(value)) >= 0.005;
  const text = new Intl.NumberFormat(tag(locale), {
    minimumFractionDigits: hasDecimals ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(value);
  return `${text} kr`;
}

export function number(value: number, locale: Locale, digits = 0): string {
  return new Intl.NumberFormat(tag(locale), { maximumFractionDigits: digits }).format(value);
}

/** ISO dates are local calendar days. Noon keeps a time zone from moving them across midnight. */
export function parseDay(iso: string): Date {
  return new Date(`${iso.slice(0, 10)}T12:00:00`);
}

export function dayLong(iso: string, locale: Locale): string {
  const text = new Intl.DateTimeFormat(tag(locale), { weekday: "long", day: "numeric", month: "long" }).format(parseDay(iso));
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function dayShort(iso: string, locale: Locale): string {
  return new Intl.DateTimeFormat(tag(locale), { day: "numeric", month: "short" }).format(parseDay(iso));
}

export function monthShort(yearMonth: string, locale: Locale): string {
  return new Intl.DateTimeFormat(tag(locale), { month: "short" }).format(parseDay(`${yearMonth}-15`));
}

export function monthLong(yearMonth: string, locale: Locale): string {
  return new Intl.DateTimeFormat(tag(locale), { month: "long", year: "numeric" }).format(parseDay(`${yearMonth}-15`));
}

export function dateTime(iso: string, locale: Locale): string {
  return new Intl.DateTimeFormat(tag(locale), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
}

export function todayIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export function quantity(value: number | null, unit: string | null, locale: Locale): string {
  if (value == null) return unit ?? "";
  if (unit === "g" && value >= 1000) return `${number(value / 1000, locale, 2)} kg`;
  if (unit === "ml" && value >= 1000) return `${number(value / 1000, locale, 2)} l`;
  return `${number(value, locale, 1)} ${unit ?? ""}`.trim();
}
