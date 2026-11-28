/*
 * إدارة المخزون: الأصناف والتصنيفات والوحدات والمخازن والمواقع وحركات ورصيد المخزون.
 * تشمل كارت الصنف ونقاط إعادة الطلب والتقارير ومطابقة قيمة المخزون محاسبيًا.
 */
import { useEffect, useMemo, useState } from "react";
import { formatMoney, sumDecimalToFixed } from "./formatters";
import StockTransfersPanel from "./StockTransfersPanel";
import CustomExcelExportButton from "./CustomExcelExportButton";
import InventoryNRVPanel from "./InventoryNRVPanel";
import { accountRoleApi, api, inventoryApi, stockApi, type Account, type AccountRole, type Branch, type Company, type InventoryCategory, type InventoryLocation, type InventoryUnit, type Product, type InventoryReconciliation, type InventoryValuationIntegrity, type InventoryCostAging, type InventoryCount, type InventoryStockAging, type StockBalance, type StockCard, type StockMovement, type Warehouse, type WarehouseProductSetting, type InventoryReorderSuggestions } from "./api";

type Tab = "products" | "categories" | "warehouses" | "units" | "locations" | "movements" | "transfers" | "counts" | "balances" | "reports" | "reorder" | "stock-card" | "accounting" | "nrv";
// شاشة المخزون الرئيسية وتبديل التبويبات بين الأصناف والمخازن والحركات والتقارير.
export default function InventoryPanel({ companyId, currency = "", activityAssignments, primaryActivity }: { companyId: number | null; currency?: string; activityAssignments?: NonNullable<Company["activity_assignments"]>; primaryActivity?: Company["activity"] }) {
  const activeActivityAssignments = (activityAssignments || []).filter((item) => item.is_active);
  const [tab, setTab] = useState<Tab>("products");
  const [products, setProducts] = useState<Product[]>([]);
  const [categories, setCategories] = useState<InventoryCategory[]>([]);
  const [units, setUnits] = useState<InventoryUnit[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [reconciliationBranch, setReconciliationBranch] = useState("");
  const [warehouseSettings, setWarehouseSettings] = useState<WarehouseProductSetting[]>([]);
  const [reorderSuggestions, setReorderSuggestions] = useState<InventoryReorderSuggestions | null>(null);
  const [stockCard, setStockCard] = useState<StockCard | null>(null);
  const [stockCardError, setStockCardError] = useState("");
  const [stockCardBusy, setStockCardBusy] = useState(false);
  const [stockCardForm, setStockCardForm] = useState({ product: "", warehouse: "", date_from: "", date_to: "" });
  const [reorderForm, setReorderForm] = useState({ product: "", warehouse: "", min_stock: "0" });
  const [locations, setLocations] = useState<InventoryLocation[]>([]);
  const [movements, setMovements] = useState<StockMovement[]>([]);
  const [movementStatusFilter, setMovementStatusFilter] = useState<"all" | "draft" | "posted">("all");
  const [stockAgeFilter, setStockAgeFilter] = useState<"all" | "active" | "slow" | "very_slow" | "stagnant" | "unknown">("all");
  const [balances, setBalances] = useState<StockBalance[]>([]);
  const [reconciliation, setReconciliation] = useState<InventoryReconciliation | null>(null);
  const [reconciliationError, setReconciliationError] = useState("");
  const [reconciling, setReconciling] = useState(false);
  const [valuationIntegrity, setValuationIntegrity] = useState<InventoryValuationIntegrity | null>(null);
  const [valuationIntegrityError, setValuationIntegrityError] = useState("");
  const [valuationIntegrityBusy, setValuationIntegrityBusy] = useState(false);
  const [costAging, setCostAging] = useState<InventoryCostAging | null>(null);
  const [costAgingError, setCostAgingError] = useState("");
  const [costAgingBusy, setCostAgingBusy] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [roleAssignments, setRoleAssignments] = useState<AccountRole[]>([]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ code: "", name_ar: "", barcode: "", unit: "", category: "", product_type: "goods", stock_class: "finished_goods", purchase_price: "0", sale_price: "0", unit_weight: "0", unit_volume: "0", min_stock: "0", costing_method: "weighted_average", track_expiry: false });
  const [unitForm, setUnitForm] = useState({ code: "", name_ar: "", symbol: "", decimal_places: "2" });
  const [categoryForm, setCategoryForm] = useState({ code: "", name_ar: "" });
  const [warehouseForm, setWarehouseForm] = useState({ code: "", name_ar: "", branch: "", address: "" });
  const [locationForm, setLocationForm] = useState({ warehouse: "", code: "", name_ar: "", location_type: "area", parent: "" });
  const [roleForm, setRoleForm] = useState({ role: "inventory", account: "", activity: "" });

  useEffect(() => {
    const preferred = activityAssignments?.find((item) => item.is_active && item.activity === primaryActivity)
      || activityAssignments?.find((item) => item.is_active);
    setRoleForm((old) => ({ ...old, activity: preferred ? String(preferred.id) : "" }));
  }, [companyId, primaryActivity, activityAssignments]);
  const [stockForm, setStockForm] = useState({ movement_number: "", movement_type: "receipt", adjustment_reason: "other", movement_date: new Date().toISOString().slice(0, 10), source_warehouse: "", destination_warehouse: "", source_location: "", destination_location: "", expiry_date: "", product: "", quantity: "1", unit_cost: "0", adjustment_direction: "in", notes: "" });
  const [stockLines, setStockLines] = useState([{ product: "", quantity: "1", unit_cost: "0", expiry_date: "" }]);
  const [counts, setCounts] = useState<InventoryCount[]>([]);
  const [stockAging, setStockAging] = useState<InventoryStockAging | null>(null);
  const [selectedCountId, setSelectedCountId] = useState<number | null>(null);
  const [countForm, setCountForm] = useState({ warehouse: "", count_number: "", count_date: new Date().toISOString().slice(0, 10), notes: "" });
  const [countLineDraft, setCountLineDraft] = useState({ product: "", location: "", expiry_date: "", counted_quantity: "", adjustment_unit_cost: "0", notes: "" });


  // تحديث قوائم الأصناف والمخازن والوحدات والأرصدة وفق الشركة الحالية.
const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [p, c, u, w, l, m, b, a, ar, ws, br, ct, sa, rs] = await Promise.all([inventoryApi.products(companyId), inventoryApi.categories(companyId), inventoryApi.units(companyId), inventoryApi.warehouses(companyId), inventoryApi.locations(companyId), stockApi.movements(companyId), stockApi.balances(companyId), api.accounts(companyId), accountRoleApi.list(companyId), inventoryApi.warehouseProductSettings(companyId), api.branches(companyId), stockApi.counts(companyId), stockApi.stockAging(companyId), stockApi.reorderSuggestions(companyId)]);
      setProducts(p); setCategories(c); setUnits(u); setWarehouses(w); setLocations(l); setMovements(m); setBalances(b); setAccounts(a); setRoleAssignments(ar); setWarehouseSettings(ws); setBranches(br); setCounts(ct); setStockAging(sa); setReorderSuggestions(rs);
      setSelectedCountId((old) => old && ct.some((item) => item.id === old) ? old : (ct[0]?.id ?? null));
      setReconciliationBranch((old) => old && br.some((item) => String(item.id) === old) ? old : "");
      setRoleForm((old) => ({ ...old, account: String(ar.find((item) => item.role === old.role)?.account || "") }));
      setLocationForm((old) => ({ ...old, warehouse: old.warehouse || String(w.find((item) => item.is_active)?.id || "") }));
      setReorderForm((old) => ({ ...old, warehouse: old.warehouse || String(w.find((item) => item.is_active)?.id || ""), product: old.product || String(p.find((item) => item.product_type === "goods" && item.is_active)?.id || "") }));
      setStockCardForm((old) => ({ ...old, warehouse: old.warehouse || String(w.find((item) => item.is_active)?.id || ""), product: old.product || String(p.find((item) => item.product_type === "goods" && item.is_active)?.id || "") }));
      const defaultProduct = String(p.find((item) => item.product_type === "goods" && item.is_active)?.id || "");
      setStockForm((old) => ({ ...old, source_warehouse: old.source_warehouse || String(w.find((item) => item.is_active)?.id || ""), destination_warehouse: old.destination_warehouse || String(w.find((item) => item.is_active)?.id || "") }));
      setStockLines((old) => old.map((line, index) => ({ ...line, product: line.product || (index === 0 ? defaultProduct : "") })));
      setForm((old) => ({ ...old, unit: old.unit || String(u[0]?.id || ""), category: old.category || String(c[0]?.id || "") }));
      setCountForm((old) => ({ ...old, warehouse: old.warehouse || String(w.find((item) => item.is_active)?.id || "") }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المخزون."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);
  // تصدير بطاقة حركة الصنف إلى CSV متوافق مع Excel مع حماية الصيغ.
  const exportStockCard = () => {
    if (!stockCard) return;
    const headers = ["التاريخ", "رقم الحركة", "نوع الحركة", "وارد كمية", "منصرف كمية", "تكلفة الوحدة", "وارد قيمة", "منصرف قيمة", "الرصيد الكمي", "قيمة الرصيد"];
    const rows = stockCard.rows.map((row) => [row.date, row.movement_number, row.movement_type, row.quantity_in, row.quantity_out, row.unit_cost, row.value_in, row.value_out, row.balance_quantity, row.balance_value]);
    const safe = (value: string | number) => {
      const text = String(value ?? "");
      const protectedText = /^[=+@-]/.test(text) || text.startsWith("\t") || text.startsWith("\r") ? "'" + text : text;
      return '"' + protectedText.replace(/"/g, '""') + '"';
    };
    const csv = "\uFEFF" + [headers, ...rows].map((row) => row.map(safe).join(";")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `stock-card-${stockCard.product_code}-${stockCard.warehouse}.csv`;
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  };
  // تحميل كارت الصنف وحركاته التفصيلية للفلاتر المحددة.
const loadStockCard = async (page = 1) => {
    if (!stockCardForm.product || !stockCardForm.warehouse) { setStockCardError("اختر الصنف والمخزن أولاً."); return; }
    setStockCardBusy(true); setStockCardError("");
    try { setStockCard(await stockApi.stockCard(Number(stockCardForm.product), Number(stockCardForm.warehouse), stockCardForm.date_from, stockCardForm.date_to, page, 50)); }
    catch (reason) { setStockCard(null); setStockCardError(reason instanceof Error ? reason.message : "تعذر تحميل بطاقة الصنف."); }
    finally { setStockCardBusy(false); }
  };
  // تحميل تقرير مطابقة أرصدة المخزون مع أرصدة الحسابات.
const loadReconciliation = async () => {
    if (!companyId) return;
    setReconciling(true); setReconciliationError("");
    try { setReconciliation(await stockApi.reconciliation(companyId, reconciliationBranch ? Number(reconciliationBranch) : undefined)); }
    catch (reason) { setReconciliation(null); setReconciliationError(reason instanceof Error ? reason.message : "تعذرت مطابقة المخزون مع دفتر الأستاذ."); }
    finally { setReconciling(false); }
  };
  // تحميل فحص سلامة تقييم المخزون واكتشاف الفروق أو الحركات المعلقة.
const loadValuationIntegrity = async () => {
    if (!companyId) return;
    setValuationIntegrityBusy(true); setValuationIntegrityError("");
    try { setValuationIntegrity(await stockApi.valuationIntegrity(companyId, reconciliationBranch ? Number(reconciliationBranch) : undefined)); }
    catch (reason) { setValuationIntegrity(null); setValuationIntegrityError(reason instanceof Error ? reason.message : "تعذر فحص سلامة تقييم المخزون."); }
    finally { setValuationIntegrityBusy(false); }
  };
  // تحميل تقرير أعمار طبقات FIFO من تاريخ الاستلام الأصلي، مع احترام نطاق الفرع.
  const loadCostAging = async () => {
    if (!companyId) return;
    setCostAgingBusy(true); setCostAgingError("");
    try { setCostAging(await stockApi.costAging(companyId, reconciliationBranch ? Number(reconciliationBranch) : undefined)); }
    catch (reason) { setCostAging(null); setCostAgingError(reason instanceof Error ? reason.message : "تعذر تحميل تقرير أعمار طبقات التكلفة."); }
    finally { setCostAgingBusy(false); }
  };
  const filteredProducts = useMemo(() => products.filter((p) => `${p.code} ${p.barcode} ${p.name_ar}`.toLowerCase().includes(search.toLowerCase())), [products, search]);
  const inventoryIndexes = useMemo(() => {
    const byWarehouse = new Map<number, { quantity: number; value: string; products: Set<number> }>();
    const byWarehouseProduct = new Map<string, { quantity: number; value: string }>();
    for (const balance of balances) {
      const warehouse = byWarehouse.get(balance.warehouse) ?? { quantity: 0, value: "0", products: new Set<number>() };
      warehouse.quantity += Number(balance.quantity);
      warehouse.value = sumDecimalToFixed([warehouse.value, balance.inventory_value], 4);
      warehouse.products.add(balance.product);
      byWarehouse.set(balance.warehouse, warehouse);
      const key = `${balance.warehouse}:${balance.product}`;
      const productBalance = byWarehouseProduct.get(key) ?? { quantity: 0, value: "0" };
      productBalance.quantity += Number(balance.quantity);
      productBalance.value = sumDecimalToFixed([productBalance.value, balance.inventory_value], 4);
      byWarehouseProduct.set(key, productBalance);
    }
    return { byWarehouse, byWarehouseProduct };
  }, [balances]);
  const warehouseSettingIndex = useMemo(() => new Map(
    warehouseSettings.filter((item) => item.is_active).map((item) => [`${item.warehouse}:${item.product}`, item] as const)
  ), [warehouseSettings]);
  const warehouseValuation = useMemo(() => warehouses.map((warehouse) => {
    const totals = inventoryIndexes.byWarehouse.get(warehouse.id);
    return { warehouse, quantity: totals?.quantity ?? 0, value: totals?.value ?? 0, products: totals?.products.size ?? 0 };
  }).filter((row) => row.products > 0), [warehouses, inventoryIndexes]);
  const reorderAlerts = reorderSuggestions?.rows.filter((row) => row.stock_status === "reorder") ?? [];
  const expiryAlerts = balances.filter((row) => row.expiry_date && Number(row.quantity) > 0).map((row) => {
    const days = Math.ceil((new Date(row.expiry_date + "T00:00:00").getTime() - new Date().setHours(0, 0, 0, 0)) / 86400000);
    return { ...row, days };
  }).filter((row) => row.days <= 30).sort((a, b) => a.days - b.days);
  const stockAgingRows = stockAging?.rows.map((row) => ({
    product: row.product, product_code: row.product_code, product_name: row.product_name,
    warehouse: row.warehouse, warehouse_name: row.warehouse_name,
    quantity: Number(row.quantity), value: row.value, last_movement: row.last_movement,
    ageDays: row.age_days, ageBand: row.age_band,
  })) ?? [];
  const stockAgingCounts = stockAging?.summary ?? {
    active: 0, slow: 0, very_slow: 0, stagnant: 0, unknown: 0,
  };
  const totalInventoryValue = sumDecimalToFixed(balances.map((row) => row.inventory_value), 4);
  const totalInventoryQuantity = balances.reduce((sum, row) => sum + Number(row.quantity), 0);
  // حفظ العملية الحالية في تبويب المخزون ثم إعادة تحميل البيانات المرتبطة.
const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      if (tab === "products") {
        if (!form.unit) throw new Error("أضف وحدة قياس أولاً.");
        await inventoryApi.createProduct({ company: companyId, code: form.code, name_ar: form.name_ar, barcode: form.barcode, unit: Number(form.unit), category: form.category ? Number(form.category) : null, product_type: form.product_type, stock_class: form.product_type === "goods" ? form.stock_class : null, purchase_price: form.purchase_price, sale_price: form.sale_price, unit_weight: form.unit_weight, unit_volume: form.unit_volume, min_stock: form.min_stock, costing_method: form.costing_method, track_expiry: form.track_expiry });
        setForm((old) => ({ ...old, code: "", name_ar: "", barcode: "" }));
      } else if (tab === "categories") {
        await inventoryApi.createCategory({ company: companyId, ...categoryForm }); setCategoryForm({ code: "", name_ar: "" });
      } else if (tab === "units") {
        await inventoryApi.createUnit({ company: companyId, ...unitForm, decimal_places: Number(unitForm.decimal_places) }); setUnitForm({ code: "", name_ar: "", symbol: "", decimal_places: "2" });
      } else if (tab === "warehouses") {
        await inventoryApi.createWarehouse({ company: companyId, ...warehouseForm, branch: warehouseForm.branch ? Number(warehouseForm.branch) : null }); setWarehouseForm({ code: "", name_ar: "", branch: "", address: "" });
      } else if (tab === "reorder") {
        if (!reorderForm.product || !reorderForm.warehouse) throw new Error("اختر الصنف والمخزن.");
        const existing = warehouseSettings.find((item) => item.product === Number(reorderForm.product) && item.warehouse === Number(reorderForm.warehouse));
        const payload = { product: Number(reorderForm.product), warehouse: Number(reorderForm.warehouse), min_stock: reorderForm.min_stock, is_active: true };
        if (existing) await inventoryApi.updateWarehouseProductSetting(existing.id, payload);
        else await inventoryApi.createWarehouseProductSetting(payload);
      } else if (tab === "locations") {
        if (!locationForm.warehouse) throw new Error("أضف مخزناً أولاً.");
        await inventoryApi.createLocation({ warehouse: Number(locationForm.warehouse), code: locationForm.code, name_ar: locationForm.name_ar, location_type: locationForm.location_type, parent: locationForm.parent ? Number(locationForm.parent) : null });
        setLocationForm((old) => ({ ...old, code: "", name_ar: "", parent: "" }));
      } else if (tab === "accounting") {
        if (!roleForm.account) throw new Error("اختر حساباً تفصيلياً قابلاً للترحيل.");
        const activityId = roleForm.activity ? Number(roleForm.activity) : null;
        const payload = { company: companyId, role: roleForm.role, account: Number(roleForm.account), activity: activityId };
        const existing = roleAssignments.find((item) => item.role === roleForm.role && item.activity === activityId);
        if (existing) await accountRoleApi.update(existing.id, payload);
        else await accountRoleApi.create(payload);
        setRoleForm((old) => ({ ...old, account: "" }));
      } else if (tab === "counts") {
        if (!countForm.warehouse) throw new Error("اختر المخزن لبدء الجرد.");
        const created = await stockApi.createCount({
          company: companyId, warehouse: Number(countForm.warehouse),
          count_number: countForm.count_number, count_date: countForm.count_date, notes: countForm.notes,
        });
        setSelectedCountId(created.id);
        setCountForm((old) => ({ ...old, count_number: "", notes: "" }));
      } else if (tab === "movements") {
        const type = stockForm.movement_type;
        const sourceId = type === "receipt" || (type === "adjustment" && stockForm.adjustment_direction === "in") ? null : Number(stockForm.source_warehouse);
        const destinationId = type === "issue" || (type === "adjustment" && stockForm.adjustment_direction === "out") ? null : Number(stockForm.destination_warehouse);
        if (!stockLines.length || stockLines.some((line) => !line.product || Number(line.quantity) <= 0)) throw new Error("أضف صنفاً واحداً على الأقل وتأكد من اختيار الصنف والكمية الصحيحة لكل سطر.");
        await stockApi.createMovement({ company: companyId, movement_number: stockForm.movement_number, movement_type: type, adjustment_reason: type === "adjustment" ? stockForm.adjustment_reason : "", movement_date: stockForm.movement_date, source_warehouse: sourceId, destination_warehouse: destinationId, notes: stockForm.notes, lines: stockLines.map((line) => ({ product: Number(line.product), quantity: line.quantity, unit_cost: line.unit_cost || "0", source_location: sourceId && stockForm.source_location ? Number(stockForm.source_location) : null, destination_location: destinationId && stockForm.destination_location ? Number(stockForm.destination_location) : null, expiry_date: line.expiry_date || null, adjustment_direction: type === "adjustment" ? stockForm.adjustment_direction : "" })) });
        setStockForm((old) => ({ ...old, movement_number: "", notes: "" }));
        setStockLines([{ product: String(products.find((item) => item.product_type === "goods" && item.is_active)?.id || ""), quantity: "1", unit_cost: "0", expiry_date: "" }]);
      }
      setShowForm(false); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ البيانات."); }
    finally { setBusy(false); }
  };
  const tabs: { id: Tab; label: string; count: number }[] = [
    { id: "products", label: "الأصناف والخدمات", count: products.length },
    { id: "categories", label: "المجموعات", count: categories.length },
    { id: "units", label: "وحدات القياس", count: units.length },
    { id: "warehouses", label: "المخازن", count: warehouses.length },
    { id: "locations", label: "مواقع التخزين", count: locations.length },
    { id: "movements", label: "آخر 100 حركة", count: movements.length },
    { id: "transfers", label: "التحويل بين الفروع", count: 0 },
    { id: "counts", label: "الجرد الفعلي", count: counts.length },
    { id: "balances", label: "أرصدة المخزون", count: balances.length },
    { id: "reports", label: "تقارير المخزون", count: warehouseValuation.length + expiryAlerts.length + reorderAlerts.length },
    { id: "reorder", label: "حدود الطلب لكل مخزن", count: warehouseSettings.filter((item) => item.is_active).length },
    { id: "stock-card", label: "بطاقة حركة الصنف", count: stockCard?.pagination.total_rows || 0 },
    { id: "accounting", label: "ربط الحسابات", count: roleAssignments.length },
    { id: "nrv", label: "تقييم NRV (IAS 2)", count: 0 },
  ];
  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر شركة لعرض بيانات المخزون.</div></section>;
  return <section className="work-panel inventory-panel">
    <div className="work-panel-head"><div><h2>إدارة المخزون والمستودعات</h2><p>الأصناف والمخازن وحركات الاستلام والصرف والتحويل وأرصدة المخزون وربط الحسابات. <strong>القيم المالية بالعملة الأساسية: {currency || "—"}</strong></p></div><div className="table-actions"><CustomExcelExportButton resource="inventory" companyId={companyId} />{tab !== "balances" && tab !== "reports" && tab !== "stock-card" && tab !== "transfers" && tab !== "nrv" && <button className="primary-button" onClick={() => setShowForm((v) => !v)}>{showForm ? "إغلاق النموذج" : "＋ إضافة جديد"}</button>}</div></div>
    <div className="inventory-tabs">{tabs.map((item) => <button key={item.id} className={tab === item.id ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab(item.id); setShowForm(false); setSearch(""); }}>{item.label}<b>{item.count}</b></button>)}</div>
    {error && <div className="error-banner">{error}</div>}
    {tab === "nrv" && <InventoryNRVPanel companyId={companyId} products={products} warehouses={warehouses} />}
    {showForm && <form className="inventory-form" onSubmit={submit}>
      {tab === "products" && <>
        <label>كود الصنف<input required value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></label>
        <label>اسم الصنف<input required value={form.name_ar} onChange={(e) => setForm({ ...form, name_ar: e.target.value })} /></label>
        <label>الباركود<input value={form.barcode} onChange={(e) => setForm({ ...form, barcode: e.target.value })} /></label>
        <label>نوع السجل<select value={form.product_type} onChange={(e) => setForm({ ...form, product_type: e.target.value, stock_class: e.target.value === "goods" ? "finished_goods" : "" })}><option value="goods">صنف مخزني</option><option value="service">خدمة</option></select></label>
        {form.product_type === "goods" && <label>تصنيف الصنف<select required value={form.stock_class} onChange={(e) => setForm({ ...form, stock_class: e.target.value })}><option value="raw_material">مواد خام</option><option value="finished_goods">منتجات تامة</option><option value="operating_supplies">مستلزمات تشغيل</option><option value="spare_parts">قطع غيار</option></select></label>}
        <label>وحدة القياس<select required value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })}><option value="">اختر الوحدة</option>{units.map((u) => <option key={u.id} value={u.id}>{u.name_ar} ({u.code})</option>)}</select></label>
        <label>المجموعة<select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}><option value="">بدون مجموعة</option>{categories.map((c) => <option key={c.id} value={c.id}>{c.name_ar}</option>)}</select></label>
        <label>سعر الشراء<input type="number" min="0" step="0.0001" value={form.purchase_price} onChange={(e) => setForm({ ...form, purchase_price: e.target.value })} /></label>
        <label>سعر البيع<input type="number" min="0" step="0.0001" value={form.sale_price} onChange={(e) => setForm({ ...form, sale_price: e.target.value })} /></label>
        <label>وزن الوحدة (كجم)<input type="number" min="0" step="0.000001" value={form.unit_weight} onChange={(e) => setForm({ ...form, unit_weight: e.target.value })} /></label>
        <label>حجم الوحدة (م³)<input type="number" min="0" step="0.000001" value={form.unit_volume} onChange={(e) => setForm({ ...form, unit_volume: e.target.value })} /></label>
        <label>حد إعادة الطلب<input type="number" min="0" step="0.0001" value={form.min_stock} onChange={(e) => setForm({ ...form, min_stock: e.target.value })} /></label>
        <label>طريقة التقييم<select value={form.costing_method} onChange={(e) => setForm({ ...form, costing_method: e.target.value })}><option value="weighted_average">متوسط مرجح</option><option value="fifo">FIFO</option></select></label>
        <label className="inventory-check"><input type="checkbox" checked={form.track_expiry} disabled={form.product_type === "service"} onChange={(e) => setForm({ ...form, track_expiry: e.target.checked })} /> تتبع الصلاحية</label>
      </>}
      {tab === "categories" && <><label>كود المجموعة<input required value={categoryForm.code} onChange={(e) => setCategoryForm({ ...categoryForm, code: e.target.value })} /></label><label>اسم المجموعة<input required value={categoryForm.name_ar} onChange={(e) => setCategoryForm({ ...categoryForm, name_ar: e.target.value })} /></label></>}
      {tab === "units" && <><label>كود الوحدة<input required value={unitForm.code} onChange={(e) => setUnitForm({ ...unitForm, code: e.target.value })} /></label><label>اسم الوحدة<input required value={unitForm.name_ar} onChange={(e) => setUnitForm({ ...unitForm, name_ar: e.target.value })} /></label><label>الرمز<input value={unitForm.symbol} onChange={(e) => setUnitForm({ ...unitForm, symbol: e.target.value })} /></label><label>الخانات العشرية<input type="number" min="0" max="6" value={unitForm.decimal_places} onChange={(e) => setUnitForm({ ...unitForm, decimal_places: e.target.value })} /></label></>}
      {tab === "reorder" && <>
        <label>الصنف المخزني<select required value={reorderForm.product} onChange={(e) => setReorderForm({ ...reorderForm, product: e.target.value })}><option value="">اختر الصنف</option>{products.filter((item) => item.product_type === "goods" && item.is_active).map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label>
        <label>المخزن<select required value={reorderForm.warehouse} onChange={(e) => setReorderForm({ ...reorderForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.filter((item) => item.is_active).map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label>
        <label>حد إعادة الطلب لهذا المخزن<input required type="number" min="0" step="0.0001" value={reorderForm.min_stock} onChange={(e) => setReorderForm({ ...reorderForm, min_stock: e.target.value })} /></label>
        <p className="inventory-wide">يتم حفظ حد مستقل لكل تركيبة صنف ومخزن. عند عدم وجود إعداد خاص، يظل حد الصنف العام هو القيمة الافتراضية.</p>
      </>}
      {tab === "warehouses" && <><label>كود المخزن<input required value={warehouseForm.code} onChange={(e) => setWarehouseForm({ ...warehouseForm, code: e.target.value })} /></label><label>اسم المخزن<input required value={warehouseForm.name_ar} onChange={(e) => setWarehouseForm({ ...warehouseForm, name_ar: e.target.value })} /></label><label className="inventory-wide">العنوان<textarea value={warehouseForm.address} onChange={(e) => setWarehouseForm({ ...warehouseForm, address: e.target.value })} /></label></>}
      {tab === "locations" && <><label>المخزن<select required value={locationForm.warehouse} onChange={(e) => setLocationForm({ ...locationForm, warehouse: e.target.value, parent: "" })}><option value="">اختر المخزن</option>{warehouses.filter((w) => w.is_active).map((w) => <option key={w.id} value={w.id}>{w.name_ar}</option>)}</select></label><label>نوع الموقع<select value={locationForm.location_type} onChange={(e) => setLocationForm({ ...locationForm, location_type: e.target.value })}><option value="floor">دور</option><option value="area">منطقة</option><option value="rack">رف</option><option value="bin">خانة</option></select></label><label>الكود<input required value={locationForm.code} onChange={(e) => setLocationForm({ ...locationForm, code: e.target.value })} /></label><label>اسم الموقع<input required value={locationForm.name_ar} onChange={(e) => setLocationForm({ ...locationForm, name_ar: e.target.value })} /></label><label>الموقع الأب<select value={locationForm.parent} onChange={(e) => setLocationForm({ ...locationForm, parent: e.target.value })}><option value="">بدون موقع أب</option>{locations.filter((x) => String(x.warehouse) === locationForm.warehouse).map((x) => <option key={x.id} value={x.id}>{x.name_ar} ({x.code})</option>)}</select></label></>}
      {tab === "accounting" && <><label>النشاط<select value={roleForm.activity} onChange={(e) => { const nextActivity = e.target.value; const activityId = nextActivity ? Number(nextActivity) : null; const assigned = roleAssignments.find((item) => item.role === roleForm.role && item.activity === activityId) || (activityId !== null ? roleAssignments.find((item) => item.role === roleForm.role && item.activity === null) : undefined); setRoleForm({ ...roleForm, activity: nextActivity, account: assigned ? String(assigned.account) : "" }); }}><option value="">عام للشركة (افتراضي)</option>{activeActivityAssignments.map((item) => <option key={item.id} value={item.id}>{{commercial:"تجاري",service:"خدمي",medical:"طبي",restaurants:"مطاعم",industrial:"صناعي"}[item.activity]}</option>)}</select></label><label>الدور المحاسبي<select value={roleForm.role} onChange={(e) => { const next = e.target.value; const activityId = roleForm.activity ? Number(roleForm.activity) : null; const assigned = roleAssignments.find((item) => item.role === next && item.activity === activityId) || (activityId !== null ? roleAssignments.find((item) => item.role === next && item.activity === null) : undefined); setRoleForm({ ...roleForm, role: next, account: assigned ? String(assigned.account) : "" }); }}><option value="inventory">حساب المخزون</option><option value="goods_received_not_invoiced">بضاعة مستلمة غير مفوترة</option><option value="accounts_payable">حسابات الموردين</option><option value="accounts_receivable">حسابات العملاء</option><option value="sales">إيرادات المبيعات</option><option value="vat_input">ضريبة القيمة المضافة - مدخلات</option><option value="vat_output">ضريبة القيمة المضافة - مخرجات</option><option value="cash">الصندوق</option><option value="bank">البنوك</option><option value="cogs">تكلفة المبيعات</option><option value="inventory_adjustment_gain">أرباح تسوية المخزون</option><option value="inventory_adjustment_loss">خسائر تسوية المخزون</option><option value="inter_branch_clearing">تسوية بين الفروع</option></select></label><label>الحساب المرتبط<select required value={roleForm.account} onChange={(e) => setRoleForm({ ...roleForm, account: e.target.value })}><option value="">اختر حساباً</option>{accounts.filter((a) => a.is_active && !a.is_group && a.allow_posting).map((a) => <option key={a.id} value={a.id}>{a.code} - {a.name_ar}</option>)}</select></label><p className="inventory-wide">اربط الأدوار المطلوبة قبل الترحيل: الاستلام يحتاج المخزون والبضاعة المستلمة غير المفوترة، وفواتير الموردين تحتاج حساب الموردين وضريبة المدخلات، وفواتير المبيعات تحتاج العملاء وإيرادات المبيعات وضريبة المخرجات، والتحصيل والصرف يحتاج الصندوق أو البنك حسب الطريقة، والصرف يحتاج المخزون وتكلفة المبيعات، وتسويات الجرد تحتاج حسابي الزيادة والعجز، والتحويل بين الفروع يحتاج حساب التسوية.</p></>}
      {tab === "counts" && <>
        <label>رقم مستند الجرد<input required value={countForm.count_number} onChange={(e) => setCountForm({ ...countForm, count_number: e.target.value })} /></label>
        <label>المخزن<select required value={countForm.warehouse} onChange={(e) => setCountForm({ ...countForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.filter((item) => item.is_active).map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label>
        <label>تاريخ الجرد<input type="date" required value={countForm.count_date} onChange={(e) => setCountForm({ ...countForm, count_date: e.target.value })} /></label>
        <label className="inventory-wide">ملاحظات<textarea value={countForm.notes} onChange={(e) => setCountForm({ ...countForm, notes: e.target.value })} /></label>
        <p className="inventory-wide">سيأخذ النظام لقطة من أرصدة المخزن الحالية. لن تُرحّل أي فروق قبل إدخال الكميات الفعلية واعتماد الجرد. إذا حدثت حركة مخزنية بعد بدء العد، سيطلب النظام إعادة الجرد لحماية دقة النتائج.</p>
      </>}
      {tab === "movements" && <>
        <label>رقم الحركة<input required value={stockForm.movement_number} onChange={(e) => setStockForm({ ...stockForm, movement_number: e.target.value })} /></label>
        <label>نوع الحركة<select value={stockForm.movement_type} onChange={(e) => setStockForm({ ...stockForm, movement_type: e.target.value, source_location: "", destination_location: "" })}><option value="receipt">استلام</option><option value="issue">صرف</option><option value="transfer">تحويل بين مخازن</option><option value="adjustment">تسوية جرد</option></select></label>
        <label>التاريخ<input type="date" required value={stockForm.movement_date} onChange={(e) => setStockForm({ ...stockForm, movement_date: e.target.value })} /></label>
        {(stockForm.movement_type === "issue" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "out")) && <label>مخزن المصدر<select required value={stockForm.source_warehouse} onChange={(e) => setStockForm({ ...stockForm, source_warehouse: e.target.value, source_location: "" })}>{warehouses.map((w) => <option key={w.id} value={w.id}>{w.name_ar}</option>)}</select></label>}
        {(stockForm.movement_type === "issue" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "out")) && <label>موقع الصرف<select value={stockForm.source_location} onChange={(e) => setStockForm({ ...stockForm, source_location: e.target.value })}><option value="">الموقع العام للمخزن</option>{locations.filter((x) => x.is_active && String(x.warehouse) === stockForm.source_warehouse).map((x) => <option key={x.id} value={x.id}>{x.name_ar}</option>)}</select></label>}
        {(stockForm.movement_type === "receipt" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "in")) && <label>مخزن الوجهة<select required value={stockForm.destination_warehouse} onChange={(e) => setStockForm({ ...stockForm, destination_warehouse: e.target.value, destination_location: "" })}>{warehouses.map((w) => <option key={w.id} value={w.id}>{w.name_ar}</option>)}</select></label>}
        {(stockForm.movement_type === "receipt" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "in")) && <label>موقع الاستلام<select value={stockForm.destination_location} onChange={(e) => setStockForm({ ...stockForm, destination_location: e.target.value })}><option value="">الموقع العام للمخزن</option>{locations.filter((x) => x.is_active && String(x.warehouse) === stockForm.destination_warehouse).map((x) => <option key={x.id} value={x.id}>{x.name_ar}</option>)}</select></label>}
        {stockForm.movement_type === "adjustment" && <label>اتجاه التسوية<select value={stockForm.adjustment_direction} onChange={(e) => setStockForm({ ...stockForm, adjustment_direction: e.target.value, source_location: "", destination_location: "" })}><option value="in">زيادة جرد</option><option value="out">عجز جرد</option></select></label>}
        {stockForm.movement_type === "adjustment" && <label>سبب التسوية<select required value={stockForm.adjustment_reason} onChange={(e) => setStockForm({ ...stockForm, adjustment_reason: e.target.value })}><option value="count">جرد فعلي</option><option value="damage">تالف</option><option value="expired">منتهي الصلاحية</option><option value="write_off">شطب أو إعدام</option><option value="other">أخرى</option></select></label>}
        <div className="inventory-wide inventory-line-editor">
          <div className="inventory-line-heading"><strong>أصناف الحركة</strong><button type="button" className="add-line-button" onClick={() => setStockLines((rows) => [...rows, { product: "", quantity: "1", unit_cost: "0", expiry_date: "" }])}>＋ إضافة صنف</button></div>
          <div className="data-table-wrap"><table><thead><tr><th>#</th><th>الصنف</th><th>الكمية</th><th>الصلاحية</th>{(stockForm.movement_type === "receipt" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "in")) && <th>تكلفة الوحدة</th>}<th>إجراء</th></tr></thead><tbody>
            {stockLines.map((line, index) => {
              const selectedProduct = products.find((item) => String(item.id) === line.product);
              return <tr key={index}><td>{index + 1}</td>
                <td><select required aria-label={`الصنف ${index + 1}`} value={line.product} onChange={(event) => setStockLines((rows) => rows.map((item, i) => i === index ? { ...item, product: event.target.value, expiry_date: "" } : item))}><option value="">اختر الصنف</option>{products.filter((item) => item.product_type === "goods" && item.is_active).map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></td>
                <td><input required aria-label={`الكمية ${index + 1}`} type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={(event) => setStockLines((rows) => rows.map((item, i) => i === index ? { ...item, quantity: event.target.value } : item))} /></td>
                <td>{selectedProduct?.track_expiry ? <input required aria-label={`تاريخ الصلاحية ${index + 1}`} type="date" value={line.expiry_date} onChange={(event) => setStockLines((rows) => rows.map((item, i) => i === index ? { ...item, expiry_date: event.target.value } : item))} /> : "—"}</td>
                {(stockForm.movement_type === "receipt" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "in")) && <td><input aria-label={`تكلفة الوحدة ${index + 1}`} type="number" min="0" step="0.0001" value={line.unit_cost} onChange={(event) => setStockLines((rows) => rows.map((item, i) => i === index ? { ...item, unit_cost: event.target.value } : item))} /></td>}
                <td><button type="button" className="remove-line-button" disabled={stockLines.length === 1} onClick={() => setStockLines((rows) => rows.filter((_, i) => i !== index))}>×</button></td></tr>;
            })}
          </tbody></table></div>
          <small>يمكن إضافة عدة أصناف في نفس الحركة. يتم تحديد موقع المخزن على مستوى الحركة، وتاريخ الصلاحية وتكلفة الوحدة لكل صنف على حدة.</small>
        </div>
        <label className="inventory-wide">ملاحظات<textarea value={stockForm.notes} onChange={(e) => setStockForm({ ...stockForm, notes: e.target.value })} /></label>
      </>}
      <div className="inventory-actions"><button className="primary-button" disabled={busy} type="submit">{busy ? "جارٍ الحفظ…" : "حفظ"}</button><button className="subtle-button" type="button" onClick={() => setShowForm(false)}>إلغاء</button></div>
    </form>}
    <div className="table-toolbar" style={tab === "transfers" ? { display: "none" } : undefined}><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="بحث سريع…" /><span className="module-count">{busy ? "جارٍ التحديث…" : "تحديث"}</span><button className="subtle-button" onClick={() => void refresh()}>↻</button></div>
    <div className="data-table-wrap" style={tab === "transfers" ? { display: "none" } : undefined}>
      {tab === "products" && <table><thead><tr><th>الكود</th><th>الصنف</th><th>الباركود</th><th>النوع</th><th>تصنيف المخزون</th><th>الوحدة</th><th>سعر البيع</th><th>طريقة التقييم</th><th>الحالة</th></tr></thead><tbody>{filteredProducts.map((p) => <tr key={p.id}><td className="code-cell">{p.code}</td><td>{p.name_ar}</td><td>{p.barcode || "—"}</td><td>{p.product_type === "goods" ? "مخزني" : "خدمة"}</td><td>{p.stock_class === "raw_material" ? "مواد خام" : p.stock_class === "finished_goods" ? "منتجات تامة" : p.stock_class === "operating_supplies" ? "مستلزمات تشغيل" : p.stock_class === "spare_parts" ? "قطع غيار" : "—"}</td><td>{p.unit_name}</td><td>{formatMoney(p.sale_price)}</td><td>{p.costing_method === "fifo" ? "FIFO" : "متوسط مرجح"}</td><td><span className={p.is_active ? "customer-status-active" : "customer-status-inactive"}>{p.is_active ? "نشط" : "موقوف"}</span></td></tr>)}</tbody></table>}
      {tab === "categories" && <table><thead><tr><th>الكود</th><th>اسم المجموعة</th><th>الحالة</th></tr></thead><tbody>{categories.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "units" && <table><thead><tr><th>الكود</th><th>اسم الوحدة</th><th>الرمز</th><th>الدقة</th><th>الحالة</th></tr></thead><tbody>{units.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.symbol || "—"}</td><td>{x.decimal_places}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "reorder" && <table><thead><tr><th>الصنف</th><th>المخزن</th><th>الرصيد الحالي</th><th>حد إعادة الطلب</th><th>المصدر</th><th>الحالة</th></tr></thead><tbody>{warehouseSettings.filter((item) => item.is_active && `${item.product_code} ${item.product_name} ${item.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((item) => {
        const quantity = balances.filter((balance) => balance.product === item.product && balance.warehouse === item.warehouse).reduce((sum, balance) => sum + Number(balance.quantity), 0);
        return <tr key={item.id}><td>{item.product_code} - {item.product_name}</td><td>{item.warehouse_name}</td><td>{quantity.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(item.min_stock).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>خاص بالمخزن</td><td><span className={quantity <= Number(item.min_stock) && Number(item.min_stock) > 0 ? "status-pill reversed" : "status-pill draft"}>{Number(item.min_stock) > 0 && quantity <= Number(item.min_stock) ? "يحتاج إعادة طلب" : "طبيعي"}</span></td></tr>;
      })}{!warehouseSettings.filter((item) => item.is_active).length && <tr><td colSpan={6}>لا توجد حدود مخصصة بعد. أضف حدًا لكل صنف ومخزن حسب الحاجة.</td></tr>}</tbody></table>}
      {tab === "warehouses" && <table><thead><tr><th>الكود</th><th>اسم المخزن</th><th>الفرع</th><th>العنوان</th><th>الحالة</th></tr></thead><tbody>{warehouses.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.branch_code || "عام"}</td><td>{x.address || "—"}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "locations" && <table><thead><tr><th>المخزن</th><th>الكود</th><th>اسم الموقع</th><th>النوع</th><th>الموقع الأب</th><th>الحالة</th></tr></thead><tbody>{locations.filter((x) => `${x.code} ${x.name_ar} ${x.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td>{x.warehouse_name}</td><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{{ floor: "دور", area: "منطقة", rack: "رف", bin: "خانة" }[x.location_type]}</td><td>{locations.find((parent) => parent.id === x.parent)?.name_ar || "—"}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "movements" && <>
        <div className="inventory-tabs movement-status-filters" role="group" aria-label="تصفية حالة الحركات">
          <button type="button" className={movementStatusFilter === "all" ? "inventory-tab active" : "inventory-tab"} onClick={() => setMovementStatusFilter("all")}>كل الحركات <b>{movements.length}</b></button>
          <button type="button" className={movementStatusFilter === "draft" ? "inventory-tab active" : "inventory-tab"} onClick={() => setMovementStatusFilter("draft")}>المعلقة <b>{movements.filter((m) => m.status === "draft").length}</b></button>
          <button type="button" className={movementStatusFilter === "posted" ? "inventory-tab active" : "inventory-tab"} onClick={() => setMovementStatusFilter("posted")}>المرحّلة <b>{movements.filter((m) => m.status === "posted").length}</b></button>
        </div>
        <table><thead><tr><th>رقم الحركة</th><th>النوع</th><th>سبب التسوية</th><th>التاريخ</th><th>من</th><th>إلى</th><th>الأصناف</th><th>قيمة الحركة</th><th>قيود اليومية</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{movements.filter((m) => (movementStatusFilter === "all" || m.status === movementStatusFilter) && `${m.movement_number} ${m.notes}`.toLowerCase().includes(search.toLowerCase())).map((m) => <tr key={m.id}><td className="code-cell">{m.movement_number}</td><td>{{ receipt: "استلام", issue: "صرف", sup_return: "مرتجع مورد", sup_rev: "عكس مرتجع مورد", sal_rev: "عكس مردود مبيعات", transfer: "تحويل", adjustment: "تسوية" }[m.movement_type]}</td><td>{m.movement_type === "adjustment" ? (({ count: "جرد فعلي", damage: "تالف", expired: "منتهي الصلاحية", write_off: "شطب أو إعدام", other: "أخرى" } as Record<string, string>)[m.adjustment_reason] || "—") : "—"}</td><td>{m.movement_date}</td><td>{m.source_warehouse_name || "—"}</td><td>{m.destination_warehouse_name || "—"}</td><td>{m.lines.length}</td><td>{formatMoney(sumDecimalToFixed(m.lines.map((line) => line.cost_amount || "0"), 4))}</td><td>{m.journal_entries.length ? m.journal_entries.map((entry) => entry.entry_number).join("، ") : "—"}</td><td>{m.status === "posted" ? "مرحّلة" : "مسودة"}</td><td>{m.status === "draft" ? <button className="subtle-button" disabled={busy} onClick={async () => { setBusy(true); setError(""); try { await stockApi.postMovement(m.id); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل الحركة."); } finally { setBusy(false); } }}>ترحيل</button> : "—"}</td></tr>)}</tbody></table>
      </>}
      {tab === "counts" && <div className="inventory-reports">
        <section className="activity-panel inventory-report-section">
          <div className="activity-title"><div><h3>مستندات الجرد الفعلي</h3><p>كل جرد يبدأ بلقطة رصيد ثابتة، ثم تُدخل الكميات المعدودة وتُرحّل الفروق بمستندات تسوية مرتبطة.</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>رقم الجرد</th><th>التاريخ</th><th>المخزن</th><th>عدد البنود</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>
            {counts.filter((item) => `${item.count_number} ${item.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((item) => <tr key={item.id}>
              <td className="code-cell">{item.count_number}</td><td>{item.count_date}</td><td>{item.warehouse_name}</td><td>{item.lines_count}</td>
              <td><span className={`status-pill ${item.status === "posted" ? "posted" : item.status === "cancelled" ? "reversed" : "draft"}`}>{item.status === "posted" ? "مرحّل" : item.status === "cancelled" ? "ملغي" : "قيد العد"}</span></td>
              <td><button type="button" className="subtle-button" onClick={() => setSelectedCountId(item.id)}>عرض البنود</button></td>
            </tr>)}
            {!counts.length && <tr><td colSpan={6}>لا توجد مستندات جرد. ابدأ جردًا جديدًا من زر إضافة جديد.</td></tr>}
          </tbody></table></div>
        </section>
        {counts.find((item) => item.id === selectedCountId) && (() => {
          const selected = counts.find((item) => item.id === selectedCountId)!;
          const pendingLines = selected.lines.filter((line) => line.counted_quantity === null).length;
          const varianceLines = selected.lines.filter((line) => line.counted_quantity !== null && Number(line.variance_quantity || "0") !== 0).length;
          const selectedWarehouseLocations = locations.filter((item) => item.is_active && item.warehouse === selected.warehouse);
          return <section className="activity-panel inventory-report-section">
            <div className="activity-title"><div><h3>تفاصيل الجرد: {selected.count_number}</h3><p>{selected.warehouse_name} · لقطة الرصيد {new Date(selected.created_at).toLocaleString("ar-EG")}</p></div>
              {selected.status === "draft" && <div className="inventory-actions">
                <button type="button" className="primary-button" disabled={busy || pendingLines > 0} onClick={async () => {
                  setBusy(true); setError("");
                  try { await stockApi.postCount(selected.id); await refresh(); }
                  catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر اعتماد الجرد."); }
                  finally { setBusy(false); }
                }}>ترحيل فروق الجرد ({varianceLines})</button>
                <button type="button" className="subtle-button" disabled={busy} onClick={async () => {
                  if (!window.confirm("هل تريد إلغاء مستند الجرد؟ لن يتم تعديل أرصدة المخزون.")) return;
                  setBusy(true); setError("");
                  try { await stockApi.cancelCount(selected.id); await refresh(); }
                  catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إلغاء الجرد."); }
                  finally { setBusy(false); }
                }}>إلغاء الجرد</button>
              </div>}
            </div>
            {selected.status === "draft" && <p className="inventory-report-note">أدخل صفرًا إذا لم يوجد الصنف فعليًا. لا يمكن اعتماد الجرد قبل إدخال الكمية لكل بند. أي حركة جديدة بعد لقطة الجرد تتطلب بدء جرد جديد.</p>}
            <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>الموقع</th><th>الصلاحية</th><th>رصيد الدفاتر</th><th>الكمية الفعلية</th><th>الفرق</th><th>تكلفة الزيادة</th><th>الحالة</th><th>تم العد بواسطة</th></tr></thead><tbody>
              {selected.lines.map((line) => <tr key={line.id}>
                <td>{line.product_code} - {line.product_name}</td><td>{line.location_name || "عام"}</td><td>{line.expiry_date || "—"}</td>
                <td>{Number(line.system_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td>
                <td><input key={`counted-${line.id}-${line.updated_at}`} aria-label={`الكمية الفعلية ${line.product_code}`} type="number" min="0" step="0.0001" disabled={selected.status !== "draft"} defaultValue={line.counted_quantity ?? ""} onBlur={async (event) => {
                  if (selected.status !== "draft" || event.target.value === (line.counted_quantity ?? "")) return;
                  setBusy(true); setError("");
                  try { await stockApi.updateCountLine(line.id, { counted_quantity: event.target.value }); await refresh(); }
                  catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ الكمية المعدودة."); }
                  finally { setBusy(false); }
                }} /></td>
                <td>{line.variance_quantity === null ? "—" : Number(line.variance_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td>
                <td>{selected.status === "draft" && Number(line.variance_quantity || "0") >= 0 ? <input key={`cost-${line.id}-${line.updated_at}`} aria-label={`تكلفة الزيادة ${line.product_code}`} type="number" min="0" step="0.0001" defaultValue={line.adjustment_unit_cost} onBlur={async (event) => {
                  if (event.target.value === line.adjustment_unit_cost) return;
                  setBusy(true); setError("");
                  try { await stockApi.updateCountLine(line.id, { adjustment_unit_cost: event.target.value }); await refresh(); }
                  catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ تكلفة الزيادة."); }
                  finally { setBusy(false); }
                }} /> : formatMoney(line.adjustment_unit_cost)}</td>
                <td>{line.counted_quantity === null ? "لم يُعد" : Number(line.variance_quantity || "0") === 0 ? "مطابق" : Number(line.variance_quantity || "0") > 0 ? "زيادة" : "عجز"}</td>
                <td>{line.counted_by_name || "—"}{selected.status === "draft" && Number(line.system_quantity) === 0 && <button type="button" className="remove-line-button" title="حذف البند الإضافي" disabled={busy} onClick={async () => {
                  if (!window.confirm("حذف هذا الصنف الإضافي من الجرد؟")) return;
                  setBusy(true); setError("");
                  try { await stockApi.removeExtraCountLine(line.id); await refresh(); }
                  catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حذف بند الجرد."); }
                  finally { setBusy(false); }
                }}>×</button>}</td>
              </tr>)}
              {!selected.lines.length && <tr><td colSpan={9}>لا توجد أرصدة عند بدء الجرد. أضف الأصناف التي تم العثور عليها فعليًا من النموذج أدناه.</td></tr>}
            </tbody></table></div>
            {selected.status === "draft" && <div className="inventory-form">
              <h4 className="inventory-wide">إضافة صنف غير موجود في رصيد الدفاتر</h4>
              <label>الصنف<select required value={countLineDraft.product} onChange={(e) => setCountLineDraft({ ...countLineDraft, product: e.target.value, expiry_date: "" })}><option value="">اختر الصنف</option>{products.filter((item) => item.product_type === "goods" && item.is_active).map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label>
              <label>موقع التخزين<select value={countLineDraft.location} onChange={(e) => setCountLineDraft({ ...countLineDraft, location: e.target.value })}><option value="">الموقع العام</option>{selectedWarehouseLocations.map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label>
              {products.find((item) => String(item.id) === countLineDraft.product)?.track_expiry && <label>تاريخ الصلاحية<input type="date" required value={countLineDraft.expiry_date} onChange={(e) => setCountLineDraft({ ...countLineDraft, expiry_date: e.target.value })} /></label>}
              <label>الكمية الفعلية<input type="number" min="0.0001" step="0.0001" required value={countLineDraft.counted_quantity} onChange={(e) => setCountLineDraft({ ...countLineDraft, counted_quantity: e.target.value })} /></label>
              <label>تكلفة الوحدة التقديرية<input type="number" min="0.0001" step="0.0001" required value={countLineDraft.adjustment_unit_cost} onChange={(e) => setCountLineDraft({ ...countLineDraft, adjustment_unit_cost: e.target.value })} /></label>
              <label className="inventory-wide">ملاحظات<textarea value={countLineDraft.notes} onChange={(e) => setCountLineDraft({ ...countLineDraft, notes: e.target.value })} /></label>
              <div className="inventory-actions inventory-wide"><button type="button" className="primary-button" disabled={busy || !countLineDraft.product || !countLineDraft.counted_quantity} onClick={async () => {
                setBusy(true); setError("");
                try {
                  const selectedProduct = products.find((item) => String(item.id) === countLineDraft.product);
                  await stockApi.createCountLine({
                    count: selected.id, product: Number(countLineDraft.product),
                    location: countLineDraft.location ? Number(countLineDraft.location) : null,
                    expiry_date: selectedProduct?.track_expiry ? countLineDraft.expiry_date : null,
                    counted_quantity: countLineDraft.counted_quantity,
                    adjustment_unit_cost: countLineDraft.adjustment_unit_cost,
                    notes: countLineDraft.notes,
                  });
                  setCountLineDraft({ product: "", location: "", expiry_date: "", counted_quantity: "", adjustment_unit_cost: "0", notes: "" });
                  await refresh();
                } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إضافة الصنف للجرد."); }
                finally { setBusy(false); }
              }}>إضافة بند الجرد</button></div>
            </div>}
            {selected.status === "posted" && <p className="inventory-report-note">تم ترحيل الجرد. مستند زيادة المخزون: {selected.gain_movement_number || "لا يوجد"} · مستند عجز المخزون: {selected.loss_movement_number || "لا يوجد"}.</p>}
          </section>;
        })()}
      </div>}
      {tab === "accounting" && <table><thead><tr><th>الدور المحاسبي</th><th>الحساب</th><th>الحالة</th></tr></thead><tbody>{[{ role: "inventory", label: "حساب المخزون" }, { role: "goods_received_not_invoiced", label: "بضاعة مستلمة غير مفوترة" }, { role: "cogs", label: "تكلفة المبيعات" }, { role: "inventory_adjustment_gain", label: "أرباح تسوية المخزون" }, { role: "inventory_adjustment_loss", label: "خسائر تسوية المخزون" }, { role: "inter_branch_clearing", label: "تسوية بين الفروع" }].map((item) => { const activityId = roleForm.activity ? Number(roleForm.activity) : null; const assigned = roleAssignments.find((x) => x.role === item.role && x.activity === activityId) || (activityId !== null ? roleAssignments.find((x) => x.role === item.role && x.activity === null) : undefined); const account = accounts.find((x) => x.id === assigned?.account); return <tr key={item.role}><td>{item.label}</td><td>{assigned ? `${account?.code || assigned.account_code} - ${account?.name_ar || ""}` : "غير مربوط"}</td><td>{assigned ? (assigned.activity === null ? "افتراضي الشركة" : "خاص بالنشاط") : "مطلوب"}</td></tr>; })}</tbody></table>}
      {tab === "balances" && <table><thead><tr><th>كود الصنف</th><th>الصنف</th><th>المخزن</th><th>موقع التخزين</th><th>الصلاحية</th><th>الرصيد</th><th>حالة الرصيد</th><th>متوسط التكلفة</th><th>قيمة المخزون</th></tr></thead><tbody>{balances.filter((b) => `${b.product_code} ${b.product_name} ${b.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((b) => { const product = products.find((item) => item.id === b.product); const setting = warehouseSettingIndex.get(`${b.warehouse}:${b.product}`); const totalQuantity = inventoryIndexes.byWarehouseProduct.get(`${b.warehouse}:${b.product}`)?.quantity ?? Number(b.quantity); const minStock = setting ? Number(setting.min_stock) : Number(product?.min_stock || 0); const stockState = totalQuantity <= 0 ? "نفد" : minStock > 0 && totalQuantity <= minStock ? "يحتاج شراء" : "متاح"; const stockClass = totalQuantity <= 0 ? "reversed" : minStock > 0 && totalQuantity <= minStock ? "draft" : "posted"; return <tr key={b.id}><td className="code-cell">{b.product_code}</td><td>{b.product_name}</td><td>{b.warehouse_name}</td><td>{b.location_name || "عام"}</td><td>{b.expiry_date || "—"}</td><td>{Number(b.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td><span className={`status-pill ${stockClass}`}>{stockState}</span></td><td>{formatMoney(b.average_cost)}</td><td>{formatMoney(b.inventory_value)}</td></tr>; })}</tbody></table>}
      {tab === "stock-card" && <div className="inventory-reports">
        <section className="activity-panel inventory-report-section">
          <div className="activity-title"><div><h3>بطاقة حركة الصنف</h3><p>حركة الوارد والمنصرف والرصيد المتدرج حسب المخزن والفترة</p></div>{stockCard && <button type="button" className="subtle-button" onClick={exportStockCard}>تصدير Excel (CSV)</button>}</div>
          <div className="stock-card-filters">
            <label>الصنف<select value={stockCardForm.product} onChange={(e) => setStockCardForm({ ...stockCardForm, product: e.target.value })}><option value="">اختر الصنف</option>{products.filter((item) => item.product_type === "goods" && item.is_active).map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label>
            <label>المخزن<select value={stockCardForm.warehouse} onChange={(e) => setStockCardForm({ ...stockCardForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}{item.is_active ? "" : " (موقوف - سجل تاريخي فقط)"}</option>)}</select></label>
            <label>من تاريخ<input type="date" value={stockCardForm.date_from} onChange={(e) => setStockCardForm({ ...stockCardForm, date_from: e.target.value })} /></label>
            <label>إلى تاريخ<input type="date" value={stockCardForm.date_to} onChange={(e) => setStockCardForm({ ...stockCardForm, date_to: e.target.value })} /></label>
            <button type="button" className="primary-button" disabled={stockCardBusy} onClick={() => void loadStockCard()}>{stockCardBusy ? "جارٍ التحميل…" : "عرض البطاقة"}</button>
          </div>
          {stockCardError && <div className="error-banner">{stockCardError}</div>}
          {stockCard && <>
            <div className="stock-card-summary"><div><span>الصنف</span><strong>{stockCard.product_code} - {stockCard.product_name}</strong></div><div><span>المخزن</span><strong>{stockCard.warehouse_name}</strong></div><div><span>رصيد أول المدة</span><strong>{Number(stockCard.opening_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} / {formatMoney(stockCard.opening_value)}</strong></div><div><span>رصيد آخر المدة</span><strong>{Number(stockCard.closing_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} / {formatMoney(stockCard.closing_value)}</strong></div></div>
            <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>وقت الترحيل</th><th>رقم الحركة</th><th>نوع الحركة</th><th>وارد كمية</th><th>منصرف كمية</th><th>تكلفة الوحدة</th><th>وارد قيمة</th><th>منصرف قيمة</th><th>الرصيد الكمي</th><th>قيمة الرصيد</th></tr></thead><tbody>{stockCard.rows.map((row, index) => <tr key={`${row.movement_number}-${index}`}><td>{row.date}</td><td>{row.posted_at ? new Date(row.posted_at).toLocaleString("ar-EG") : "—"}</td><td className="code-cell">{row.movement_number}</td><td>{({ receipt: "استلام", issue: "صرف", sup_return: "مرتجع مورد", sup_rev: "عكس مرتجع مورد", sal_rev: "عكس مردود مبيعات", transfer: "تحويل", adjustment: "تسوية" } as Record<string, string>)[row.movement_type] || row.movement_type}</td><td>{Number(row.quantity_in).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.quantity_out).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.unit_cost)}</td><td>{formatMoney(row.value_in)}</td><td>{formatMoney(row.value_out)}</td><td>{Number(row.balance_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.balance_value)}</td></tr>)}{!stockCard.rows.length && <tr><td colSpan={11}>لا توجد حركات مرحلة خلال الفترة المحددة.</td></tr>}</tbody></table></div>
            <div className="activity-title" style={{ alignItems: "center", marginTop: 12 }}>
              <p>عرض {stockCard.rows.length.toLocaleString("ar-EG")} من إجمالي {stockCard.pagination.total_rows.toLocaleString("ar-EG")} حركة — الصفحة {stockCard.pagination.page.toLocaleString("ar-EG")} من {stockCard.pagination.total_pages.toLocaleString("ar-EG")}. التصدير يشمل الصفحة المعروضة فقط.</p>
              <div style={{ display: "flex", gap: 8 }}>
                <button type="button" className="subtle-button" disabled={!stockCard.pagination.has_previous || stockCardBusy} onClick={() => void loadStockCard(stockCard.pagination.page - 1)}>السابق</button>
                <button type="button" className="subtle-button" disabled={!stockCard.pagination.has_next || stockCardBusy} onClick={() => void loadStockCard(stockCard.pagination.page + 1)}>التالي</button>
              </div>
            </div>
          </>}
          {!stockCard && !stockCardError && <div className="table-empty">حدد الصنف والمخزن والفترة ثم اعرض بطاقة الحركة.</div>}
        </section>
      </div>}
      {tab === "reports" && <div className="inventory-reports">
        <div className="metrics-grid inventory-report-metrics">
          <article className="metric-card"><div className="metric-top">قيمة المخزون</div><strong className="metric-value">{formatMoney(totalInventoryValue)}</strong><small>حسب تكلفة الأرصدة الحالية</small></article>
          <article className="metric-card"><div className="metric-top">إجمالي الكميات</div><strong className="metric-value">{totalInventoryQuantity.toLocaleString("ar-EG", { maximumFractionDigits: 3 })}</strong><small>مجموع أرصدة جميع المخازن</small></article>
          <article className="metric-card"><div className="metric-top">أصناف عند حد الطلب</div><strong className="metric-value">{reorderAlerts.length.toLocaleString("ar-EG")}</strong><small>وفق حد كل صنف داخل كل مخزن</small></article>
          <article className="metric-card"><div className="metric-top">تنبيهات الصلاحية</div><strong className="metric-value">{expiryAlerts.length.toLocaleString("ar-EG")}</strong><small>منتهية أو خلال 30 يوماً</small></article>
        </div>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>تقييم المخزون حسب المخزن</h3><p>قيمة وكمية الأرصدة الحالية</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>المخزن</th><th>عدد الأصناف</th><th>إجمالي الكمية</th><th>قيمة المخزون</th></tr></thead><tbody>{warehouseValuation.filter((row) => row.warehouse.name_ar.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.warehouse.id}><td>{row.warehouse.name_ar}</td><td>{row.products}</td><td>{row.quantity.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.value)}</td></tr>)}{!warehouseValuation.length && <tr><td colSpan={4}>لا توجد أرصدة مخزنية حالياً.</td></tr>}</tbody></table></div>
        </section>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>أصناف وصلت إلى حد إعادة الطلب</h3><p>اقتراح الشراء بعد خصم أوامر البيع المفتوحة وإضافة كميات أوامر الشراء غير المستلمة، وفق حد إعادة الطلب المحدد لكل مخزن</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الصنف</th><th>المخزن</th><th>الرصيد</th><th>المحجوز</th><th>المتاح</th><th>أوامر شراء مفتوحة</th><th>المتاح المتوقع</th><th>حد إعادة الطلب</th><th>الكمية المقترحة</th></tr></thead><tbody>{reorderAlerts.filter((row) => `${row.product_code} ${row.product_name} ${row.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={`${row.product}-${row.warehouse}`}><td className="code-cell">{row.product_code}</td><td>{row.product_name}</td><td>{row.warehouse_name}</td><td>{Number(row.on_hand).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.reserved).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.available).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.on_order).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.projected_available).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.reorder_point).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td><strong>{Number(row.suggested_purchase_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></td></tr>)}{!reorderAlerts.length && <tr><td colSpan={10}>لا توجد أصناف تحتاج إلى إعادة طلب.</td></tr>}</tbody></table></div>
        </section>
        <section className="activity-panel inventory-report-section">
          <div className="activity-title"><div><h3>تحليل حركة وأعمار المخزون</h3><p>تصنيف رقابي حسب عدد الأيام منذ آخر حركة مرحلة على الصنف داخل المخزن؛ لا يمثل تاريخ شراء الطبقة أو عمرها المحاسبي</p></div><select aria-label="تصفية حالة حركة المخزون" value={stockAgeFilter} onChange={(event) => setStockAgeFilter(event.target.value as typeof stockAgeFilter)}><option value="all">كل الحالات</option><option value="active">نشط</option><option value="slow">بطيء</option><option value="very_slow">بطيء جدًا</option><option value="stagnant">راكد</option><option value="unknown">غير محدد</option></select></div>
          <div className="metrics-grid inventory-report-metrics">
            <article className="metric-card"><div className="metric-top">نشط · 0–30 يوم</div><strong className="metric-value">{stockAgingCounts.active.toLocaleString("ar-EG")}</strong><small>صنف / مخزن</small></article>
            <article className="metric-card"><div className="metric-top">بطيء · 31–90 يوم</div><strong className="metric-value">{stockAgingCounts.slow.toLocaleString("ar-EG")}</strong><small>صنف / مخزن</small></article>
            <article className="metric-card"><div className="metric-top">بطيء جدًا · 91–180 يوم</div><strong className="metric-value">{stockAgingCounts.very_slow.toLocaleString("ar-EG")}</strong><small>صنف / مخزن</small></article>
            <article className="metric-card"><div className="metric-top">راكد · أكثر من 180 يوم</div><strong className="metric-value">{stockAgingCounts.stagnant.toLocaleString("ar-EG")}</strong><small>صنف / مخزن</small></article>
          </div>
          <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الصنف</th><th>المخزن</th><th>الرصيد</th><th>القيمة</th><th>آخر حركة مرحلة</th><th>أيام دون حركة</th><th>التصنيف</th></tr></thead><tbody>{stockAgingRows.filter((row) => (stockAgeFilter === "all" || row.ageBand === stockAgeFilter) && `${row.product_code} ${row.product_name} ${row.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={`${row.product}-${row.warehouse}`}><td className="code-cell">{row.product_code}</td><td>{row.product_name}</td><td>{row.warehouse_name}</td><td>{row.quantity.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.value)}</td><td>{row.last_movement || "لا توجد حركة مسجلة"}</td><td>{row.ageDays === null ? "—" : row.ageDays.toLocaleString("ar-EG")}</td><td><span className={`status-pill ${row.ageBand === "stagnant" ? "reversed" : row.ageBand === "very_slow" ? "slow" : row.ageBand === "slow" ? "draft" : row.ageBand === "active" ? "posted" : "unknown"}`}>{row.ageBand === "active" ? "نشط" : row.ageBand === "slow" ? "بطيء" : row.ageBand === "very_slow" ? "بطيء جدًا" : row.ageBand === "stagnant" ? "راكد" : "غير محدد"}</span></td></tr>)}{!stockAgingRows.length && <tr><td colSpan={8}>لا توجد أرصدة موجبة لتحليلها.</td></tr>}</tbody></table></div>
          {stockAgingCounts.unknown > 0 && <p className="inventory-report-note">يوجد {stockAgingCounts.unknown.toLocaleString("ar-EG")} صنف/مخزن بلا حركة مرحلة مسجلة؛ لم نفترض لها عمرًا أو نصنفها كراكد.</p>}{stockAging?.truncated && <p className="inventory-report-note">تم عرض أول 5000 صنف/مخزن. إجماليات الشرائح تشمل جميع النتائج.</p>}
        </section>
        <section className="activity-panel inventory-report-section">
          <div className="activity-title"><div><h3>أعمار طبقات التكلفة وفق FIFO</h3><p>العمر محسوب من تاريخ الاستلام الأصلي المرتبط بطبقة التكلفة، وليس من آخر حركة مخزنية</p></div><button type="button" className="primary-button" disabled={costAgingBusy} onClick={() => void loadCostAging()}>{costAgingBusy ? "جارٍ التحميل…" : "تحديث تقرير أعمار التكلفة"}</button></div>
          {costAgingError && <div className="error-banner">{costAgingError}</div>}
          {costAging && <>
            <div className="metrics-grid inventory-report-metrics">
              <article className="metric-card"><div className="metric-top">قيمة طبقات FIFO المتبقية</div><strong className="metric-value">{formatMoney(costAging.totals.value)}</strong><small>حتى {costAging.as_of}</small></article>
              <article className="metric-card"><div className="metric-top">كمية متبقية</div><strong className="metric-value">{Number(costAging.totals.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong><small>{costAging.totals.layers.toLocaleString("ar-EG")} طبقة تكلفة</small></article>
              <article className="metric-card"><div className="metric-top">بطيء جدًا · 91–180 يوم</div><strong className="metric-value">{formatMoney(costAging.buckets.very_slow.value)}</strong><small>{Number(costAging.buckets.very_slow.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} وحدة</small></article>
              <article className="metric-card"><div className="metric-top">راكد · أكثر من 180 يوم</div><strong className="metric-value">{formatMoney(costAging.buckets.stagnant.value)}</strong><small>{Number(costAging.buckets.stagnant.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} وحدة</small></article>
            </div>
            <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الصنف</th><th>المخزن / الموقع</th><th>رقم الاستلام الأصلي</th><th>تاريخ الاستلام</th><th>العمر بالأيام</th><th>الكمية المتبقية</th><th>تكلفة الوحدة</th><th>قيمة الطبقة</th><th>الصلاحية</th></tr></thead><tbody>{costAging.rows.filter((row) => `${row.product_code} ${row.product_name} ${row.warehouse_name} ${row.source_movement_number}`.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.id}><td className="code-cell">{row.product_code}</td><td>{row.product_name}</td><td>{row.warehouse_name}{row.location_name ? ` / ${row.location_name}` : ""}</td><td>{row.source_movement_number}</td><td>{row.receipt_date}</td><td>{row.age_days.toLocaleString("ar-EG")}</td><td>{Number(row.quantity_remaining).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.unit_cost)}</td><td>{formatMoney(row.layer_value)}</td><td>{row.expiry_status === "expired" ? <span className="status-pill reversed">منتهي</span> : row.expiry_status === "expiring" ? <span className="status-pill draft">ينتهي خلال 30 يومًا</span> : row.expiry_status === "valid" ? <span className="status-pill posted">ساري</span> : "غير محدد"}</td></tr>)}{!costAging.rows.length && <tr><td colSpan={10}>لا توجد طبقات FIFO متبقية بتاريخ التقرير.</td></tr>}</tbody></table></div>
            {costAging.truncated && <p className="inventory-report-note">تم عرض أول 500 طبقة فقط لتجنب تحميل مفرط؛ الإجماليات تشمل جميع الطبقات ضمن نطاق صلاحياتك.</p>}{costAging.untraceable_legacy_layers_count > 0 && <p className="inventory-report-note">تنبيه: توجد {costAging.untraceable_legacy_layers_count.toLocaleString("ar-EG")} طبقة قديمة منقولة لا يمكن تتبع جذر استلامها بدقة؛ تم استبعادها من أعمار FIFO حتى لا نعرض عمرًا غير موثوق.</p>}
            <p className="inventory-report-note">{costAging.basis} {costAging.scope_note}</p>
          </>}
          {!costAging && !costAgingError && <p className="inventory-report-note">اضغط تحديث التقرير لعرض طبقات FIFO المتبقية. أصناف المتوسط المرجح مستبعدة من أعمار الطبقات لأن توزيعها لا يمثل طبقات تكلفة مستقلة.</p>}
        </section>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>تنبيهات الصلاحية</h3><p>الأصناف المنتهية أو التي تنتهي خلال 30 يوماً</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>المخزن</th><th>الموقع</th><th>تاريخ الصلاحية</th><th>الأيام المتبقية</th><th>الكمية</th><th>القيمة</th></tr></thead><tbody>{expiryAlerts.filter((row) => `${row.product_code} ${row.product_name} ${row.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.id}><td>{row.product_code} - {row.product_name}</td><td>{row.warehouse_name}</td><td>{row.location_name || "عام"}</td><td>{row.expiry_date}</td><td><span className={row.days < 0 ? "status-pill reversed" : "status-pill draft"}>{row.days < 0 ? `منتهي منذ ${Math.abs(row.days)} يوم` : row.days === 0 ? "ينتهي اليوم" : `${row.days} يوم`}</span></td><td>{Number(row.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.inventory_value)}</td></tr>)}{!expiryAlerts.length && <tr><td colSpan={7}>لا توجد تنبيهات صلاحية خلال الفترة المحددة.</td></tr>}</tbody></table></div>
        </section>
        <section className="activity-panel inventory-report-section">
          <div className="activity-title"><div><h3>سلامة تقييم المخزون والحركات المعلقة</h3><p>مراجعة طبقات FIFO مقابل أرصدة المخزون، وحصر المسودات التي لم تُرحّل</p></div><button type="button" className="primary-button" disabled={valuationIntegrityBusy} onClick={() => void loadValuationIntegrity()}>{valuationIntegrityBusy ? "جارٍ الفحص…" : "فحص التقييم والحركات"}</button></div>
          {valuationIntegrityError && <div className="error-banner">{valuationIntegrityError}</div>}
          {valuationIntegrity && <>
            <div className="metrics-grid inventory-report-metrics">
              <article className="metric-card"><div className="metric-top">اختلافات FIFO</div><strong className="metric-value">{valuationIntegrity.fifo_mismatch_count.toLocaleString("ar-EG")}</strong><small>مقارنة الكمية والقيمة بين الرصيد وطبقات التكلفة</small></article>
              <article className="metric-card"><div className="metric-top">حركات غير مرحلة</div><strong className="metric-value">{valuationIntegrity.pending_movements_count.toLocaleString("ar-EG")}</strong><small>مسودات تحتاج مراجعة أو استكمال</small></article>
            </div>
            <h4>اختلافات طبقات FIFO</h4>
            <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>المخزن</th><th>كمية الرصيد</th><th>كمية الطبقات</th><th>فرق الكمية</th><th>قيمة الرصيد</th><th>قيمة الطبقات</th><th>فرق القيمة</th></tr></thead><tbody>{valuationIntegrity.fifo_mismatches.map((row) => <tr key={`${row.product}-${row.warehouse}-${row.location || 0}-${row.expiry_date || ""}`}><td>{row.product_code} - {row.product_name}</td><td>{row.warehouse_code} - {row.warehouse_name}</td><td>{Number(row.balance_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.layer_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.quantity_difference).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{formatMoney(row.balance_value)}</td><td>{formatMoney(row.layer_value)}</td><td>{formatMoney(row.value_difference)}</td></tr>)}{!valuationIntegrity.fifo_mismatches.length && <tr><td colSpan={8}>لا توجد اختلافات بين طبقات FIFO والأرصدة الحالية.</td></tr>}</tbody></table></div>
            <h4>الحركات المسودة غير المرحلة</h4>
            <div className="data-table-wrap"><table><thead><tr><th>رقم الحركة</th><th>النوع</th><th>التاريخ</th><th>المخزن المصدر</th><th>المخزن الوجهة</th><th>عدد السطور</th><th>أنشأها</th></tr></thead><tbody>{valuationIntegrity.pending_movements.map((row) => <tr key={row.id}><td>{row.movement_number}</td><td>{row.movement_type}</td><td>{row.movement_date}</td><td>{row.source_warehouse || "—"}</td><td>{row.destination_warehouse || "—"}</td><td>{row.lines_count}</td><td>{row.created_by || "—"}</td></tr>)}{!valuationIntegrity.pending_movements.length && <tr><td colSpan={7}>لا توجد حركات مسودة ضمن النطاق المحدد.</td></tr>}</tbody></table></div>
            {valuationIntegrity.pending_movements_truncated && <p className="inventory-report-note">تم عرض أول 100 حركة من إجمالي الحركات المسودة.</p>}
            <p className="inventory-report-note">{valuationIntegrity.scope_note}</p>
          </>}
          {!valuationIntegrity && !valuationIntegrityError && <div className="table-empty">نفّذ الفحص لعرض اختلافات التقييم والحركات المسودة.</div>}
        </section>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>مطابقة المخزون مع دفتر الأستاذ</h3><p>مقارنة قيمة الأرصدة الحالية برصيد حساب المخزون في القيود المرحلة</p></div><button type="button" className="primary-button" disabled={reconciling} onClick={() => void loadReconciliation()}>{reconciling ? "جارٍ الفحص…" : "تنفيذ المطابقة"}</button></div>
          <div className="stock-card-filters"><label>نطاق المطابقة<select value={reconciliationBranch} onChange={(e) => { setReconciliationBranch(e.target.value); setReconciliation(null); setReconciliationError(""); }}><option value="">كل الفروع المسموح بها</option>{branches.map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></label></div>
          {reconciliationError && <div className="error-banner">{reconciliationError}</div>}
          {reconciliation && <div className="reconciliation-result"><div><span>نطاق المطابقة</span><strong>{reconciliation.branch_name || "الفروع المتاحة للمستخدم"}</strong></div><div><span>حساب المخزون</span><strong>{reconciliation.account_code} - {reconciliation.account_name}</strong></div><div><span>قيمة المخزون</span><strong>{formatMoney(reconciliation.stock_value)}</strong></div><div><span>رصيد دفتر الأستاذ</span><strong>{formatMoney(reconciliation.gl_balance)}</strong></div><div><span>الفرق ({reconciliation.currency})</span><strong>{formatMoney(reconciliation.difference)}</strong></div><div><span>سماحية التقريب</span><strong>{formatMoney(reconciliation.rounding_tolerance)} {reconciliation.currency}</strong></div><div className={reconciliation.is_reconciled ? "reconciliation-status matched" : "reconciliation-status mismatch"}>{reconciliation.is_reconciled ? "المخزون مطابق لدفتر الأستاذ" : "يوجد فرق يحتاج إلى مراجعة"}</div></div>}
          {!reconciliation && !reconciliationError && <div className="table-empty">نفّذ المطابقة لعرض نتيجة المقارنة.</div>}
          {reconciliation?.scope_note && <p className="inventory-report-note">{reconciliation.scope_note}</p>}
        </section>
        <p className="inventory-report-note">تُحسب كمية الشراء المقترحة من الرصيد المتاح بعد خصم المحجوز وإضافة أوامر الشراء المفتوحة، ويجب ضبط حد إعادة الطلب لكل صنف داخل المخزن. المطابقة تقارن لقطة الأرصدة الحالية برصيد حساب المخزون في القيود المرحلة؛ ولا تمثل مطابقة تاريخية لفترة سابقة.</p>
      </div>}
    </div>
    {tab === "transfers" && <StockTransfersPanel companyId={companyId} products={products} warehouses={warehouses} />}
  </section>;
}
