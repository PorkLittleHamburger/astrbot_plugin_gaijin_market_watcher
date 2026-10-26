import test from "node:test";
import assert from "node:assert/strict";
import {
  MAX_BOOK_LEVELS,
  MAX_BOOK_QUANTITY,
  findBookLevelIndex,
  priceToRaw,
  sellerNet,
} from "../views/trade/book-model.mjs";

test("price conversion uses exact integer raw units", () => {
  assert.equal(priceToRaw("199.40"), 1_994_000);
  assert.equal(priceToRaw("8"), 80_000);
  assert.equal(priceToRaw("8.00"), 80_000);
  assert.equal(priceToRaw("8.001"), null);
  assert.equal(priceToRaw("-1"), null);
});

test("seller net matches backend rounding for all ordinary inputs", () => {
  assert.equal(sellerNet("199.40", 7).toFixed(2), "1186.43");
  assert.equal(sellerNet("8.00", 1).toFixed(2), "6.80");
  assert.equal(sellerNet("0.10", 3).toFixed(2), "0.25");
  assert.equal(sellerNet("8.00", 1.9), 0);
});

test("book level identity is based on price_raw rather than array position", () => {
  const levels = [{ price_raw: 90_000 }, { price_raw: 88_000 }];
  assert.equal(findBookLevelIndex(levels, 88_000), 1);
  assert.equal(findBookLevelIndex(levels, 87_000), -1);
  assert.equal(MAX_BOOK_LEVELS, 100);
  assert.equal(MAX_BOOK_QUANTITY, 1_000_000);
});
