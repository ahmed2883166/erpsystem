/**
 * تنسيق القيم المالية للعرض فقط.
 * لا يغير دقة التخزين أو الحسابات المحاسبية؛ الكميات وأسعار الصرف لها تنسيقات مستقلة.
 */
export const formatMoney = (value?: string | number | null): string => {
  if (value === undefined || value === null || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  return amount.toLocaleString("ar-EG", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
};
