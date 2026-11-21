import { useEffect, useMemo, useState } from "react";
import { accountRoleApi, api, inventoryApi, stockApi, type Account, type AccountRole, type InventoryCategory, type InventoryLocation, type InventoryUnit, type Product, type InventoryReconciliation, type StockBalance, type StockMovement, type Warehouse, type WarehouseProductSetting } from "./api";

type Tab = "products" | "categories" | "warehouses" | "units" | "locations" | "movements" | "balances" | "reports" | "reorder" | "accounting";
export default function InventoryPanel({ companyId }: { companyId: number | null }) {
  const [tab, setTab] = useState<Tab>("products");
  const [products, setProducts] = useState<Product[]>([]);
  const [categories, setCategories] = useState<InventoryCategory[]>([]);
  const [units, setUnits] = useState<InventoryUnit[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [warehouseSettings, setWarehouseSettings] = useState<WarehouseProductSetting[]>([]);
  const [reorderForm, setReorderForm] = useState({ product: "", warehouse: "", min_stock: "0" });
  const [locations, setLocations] = useState<InventoryLocation[]>([]);
  const [movements, setMovements] = useState<StockMovement[]>([]);
  const [balances, setBalances] = useState<StockBalance[]>([]);
  const [reconciliation, setReconciliation] = useState<InventoryReconciliation | null>(null);
  const [reconciliationError, setReconciliationError] = useState("");
  const [reconciling, setReconciling] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [roleAssignments, setRoleAssignments] = useState<AccountRole[]>([]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ code: "", name_ar: "", barcode: "", unit: "", category: "", product_type: "goods", purchase_price: "0", sale_price: "0", min_stock: "0", costing_method: "weighted_average", track_expiry: false });
  const [unitForm, setUnitForm] = useState({ code: "", name_ar: "", symbol: "", decimal_places: "2" });
  const [categoryForm, setCategoryForm] = useState({ code: "", name_ar: "" });
  const [warehouseForm, setWarehouseForm] = useState({ code: "", name_ar: "", branch: "", address: "" });
  const [locationForm, setLocationForm] = useState({ warehouse: "", code: "", name_ar: "", location_type: "area", parent: "" });
  const [roleForm, setRoleForm] = useState({ role: "inventory", account: "" });
  const [stockForm, setStockForm] = useState({ movement_number: "", movement_type: "receipt", movement_date: new Date().toISOString().slice(0, 10), source_warehouse: "", destination_warehouse: "", source_location: "", destination_location: "", expiry_date: "", product: "", quantity: "1", unit_cost: "0", adjustment_direction: "in", notes: "" });
  const [stockLines, setStockLines] = useState([{ product: "", quantity: "1", unit_cost: "0", expiry_date: "" }]);

  const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [p, c, u, w, l, m, b, a, ar, ws] = await Promise.all([inventoryApi.products(companyId), inventoryApi.categories(companyId), inventoryApi.units(companyId), inventoryApi.warehouses(companyId), inventoryApi.locations(companyId), stockApi.movements(companyId), stockApi.balances(companyId), api.accounts(companyId), accountRoleApi.list(companyId), inventoryApi.warehouseProductSettings(companyId)]);
      setProducts(p); setCategories(c); setUnits(u); setWarehouses(w); setLocations(l); setMovements(m); setBalances(b); setAccounts(a); setRoleAssignments(ar); setWarehouseSettings(ws);
      setRoleForm((old) => ({ ...old, account: String(ar.find((item) => item.role === old.role)?.account || "") }));
      setLocationForm((old) => ({ ...old, warehouse: old.warehouse || String(w.find((item) => item.is_active)?.id || "") }));
      setReorderForm((old) => ({ ...old, warehouse: old.warehouse || String(w.find((item) => item.is_active)?.id || ""), product: old.product || String(p.find((item) => item.product_type === "goods" && item.is_active)?.id || "") }));
      const defaultProduct = String(p.find((item) => item.product_type === "goods" && item.is_active)?.id || "");
      setStockForm((old) => ({ ...old, source_warehouse: old.source_warehouse || String(w.find((item) => item.is_active)?.id || ""), destination_warehouse: old.destination_warehouse || String(w.find((item) => item.is_active)?.id || "") }));
      setStockLines((old) => old.map((line, index) => ({ ...line, product: line.product || (index === 0 ? defaultProduct : "") })));
      setForm((old) => ({ ...old, unit: old.unit || String(u[0]?.id || ""), category: old.category || String(c[0]?.id || "") }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المخزون."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);
  const loadReconciliation = async () => {
    if (!companyId) return;
    setReconciling(true); setReconciliationError("");
    try { setReconciliation(await stockApi.reconciliation(companyId)); }
    catch (reason) { setReconciliation(null); setReconciliationError(reason instanceof Error ? reason.message : "تعذرت مطابقة المخزون مع دفتر الأستاذ."); }
    finally { setReconciling(false); }
  };
  const filteredProducts = useMemo(() => products.filter((p) => `${p.code} ${p.barcode} ${p.name_ar}`.toLowerCase().includes(search.toLowerCase())), [products, search]);
  const warehouseValuation = useMemo(() => warehouses.map((warehouse) => {
    const rows = balances.filter((balance) => balance.warehouse === warehouse.id);
    return { warehouse, quantity: rows.reduce((sum, row) => sum + Number(row.quantity), 0), value: rows.reduce((sum, row) => sum + Number(row.inventory_value), 0), products: new Set(rows.map((row) => row.product)).size };
  }).filter((row) => row.products > 0), [warehouses, balances]);
  const reorderAlerts = warehouses.filter((warehouse) => warehouse.is_active).flatMap((warehouse) => products.filter((product) => product.product_type === "goods" && product.is_active).map((product) => {
    const setting = warehouseSettings.find((item) => item.product === product.id && item.warehouse === warehouse.id && item.is_active);
    const threshold = setting ? Number(setting.min_stock) : Number(product.min_stock);
    const quantity = balances.filter((balance) => balance.product === product.id && balance.warehouse === warehouse.id).reduce((sum, balance) => sum + Number(balance.quantity), 0);
    return { product, warehouse, quantity, threshold, value: balances.filter((balance) => balance.product === product.id && balance.warehouse === warehouse.id).reduce((sum, balance) => sum + Number(balance.inventory_value), 0), isCustom: Boolean(setting) };
  })).filter((row) => row.threshold > 0 && row.quantity <= row.threshold);
  const expiryAlerts = balances.filter((row) => row.expiry_date && Number(row.quantity) > 0).map((row) => {
    const days = Math.ceil((new Date(row.expiry_date + "T00:00:00").getTime() - new Date().setHours(0, 0, 0, 0)) / 86400000);
    return { ...row, days };
  }).filter((row) => row.days <= 30).sort((a, b) => a.days - b.days);
  const totalInventoryValue = balances.reduce((sum, row) => sum + Number(row.inventory_value), 0);
  const totalInventoryQuantity = balances.reduce((sum, row) => sum + Number(row.quantity), 0);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      if (tab === "products") {
        if (!form.unit) throw new Error("أضف وحدة قياس أولاً.");
        await inventoryApi.createProduct({ company: companyId, code: form.code, name_ar: form.name_ar, barcode: form.barcode, unit: Number(form.unit), category: form.category ? Number(form.category) : null, product_type: form.product_type, purchase_price: form.purchase_price, sale_price: form.sale_price, min_stock: form.min_stock, costing_method: form.costing_method, track_expiry: form.track_expiry });
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
        const payload = { company: companyId, role: roleForm.role, account: Number(roleForm.account) };
        const existing = roleAssignments.find((item) => item.role === roleForm.role);
        if (existing) await accountRoleApi.update(existing.id, payload);
        else await accountRoleApi.create(payload);
        setRoleForm((old) => ({ ...old, account: "" }));
      } else if (tab === "movements") {
        const type = stockForm.movement_type;
        const sourceId = type === "receipt" || (type === "adjustment" && stockForm.adjustment_direction === "in") ? null : Number(stockForm.source_warehouse);
        const destinationId = type === "issue" || (type === "adjustment" && stockForm.adjustment_direction === "out") ? null : Number(stockForm.destination_warehouse);
        if (!stockLines.length || stockLines.some((line) => !line.product || Number(line.quantity) <= 0)) throw new Error("أضف صنفاً واحداً على الأقل وتأكد من اختيار الصنف والكمية الصحيحة لكل سطر.");
        await stockApi.createMovement({ company: companyId, movement_number: stockForm.movement_number, movement_type: type, movement_date: stockForm.movement_date, source_warehouse: sourceId, destination_warehouse: destinationId, notes: stockForm.notes, lines: stockLines.map((line) => ({ product: Number(line.product), quantity: line.quantity, unit_cost: line.unit_cost || "0", source_location: sourceId && stockForm.source_location ? Number(stockForm.source_location) : null, destination_location: destinationId && stockForm.destination_location ? Number(stockForm.destination_location) : null, expiry_date: line.expiry_date || null, adjustment_direction: type === "adjustment" ? stockForm.adjustment_direction : "" })) });
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
    { id: "movements", label: "حركات المخزون", count: movements.length },
    { id: "balances", label: "أرصدة المخزون", count: balances.length },
    { id: "reports", label: "تقارير المخزون", count: warehouseValuation.length + expiryAlerts.length + reorderAlerts.length },
    { id: "reorder", label: "حدود الطلب لكل مخزن", count: warehouseSettings.filter((item) => item.is_active).length },
    { id: "accounting", label: "ربط الحسابات", count: roleAssignments.length },
  ];
  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر شركة لعرض بيانات المخزون.</div></section>;
  return <section className="work-panel inventory-panel">
    <div className="work-panel-head"><div><h2>إدارة المخزون والمستودعات</h2><p>الأصناف والمخازن وحركات الاستلام والصرف والتحويل وأرصدة المخزون وربط الحسابات.</p></div>{tab !== "balances" && tab !== "reports" && <button className="primary-button" onClick={() => setShowForm((v) => !v)}>{showForm ? "إغلاق النموذج" : "＋ إضافة جديد"}</button>}</div>
    <div className="inventory-tabs">{tabs.map((item) => <button key={item.id} className={tab === item.id ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab(item.id); setShowForm(false); setSearch(""); }}>{item.label}<b>{item.count}</b></button>)}</div>
    {error && <div className="error-banner">{error}</div>}
    {showForm && <form className="inventory-form" onSubmit={submit}>
      {tab === "products" && <>
        <label>كود الصنف<input required value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></label>
        <label>اسم الصنف<input required value={form.name_ar} onChange={(e) => setForm({ ...form, name_ar: e.target.value })} /></label>
        <label>الباركود<input value={form.barcode} onChange={(e) => setForm({ ...form, barcode: e.target.value })} /></label>
        <label>نوع السجل<select value={form.product_type} onChange={(e) => setForm({ ...form, product_type: e.target.value })}><option value="goods">صنف مخزني</option><option value="service">خدمة</option></select></label>
        <label>وحدة القياس<select required value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })}><option value="">اختر الوحدة</option>{units.map((u) => <option key={u.id} value={u.id}>{u.name_ar} ({u.code})</option>)}</select></label>
        <label>المجموعة<select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}><option value="">بدون مجموعة</option>{categories.map((c) => <option key={c.id} value={c.id}>{c.name_ar}</option>)}</select></label>
        <label>سعر الشراء<input type="number" min="0" step="0.0001" value={form.purchase_price} onChange={(e) => setForm({ ...form, purchase_price: e.target.value })} /></label>
        <label>سعر البيع<input type="number" min="0" step="0.0001" value={form.sale_price} onChange={(e) => setForm({ ...form, sale_price: e.target.value })} /></label>
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
      {tab === "accounting" && <><label>الدور المحاسبي<select value={roleForm.role} onChange={(e) => { const next = e.target.value; const assigned = roleAssignments.find((item) => item.role === next); setRoleForm({ role: next, account: assigned ? String(assigned.account) : "" }); }}><option value="inventory">حساب المخزون</option><option value="goods_received_not_invoiced">بضاعة مستلمة غير مفوترة</option><option value="cogs">تكلفة المبيعات</option><option value="inventory_adjustment_gain">أرباح تسوية المخزون</option><option value="inventory_adjustment_loss">خسائر تسوية المخزون</option><option value="inter_branch_clearing">تسوية بين الفروع</option></select></label><label>الحساب المرتبط<select required value={roleForm.account} onChange={(e) => setRoleForm({ ...roleForm, account: e.target.value })}><option value="">اختر حساباً</option>{accounts.filter((a) => a.is_active && !a.is_group && a.allow_posting).map((a) => <option key={a.id} value={a.id}>{a.code} - {a.name_ar}</option>)}</select></label><p className="inventory-wide">اربط الأدوار المطلوبة قبل ترحيل الحركات: الاستلام يحتاج المخزون والبضاعة المستلمة غير المفوترة، والصرف يحتاج المخزون وتكلفة المبيعات، وتسويات الجرد تحتاج حسابي الزيادة والعجز، والتحويل بين الفروع يحتاج حساب التسوية.</p></>}
      {tab === "movements" && <>
        <label>رقم الحركة<input required value={stockForm.movement_number} onChange={(e) => setStockForm({ ...stockForm, movement_number: e.target.value })} /></label>
        <label>نوع الحركة<select value={stockForm.movement_type} onChange={(e) => setStockForm({ ...stockForm, movement_type: e.target.value, source_location: "", destination_location: "" })}><option value="receipt">استلام</option><option value="issue">صرف</option><option value="transfer">تحويل بين مخازن</option><option value="adjustment">تسوية جرد</option></select></label>
        <label>التاريخ<input type="date" required value={stockForm.movement_date} onChange={(e) => setStockForm({ ...stockForm, movement_date: e.target.value })} /></label>
        {(stockForm.movement_type === "issue" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "out")) && <label>مخزن المصدر<select required value={stockForm.source_warehouse} onChange={(e) => setStockForm({ ...stockForm, source_warehouse: e.target.value, source_location: "" })}>{warehouses.map((w) => <option key={w.id} value={w.id}>{w.name_ar}</option>)}</select></label>}
        {(stockForm.movement_type === "issue" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "out")) && <label>موقع الصرف<select value={stockForm.source_location} onChange={(e) => setStockForm({ ...stockForm, source_location: e.target.value })}><option value="">الموقع العام للمخزن</option>{locations.filter((x) => x.is_active && String(x.warehouse) === stockForm.source_warehouse).map((x) => <option key={x.id} value={x.id}>{x.name_ar}</option>)}</select></label>}
        {(stockForm.movement_type === "receipt" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "in")) && <label>مخزن الوجهة<select required value={stockForm.destination_warehouse} onChange={(e) => setStockForm({ ...stockForm, destination_warehouse: e.target.value, destination_location: "" })}>{warehouses.map((w) => <option key={w.id} value={w.id}>{w.name_ar}</option>)}</select></label>}
        {(stockForm.movement_type === "receipt" || stockForm.movement_type === "transfer" || (stockForm.movement_type === "adjustment" && stockForm.adjustment_direction === "in")) && <label>موقع الاستلام<select value={stockForm.destination_location} onChange={(e) => setStockForm({ ...stockForm, destination_location: e.target.value })}><option value="">الموقع العام للمخزن</option>{locations.filter((x) => x.is_active && String(x.warehouse) === stockForm.destination_warehouse).map((x) => <option key={x.id} value={x.id}>{x.name_ar}</option>)}</select></label>}
        {stockForm.movement_type === "adjustment" && <label>اتجاه التسوية<select value={stockForm.adjustment_direction} onChange={(e) => setStockForm({ ...stockForm, adjustment_direction: e.target.value, source_location: "", destination_location: "" })}><option value="in">زيادة جرد</option><option value="out">عجز جرد</option></select></label>}
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
    <div className="table-toolbar"><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="بحث سريع…" /><span className="module-count">{busy ? "جارٍ التحديث…" : "تحديث"}</span><button className="subtle-button" onClick={() => void refresh()}>↻</button></div>
    <div className="data-table-wrap">
      {tab === "products" && <table><thead><tr><th>الكود</th><th>الصنف</th><th>الباركود</th><th>النوع</th><th>الوحدة</th><th>سعر البيع</th><th>طريقة التقييم</th><th>الحالة</th></tr></thead><tbody>{filteredProducts.map((p) => <tr key={p.id}><td className="code-cell">{p.code}</td><td>{p.name_ar}</td><td>{p.barcode || "—"}</td><td>{p.product_type === "goods" ? "مخزني" : "خدمة"}</td><td>{p.unit_name}</td><td>{Number(p.sale_price).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{p.costing_method === "fifo" ? "FIFO" : "متوسط مرجح"}</td><td><span className={p.is_active ? "customer-status-active" : "customer-status-inactive"}>{p.is_active ? "نشط" : "موقوف"}</span></td></tr>)}</tbody></table>}
      {tab === "categories" && <table><thead><tr><th>الكود</th><th>اسم المجموعة</th><th>الحالة</th></tr></thead><tbody>{categories.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "units" && <table><thead><tr><th>الكود</th><th>اسم الوحدة</th><th>الرمز</th><th>الدقة</th><th>الحالة</th></tr></thead><tbody>{units.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.symbol || "—"}</td><td>{x.decimal_places}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "reorder" && <table><thead><tr><th>الصنف</th><th>المخزن</th><th>الرصيد الحالي</th><th>حد إعادة الطلب</th><th>المصدر</th><th>الحالة</th></tr></thead><tbody>{warehouseSettings.filter((item) => item.is_active && `${item.product_code} ${item.product_name} ${item.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((item) => {
        const quantity = balances.filter((balance) => balance.product === item.product && balance.warehouse === item.warehouse).reduce((sum, balance) => sum + Number(balance.quantity), 0);
        return <tr key={item.id}><td>{item.product_code} - {item.product_name}</td><td>{item.warehouse_name}</td><td>{quantity.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(item.min_stock).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>خاص بالمخزن</td><td><span className={quantity <= Number(item.min_stock) && Number(item.min_stock) > 0 ? "status-pill reversed" : "status-pill draft"}>{Number(item.min_stock) > 0 && quantity <= Number(item.min_stock) ? "يحتاج إعادة طلب" : "طبيعي"}</span></td></tr>;
      })}{!warehouseSettings.filter((item) => item.is_active).length && <tr><td colSpan={6}>لا توجد حدود مخصصة بعد. أضف حدًا لكل صنف ومخزن حسب الحاجة.</td></tr>}</tbody></table>}
      {tab === "warehouses" && <table><thead><tr><th>الكود</th><th>اسم المخزن</th><th>الفرع</th><th>العنوان</th><th>الحالة</th></tr></thead><tbody>{warehouses.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.branch_code || "عام"}</td><td>{x.address || "—"}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "locations" && <table><thead><tr><th>المخزن</th><th>الكود</th><th>اسم الموقع</th><th>النوع</th><th>الموقع الأب</th><th>الحالة</th></tr></thead><tbody>{locations.filter((x) => `${x.code} ${x.name_ar} ${x.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td>{x.warehouse_name}</td><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{{ floor: "دور", area: "منطقة", rack: "رف", bin: "خانة" }[x.location_type]}</td><td>{locations.find((parent) => parent.id === x.parent)?.name_ar || "—"}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "movements" && <table><thead><tr><th>رقم الحركة</th><th>النوع</th><th>التاريخ</th><th>من</th><th>إلى</th><th>الأصناف</th><th>قيمة الحركة</th><th>قيود اليومية</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{movements.filter((m) => `${m.movement_number} ${m.notes}`.toLowerCase().includes(search.toLowerCase())).map((m) => <tr key={m.id}><td className="code-cell">{m.movement_number}</td><td>{{ receipt: "استلام", issue: "صرف", transfer: "تحويل", adjustment: "تسوية" }[m.movement_type]}</td><td>{m.movement_date}</td><td>{m.source_warehouse_name || "—"}</td><td>{m.destination_warehouse_name || "—"}</td><td>{m.lines.length}</td><td>{m.lines.reduce((sum, line) => sum + Number(line.cost_amount || 0), 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{m.journal_entries.length ? m.journal_entries.map((entry) => entry.entry_number).join("، ") : "—"}</td><td>{m.status === "posted" ? "مرحّلة" : "مسودة"}</td><td>{m.status === "draft" ? <button className="subtle-button" disabled={busy} onClick={async () => { setBusy(true); setError(""); try { await stockApi.postMovement(m.id); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل الحركة."); } finally { setBusy(false); } }}>ترحيل</button> : "—"}</td></tr>)}</tbody></table>}
      {tab === "accounting" && <table><thead><tr><th>الدور المحاسبي</th><th>الحساب</th><th>الحالة</th></tr></thead><tbody>{[{ role: "inventory", label: "حساب المخزون" }, { role: "goods_received_not_invoiced", label: "بضاعة مستلمة غير مفوترة" }, { role: "cogs", label: "تكلفة المبيعات" }, { role: "inventory_adjustment_gain", label: "أرباح تسوية المخزون" }, { role: "inventory_adjustment_loss", label: "خسائر تسوية المخزون" }, { role: "inter_branch_clearing", label: "تسوية بين الفروع" }].map((item) => { const assigned = roleAssignments.find((x) => x.role === item.role); const account = accounts.find((x) => x.id === assigned?.account); return <tr key={item.role}><td>{item.label}</td><td>{assigned ? `${account?.code || assigned.account_code} - ${account?.name_ar || ""}` : "غير مربوط"}</td><td>{assigned ? "مربوط" : "مطلوب"}</td></tr>; })}</tbody></table>}
      {tab === "balances" && <table><thead><tr><th>كود الصنف</th><th>الصنف</th><th>المخزن</th><th>موقع التخزين</th><th>الصلاحية</th><th>الرصيد</th><th>متوسط التكلفة</th><th>قيمة المخزون</th></tr></thead><tbody>{balances.filter((b) => `${b.product_code} ${b.product_name} ${b.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((b) => <tr key={b.id}><td className="code-cell">{b.product_code}</td><td>{b.product_name}</td><td>{b.warehouse_name}</td><td>{b.location_name || "عام"}</td><td>{b.expiry_date || "—"}</td><td>{Number(b.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(b.average_cost).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(b.inventory_value).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td></tr>)}</tbody></table>}
      {tab === "reports" && <div className="inventory-reports">
        <div className="metrics-grid inventory-report-metrics">
          <article className="metric-card"><div className="metric-top">قيمة المخزون</div><strong className="metric-value">{totalInventoryValue.toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong><small>حسب تكلفة الأرصدة الحالية</small></article>
          <article className="metric-card"><div className="metric-top">إجمالي الكميات</div><strong className="metric-value">{totalInventoryQuantity.toLocaleString("ar-EG", { maximumFractionDigits: 3 })}</strong><small>مجموع أرصدة جميع المخازن</small></article>
          <article className="metric-card"><div className="metric-top">أصناف عند حد الطلب</div><strong className="metric-value">{reorderAlerts.length.toLocaleString("ar-EG")}</strong><small>وفق حد كل صنف داخل كل مخزن</small></article>
          <article className="metric-card"><div className="metric-top">تنبيهات الصلاحية</div><strong className="metric-value">{expiryAlerts.length.toLocaleString("ar-EG")}</strong><small>منتهية أو خلال 30 يوماً</small></article>
        </div>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>تقييم المخزون حسب المخزن</h3><p>قيمة وكمية الأرصدة الحالية</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>المخزن</th><th>عدد الأصناف</th><th>إجمالي الكمية</th><th>قيمة المخزون</th></tr></thead><tbody>{warehouseValuation.filter((row) => row.warehouse.name_ar.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.warehouse.id}><td>{row.warehouse.name_ar}</td><td>{row.products}</td><td>{row.quantity.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{row.value.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td></tr>)}{!warehouseValuation.length && <tr><td colSpan={4}>لا توجد أرصدة مخزنية حالياً.</td></tr>}</tbody></table></div>
        </section>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>أصناف وصلت إلى حد إعادة الطلب</h3><p>مقارنة رصيد كل صنف داخل كل مخزن بحده الخاص، مع استخدام الحد العام للصنف كقيمة افتراضية</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الصنف</th><th>المخزن</th><th>الرصيد الحالي</th><th>حد إعادة الطلب</th><th>قيمة المخزون</th></tr></thead><tbody>{reorderAlerts.filter((row) => `${row.product.code} ${row.product.name_ar} ${row.warehouse.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={`${row.product.id}-${row.warehouse.id}`}><td className="code-cell">{row.product.code}</td><td>{row.product.name_ar}</td><td>{row.warehouse.name_ar}</td><td>{row.quantity.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{row.threshold.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{row.value.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td></tr>)}{!reorderAlerts.length && <tr><td colSpan={6}>لا توجد أصناف عند حد إعادة الطلب.</td></tr>}</tbody></table></div>
        </section>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>تنبيهات الصلاحية</h3><p>الأصناف المنتهية أو التي تنتهي خلال 30 يوماً</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>المخزن</th><th>الموقع</th><th>تاريخ الصلاحية</th><th>الأيام المتبقية</th><th>الكمية</th><th>القيمة</th></tr></thead><tbody>{expiryAlerts.filter((row) => `${row.product_code} ${row.product_name} ${row.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.id}><td>{row.product_code} - {row.product_name}</td><td>{row.warehouse_name}</td><td>{row.location_name || "عام"}</td><td>{row.expiry_date}</td><td><span className={row.days < 0 ? "status-pill reversed" : "status-pill draft"}>{row.days < 0 ? `منتهي منذ ${Math.abs(row.days)} يوم` : row.days === 0 ? "ينتهي اليوم" : `${row.days} يوم`}</span></td><td>{Number(row.quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(row.inventory_value).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td></tr>)}{!expiryAlerts.length && <tr><td colSpan={7}>لا توجد تنبيهات صلاحية خلال الفترة المحددة.</td></tr>}</tbody></table></div>
        </section>
        <section className="activity-panel inventory-report-section"><div className="activity-title"><div><h3>مطابقة المخزون مع دفتر الأستاذ</h3><p>مقارنة قيمة الأرصدة الحالية برصيد حساب المخزون في القيود المرحلة</p></div><button type="button" className="primary-button" disabled={reconciling} onClick={() => void loadReconciliation()}>{reconciling ? "جارٍ الفحص…" : "تنفيذ المطابقة"}</button></div>
          {reconciliationError && <div className="error-banner">{reconciliationError}</div>}
          {reconciliation && <div className="reconciliation-result"><div><span>حساب المخزون</span><strong>{reconciliation.account_code} - {reconciliation.account_name}</strong></div><div><span>قيمة المخزون</span><strong>{Number(reconciliation.stock_value).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>رصيد دفتر الأستاذ</span><strong>{Number(reconciliation.gl_balance).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>الفرق</span><strong>{Number(reconciliation.difference).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div className={reconciliation.is_reconciled ? "reconciliation-status matched" : "reconciliation-status mismatch"}>{reconciliation.is_reconciled ? "المخزون مطابق لدفتر الأستاذ" : "يوجد فرق يحتاج إلى مراجعة"}</div></div>}
          {!reconciliation && !reconciliationError && <div className="table-empty">نفّذ المطابقة لعرض نتيجة المقارنة.</div>}
        </section>
        <p className="inventory-report-note">حد إعادة الطلب يُحدد لكل مخزن وصنف، ويُستخدم الحد العام للصنف كقيمة افتراضية عند غياب الإعداد الخاص. المطابقة تقارن لقطة الأرصدة الحالية برصيد حساب المخزون في القيود المرحلة؛ ولا تمثل مطابقة تاريخية لفترة سابقة.</p>
      </div>}
    </div>
  </section>;
}
