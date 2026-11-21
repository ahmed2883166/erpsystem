import { useEffect, useMemo, useState } from "react";
import { inventoryApi, type InventoryCategory, type InventoryLocation, type InventoryUnit, type Product, type Warehouse } from "./api";

type Tab = "products" | "categories" | "warehouses" | "units" | "locations";
export default function InventoryPanel({ companyId }: { companyId: number | null }) {
  const [tab, setTab] = useState<Tab>("products");
  const [products, setProducts] = useState<Product[]>([]);
  const [categories, setCategories] = useState<InventoryCategory[]>([]);
  const [units, setUnits] = useState<InventoryUnit[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [locations, setLocations] = useState<InventoryLocation[]>([]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ code: "", name_ar: "", barcode: "", unit: "", category: "", product_type: "goods", purchase_price: "0", sale_price: "0", min_stock: "0", costing_method: "weighted_average", track_expiry: false });
  const [unitForm, setUnitForm] = useState({ code: "", name_ar: "", symbol: "", decimal_places: "2" });
  const [categoryForm, setCategoryForm] = useState({ code: "", name_ar: "" });
  const [warehouseForm, setWarehouseForm] = useState({ code: "", name_ar: "", branch: "", address: "" });
  const [locationForm, setLocationForm] = useState({ warehouse: "", code: "", name_ar: "", location_type: "area", parent: "" });

  const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [p, c, u, w, l] = await Promise.all([inventoryApi.products(companyId), inventoryApi.categories(companyId), inventoryApi.units(companyId), inventoryApi.warehouses(companyId), inventoryApi.locations(companyId)]);
      setProducts(p); setCategories(c); setUnits(u); setWarehouses(w); setLocations(l);
      setLocationForm((old) => ({ ...old, warehouse: old.warehouse || String(w[0]?.id || "") }));
      setForm((old) => ({ ...old, unit: old.unit || String(u[0]?.id || ""), category: old.category || String(c[0]?.id || "") }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المخزون."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);
  const filteredProducts = useMemo(() => products.filter((p) => `${p.code} ${p.barcode} ${p.name_ar}`.toLowerCase().includes(search.toLowerCase())), [products, search]);
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
      } else {
        if (!locationForm.warehouse) throw new Error("أضف مخزناً أولاً.");
        await inventoryApi.createLocation({ warehouse: Number(locationForm.warehouse), code: locationForm.code, name_ar: locationForm.name_ar, location_type: locationForm.location_type, parent: locationForm.parent ? Number(locationForm.parent) : null });
        setLocationForm((old) => ({ ...old, code: "", name_ar: "", parent: "" }));
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
  ];
  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر شركة لعرض بيانات المخزون.</div></section>;
  return <section className="work-panel inventory-panel">
    <div className="work-panel-head"><div><h2>إدارة المخزون والمستودعات</h2><p>الأصناف والمجموعات ووحدات القياس والمخازن للشركة المحددة.</p></div><button className="primary-button" onClick={() => setShowForm((v) => !v)}>{showForm ? "إغلاق النموذج" : "＋ إضافة جديد"}</button></div>
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
      {tab === "warehouses" && <><label>كود المخزن<input required value={warehouseForm.code} onChange={(e) => setWarehouseForm({ ...warehouseForm, code: e.target.value })} /></label><label>اسم المخزن<input required value={warehouseForm.name_ar} onChange={(e) => setWarehouseForm({ ...warehouseForm, name_ar: e.target.value })} /></label><label className="inventory-wide">العنوان<textarea value={warehouseForm.address} onChange={(e) => setWarehouseForm({ ...warehouseForm, address: e.target.value })} /></label></>}
      {tab === "locations" && <><label>المخزن<select required value={locationForm.warehouse} onChange={(e) => setLocationForm({ ...locationForm, warehouse: e.target.value, parent: "" })}><option value="">اختر المخزن</option>{warehouses.map((w) => <option key={w.id} value={w.id}>{w.name_ar}</option>)}</select></label><label>نوع الموقع<select value={locationForm.location_type} onChange={(e) => setLocationForm({ ...locationForm, location_type: e.target.value })}><option value="floor">دور</option><option value="area">منطقة</option><option value="rack">رف</option><option value="bin">خانة</option></select></label><label>الكود<input required value={locationForm.code} onChange={(e) => setLocationForm({ ...locationForm, code: e.target.value })} /></label><label>اسم الموقع<input required value={locationForm.name_ar} onChange={(e) => setLocationForm({ ...locationForm, name_ar: e.target.value })} /></label><label>الموقع الأب<select value={locationForm.parent} onChange={(e) => setLocationForm({ ...locationForm, parent: e.target.value })}><option value="">بدون موقع أب</option>{locations.filter((x) => String(x.warehouse) === locationForm.warehouse).map((x) => <option key={x.id} value={x.id}>{x.name_ar} ({x.code})</option>)}</select></label></>}
      <div className="inventory-actions"><button className="primary-button" disabled={busy} type="submit">{busy ? "جارٍ الحفظ…" : "حفظ"}</button><button className="subtle-button" type="button" onClick={() => setShowForm(false)}>إلغاء</button></div>
    </form>}
    <div className="table-toolbar"><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="بحث سريع…" /><span className="module-count">{busy ? "جارٍ التحديث…" : "تحديث"}</span><button className="subtle-button" onClick={() => void refresh()}>↻</button></div>
    <div className="data-table-wrap">
      {tab === "products" && <table><thead><tr><th>الكود</th><th>الصنف</th><th>الباركود</th><th>النوع</th><th>الوحدة</th><th>سعر البيع</th><th>طريقة التقييم</th><th>الحالة</th></tr></thead><tbody>{filteredProducts.map((p) => <tr key={p.id}><td className="code-cell">{p.code}</td><td>{p.name_ar}</td><td>{p.barcode || "—"}</td><td>{p.product_type === "goods" ? "مخزني" : "خدمة"}</td><td>{p.unit_name}</td><td>{Number(p.sale_price).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{p.costing_method === "fifo" ? "FIFO" : "متوسط مرجح"}</td><td><span className={p.is_active ? "customer-status-active" : "customer-status-inactive"}>{p.is_active ? "نشط" : "موقوف"}</span></td></tr>)}</tbody></table>}
      {tab === "categories" && <table><thead><tr><th>الكود</th><th>اسم المجموعة</th><th>الحالة</th></tr></thead><tbody>{categories.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "units" && <table><thead><tr><th>الكود</th><th>اسم الوحدة</th><th>الرمز</th><th>الدقة</th><th>الحالة</th></tr></thead><tbody>{units.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.symbol || "—"}</td><td>{x.decimal_places}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "warehouses" && <table><thead><tr><th>الكود</th><th>اسم المخزن</th><th>الفرع</th><th>العنوان</th><th>الحالة</th></tr></thead><tbody>{warehouses.filter((x) => `${x.code} ${x.name_ar}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.branch_code || "عام"}</td><td>{x.address || "—"}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
      {tab === "locations" && <table><thead><tr><th>المخزن</th><th>الكود</th><th>اسم الموقع</th><th>النوع</th><th>الموقع الأب</th><th>الحالة</th></tr></thead><tbody>{locations.filter((x) => `${x.code} ${x.name_ar} ${x.warehouse_name}`.toLowerCase().includes(search.toLowerCase())).map((x) => <tr key={x.id}><td>{x.warehouse_name}</td><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{{ floor: "دور", area: "منطقة", rack: "رف", bin: "خانة" }[x.location_type]}</td><td>{locations.find((parent) => parent.id === x.parent)?.name_ar || "—"}</td><td>{x.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table>}
    </div>
  </section>;
}
