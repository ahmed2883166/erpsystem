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
