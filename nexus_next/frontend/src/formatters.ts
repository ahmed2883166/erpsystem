/**
 * تنسيق القيم المالية للعرض فقط.
 * التقريب هنا لا يغيّر قيمة المستند أو القيود أو قاعدة البيانات.
 * نعالج القيم النصية العشرية مباشرة لتجنب أخطاء IEEE-754 عند حدود التقريب.
 */
export const formatMoney = (value?: string | number | null): string => {
  if (value === undefined || value === null || value === "") return "—";

  // API decimal values are strings; retain their exact decimal digits until display rounding.
  const raw = String(value).trim();
  const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(raw);
  if (!match) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return "—";
    return amount.toLocaleString("ar-EG", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  const negative = match[1] === "-";
  const whole = BigInt(match[2]);
  const fraction = match[3] || "";
  const centsText = (fraction + "00").slice(0, 2);
  let cents = BigInt(centsText);
  const thirdDigit = Number(fraction[2] || "0");
  let roundedWhole = whole;

  // Display-only ROUND_HALF_UP to two decimal places.
  if (thirdDigit >= 5) {
    cents += 1n;
    if (cents === 100n) {
      cents = 0n;
      roundedWhole += 1n;
    }
  }

  const sign = negative && (roundedWhole !== 0n || cents !== 0n) ? "-" : "";
  const localizedWhole = roundedWhole.toLocaleString("ar-EG");
  const localizedFraction = cents.toString().padStart(2, "0")
    .replace(/\d/g, (digit) => "٠١٢٣٤٥٦٧٨٩"[Number(digit)]);
  const localizedSign = sign ? "−" : "";
  return localizedSign + localizedWhole + "٫" + localizedFraction;
};

/**
 * Exact decimal multiplication for form previews and API amounts.
 * Returns a decimal string rounded HALF_UP to the requested scale; it does not
 * mutate source values or apply currency-specific accounting policy.
 */
export const multiplyDecimalToFixed = (
  left: string | number,
  right: string | number,
  decimalPlaces = 4,
): string => {
  const parse = (value: string | number) => {
    const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(String(value).trim());
    if (!match) return null;
    return {
      negative: match[1] === "-",
      digits: BigInt(match[2] + (match[3] || "")),
      scale: (match[3] || "").length,
    };
  };

  const a = parse(left);
  const b = parse(right);
  if (!a || !b || !Number.isInteger(decimalPlaces) || decimalPlaces < 0 || decimalPlaces > 12) {
    const fallback = Number(left) * Number(right);
    return Number.isFinite(fallback) ? fallback.toFixed(Math.max(0, Math.min(decimalPlaces, 12))) : "0";
  }

  const negative = a.negative !== b.negative;
  const product = a.digits * b.digits;
  const sourceScale = a.scale + b.scale;
  const targetScale = 10n ** BigInt(decimalPlaces);
  let rounded: bigint;

  if (sourceScale > decimalPlaces) {
    const divisor = 10n ** BigInt(sourceScale - decimalPlaces);
    rounded = product / divisor;
    const remainder = product % divisor;
    if (remainder * 2n >= divisor) rounded += 1n;
  } else {
    rounded = product * targetScale / (10n ** BigInt(sourceScale));
  }

  const whole = rounded / targetScale;
  const fraction = (rounded % targetScale).toString().padStart(decimalPlaces, "0");
  const sign = negative && rounded !== 0n ? "-" : "";
  return decimalPlaces === 0
    ? sign + whole.toString()
    : sign + whole.toString() + "." + fraction;
};
