const PRICE_PATTERN = /^\d+(?:\.\d{1,2})?$/;
const PRICE_CENTS_PER_GJN = 100n;
const PRICE_RAW_PER_CENT = 100n;
const USER_FEE_PERCENT = 85n;

export const MAX_BOOK_LEVELS = 100;
export const MAX_BOOK_QUANTITY = 1_000_000;

function priceToCents(value) {
  const text = String(value ?? "").trim();
  if (!PRICE_PATTERN.test(text)) return null;
  const [whole, fraction = ""] = text.split(".");
  return BigInt(whole) * PRICE_CENTS_PER_GJN + BigInt((fraction + "00").slice(0, 2));
}

export function priceToRaw(value) {
  const cents = priceToCents(value);
  if (cents === null) return null;
  const raw = cents * PRICE_RAW_PER_CENT;
  return raw <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(raw) : null;
}

export function sellerNet(price, quantity = 1) {
  const cents = priceToCents(price);
  const amount = Number(quantity);
  if (cents === null || !Number.isSafeInteger(amount) || amount <= 0) return 0;
  const unitNetCents = (cents * USER_FEE_PERCENT) / 100n;
  const netCents = unitNetCents * BigInt(amount);
  return Number(netCents) / 100;
}

export function findBookLevelIndex(levels, priceRaw) {
  const target = Number(priceRaw);
  if (!Number.isFinite(target) || !Array.isArray(levels)) return -1;
  return levels.findIndex((level) => Number(level?.price_raw) === target);
}
