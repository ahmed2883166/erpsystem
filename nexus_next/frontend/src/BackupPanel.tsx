import { useEffect, useState, type FormEvent } from "react";

type ResetPreview = { counts: Record<string,number>; total_records:number; backup_ready:boolean; last_backup_at:string|null; preserved:string[]; removed:string[] };
type BackupConfig = {
  backup_path: string;
  schedule: "daily" | "weekly" | "monthly";
  enabled: boolean;
  daily_retention_days: number;
  monthly_retention_months: number;
  updated_at: string;
  can_manage: boolean;
  path_configured: boolean;
  history: {id:number;status:"success"|"failure";backup_file:string;size_bytes:number;details:string;created_at:string}[];
};

async function call<T>(method = "GET", body?: unknown, endpoint = "/api/backup/configuration/"): Promise<T> {
  const csrf = document.cookie.split("; ").find(x => x.startsWith("csrftoken="))?.split("=").slice(1).join("=") || "";
  const response = await fetch(endpoint, {
    method, credentials: "same-origin",
    headers: { Accept: "application/json", ...(body ? {"Content-Type":"application/json"} : {}), ...(method !== "GET" ? {"X-CSRFToken":decodeURIComponent(csrf)} : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.backup_path || "تعذر تنفيذ الطلب.");
  return data as T;
}

const resetLabels: Record<string,string> = {
  "approvals.SalesInvoiceApproval":"موافقات فواتير المبيعات",
  "approvals.FinancialApprovalRequest":"طلبات الموافقة المالية",
  "treasury.BankStatementLine":"سطور كشوف البنوك",
  "purchasing.SupplierPayment":"مدفوعات الموردين",
  "sales.CustomerReceipt":"تحصيلات العملاء",
  "purchasing.PurchaseInvoiceLine":"تفاصيل فواتير المشتريات",
  "sales.SalesInvoiceLine":"تفاصيل فواتير المبيعات",
  "purchasing.PurchaseOrderReceiptLine":"روابط استلام أوامر الشراء",
  "inventory.StockMovementJournalLink":"روابط حركات المخزون بالقيود",
  "inventory.StockCostLayer":"طبقات تكلفة المخزون",
  "inventory.StockBalance":"أرصدة المخزون",
  "assets.AssetDepreciation":"إهلاكات الأصول",
  "hr.PayrollResultLine":"تفاصيل نتائج المرتبات",
  "hr.PayrollResult":"نتائج المرتبات",
  "hr.PayrollPosting":"ترحيلات المرتبات",
  "hr.LoanInstallment":"أقساط سلف الموظفين",
  "hr.AttendanceRecord":"سجلات الحضور والانصراف",
  "hr.LeaveRequest":"طلبات الإجازات",
  "hr.EmployeeSalaryHistory":"تاريخ تغييرات الرواتب",
  "hr.EmployeeLoan":"سلف الموظفين",
  "hr.PayrollCycle":"دورات المرتبات",
  "purchasing.PurchaseInvoice":"فواتير المشتريات",
  "sales.SalesInvoice":"فواتير المبيعات",
  "purchasing.PurchaseReceipt":"استلامات المشتريات",
  "sales.SalesDelivery":"تسليمات المبيعات",
  "purchasing.PurchaseOrderLine":"تفاصيل أوامر الشراء",
  "purchasing.PurchaseOrder":"أوامر الشراء",
  "treasury.BankReconciliation":"التسويات البنكية",
  "assets.FixedAsset":"سجل الأصول الثابتة",
  "treasury.TreasuryTransaction":"حركات الخزينة والبنوك",
  "inventory.StockMovementLine":"تفاصيل حركات المخزون",
  "inventory.StockMovement":"حركات المخزون",
  "accounting.JournalLine":"تفاصيل قيود اليومية",
  "accounting.JournalEntry":"قيود اليومية",
};

export default function BackupPanel() {
  const [config, setConfig] = useState<BackupConfig | null>(null);
  const [path, setPath] = useState("");
  const [schedule, setSchedule] = useState<BackupConfig["schedule"]>("daily");
  const [enabled, setEnabled] = useState(true);
  const [daily, setDaily] = useState(30);
  const [monthly, setMonthly] = useState(12);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [running, setRunning] = useState(false);
  const [resetPreview, setResetPreview] = useState<ResetPreview | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [resetPhrase, setResetPhrase] = useState("");
  const [resetBusy, setResetBusy] = useState(false);

  const load = async () => {
    try {
      const value = await call<BackupConfig>();
      setConfig(value); setPath(value.backup_path); setSchedule(value.schedule);
      setEnabled(value.enabled); setDaily(value.daily_retention_days); setMonthly(value.monthly_retention_months);
      if (value.can_manage) setResetPreview(await call<ResetPreview>("GET", undefined, "/api/backup/reset-preview/"));
    } catch (e) { setError(e instanceof Error ? e.message : "تعذر تحميل إعدادات النسخ."); }
  };
  useEffect(() => { void load(); }, []);

  const runBackup = async () => {
    if (!window.confirm("بدء نسخة احتياطية الآن في المسار المعتمد؟")) return;
    setRunning(true); setError(""); setNotice("");
    try {
      const result = await call<{detail:string}>("POST");
      setNotice(result.detail);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "فشل تنفيذ النسخ الاحتياطي."); }
    finally { setRunning(false); }
  };

  const executeReset = async (event: FormEvent) => {
    event.preventDefault();
    if (!window.confirm("تحذير نهائي: سيتم حذف كل الحركات التشغيلية من جميع الشركات. هل أنشأت وراجعت النسخة الاحتياطية؟")) return;
    setResetBusy(true); setError(""); setNotice("");
    try {
      const result = await call<{detail:string}>("POST", {password:resetPassword, confirmation:resetPhrase}, "/api/backup/reset/");
      setNotice(result.detail); setResetPassword(""); setResetPhrase(""); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "تعذر تصفير الحركات."); }
    finally { setResetBusy(false); }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setNotice("");
    try {
      const value = await call<BackupConfig>("PATCH", {
        backup_path: path, schedule, enabled,
        daily_retention_days: daily, monthly_retention_months: monthly,
      });
      setConfig(value); setNotice("تم حفظ إعدادات النسخ. التغيير متاح لمدير النظام فقط.");
    } catch (e) { setError(e instanceof Error ? e.message : "تعذر حفظ الإعدادات."); }
    finally { setBusy(false); }
  };

  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>النسخ الاحتياطي وحماية البيانات</h2><p>تحديد مكان النسخ وسياسة الاحتفاظ بالنسخ الدورية</p></div><span className="coming-soon">{config?.path_configured ? "المسار محدد" : "المسار غير محدد"}</span></div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    {!config ? <div className="table-empty">جارٍ تحميل الإعدادات…</div> : <>
      <div className="review-meta">
        <div><span>حالة النسخ التلقائي</span><strong>{config.enabled ? "مفعّل" : "متوقف"}</strong></div>
        <div><span>مسار النسخ المعتمد</span><strong dir="ltr">{config.backup_path || "لم يتم تحديده"}</strong></div>
        <div><span>صلاحية التعديل</span><strong>{config.can_manage ? "مدير النظام فقط" : "عرض فقط"}</strong></div>
      </div>
      {config.can_manage && <div className="form-actions"><button type="button" className="primary-button" disabled={running || !config.enabled || !config.path_configured} onClick={() => void runBackup()}>{running ? "جارٍ إنشاء النسخة والتحقق منها…" : "إنشاء نسخة احتياطية الآن"}</button></div>}
      {config.can_manage ? <form className="journal-form" onSubmit={save}>
        <label className="form-wide">مجلد النسخ الاحتياطي على قرص غير C
          <input required dir="ltr" value={path} onChange={e=>setPath(e.target.value)} placeholder="D:\\NEXUS_Backups" />
        </label>
        <p className="asset-report-note form-wide">أنشئ المجلد مسبقًا على جهاز تشغيل النظام، وتأكد من أن حساب تشغيل NEXUS يستطيع الكتابة فيه. لا يُقبل مسار على C:.</p>
        <label>الجدولة<select value={schedule} onChange={e=>setSchedule(e.target.value as BackupConfig["schedule"])}><option value="daily">يومي</option><option value="weekly">أسبوعي</option><option value="monthly">شهري</option></select></label>
        <label>الاحتفاظ بالنسخ اليومية (أيام)<input type="number" min="1" max="3650" value={daily} onChange={e=>setDaily(Number(e.target.value))}/></label>
        <label>الاحتفاظ بالنسخ الشهرية (شهور)<input type="number" min="1" max="120" value={monthly} onChange={e=>setMonthly(Number(e.target.value))}/></label>
        <label className="form-wide"><span><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)}/> تفعيل النسخ الاحتياطي المجدول</span></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ إعدادات النسخ"}</button></div>
      </form> : <p className="asset-report-note">يمكنك مشاهدة الإعدادات فقط. تغيير مسار النسخ أو الجدولة محجوز لمدير النظام الأعلى (Superuser).</p>}
      <div className="work-panel-head"><div><h3>سجل النسخ الاحتياطية</h3><p>آخر 50 عملية، مع نتيجة التحقق وحجم الملف ومكان حفظه</p></div></div>
      <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>النتيجة</th><th>الحجم</th><th>مكان الحفظ</th></tr></thead><tbody>
        {config.history.map(run=><tr key={run.id}><td>{new Date(run.created_at).toLocaleString("ar-EG")}</td><td>{run.status === "success" ? "ناجح ومتحقق" : "فشل"}</td><td>{run.size_bytes ? (run.size_bytes / (1024*1024)).toFixed(2) + " MB" : "—"}</td><td dir="ltr">{run.backup_file || run.details}</td></tr>)}
        {!config.history.length && <tr><td colSpan={4}>لا توجد عمليات نسخ مسجلة بعد.</td></tr>}
      </tbody></table></div>
      <p className="asset-report-note">لا نعرض نسبة تقدم وهمية؛ أثناء التنفيذ تظهر حالة التشغيل، ولا تُسجل النسخة ناجحة إلا بعد التحقق من الأرشيف ووجود الملف بالحجم المتوقع. بعد تغيير الجدولة، أعد تشغيل scripts/install-backup-task.ps1 لتحديث مهمة Windows.</p>
      {config.can_manage && <section className="work-panel reset-panel">
        <div className="work-panel-head"><div><h3>تصفير الحركات التشغيلية</h3><p>إجراء نهائي لجميع الشركات؛ يحتفظ بالبيانات الأساسية وإعدادات النظام وسجل التدقيق.</p></div><span className="coming-soon">{resetPreview?.backup_ready ? "النسخة الاحتياطية جاهزة" : "يلزم نسخ احتياطي حديث"}</span></div>
        {resetPreview && <>
          <div className="review-meta"><div><span>السجلات المحددة للتصفير</span><strong>{resetPreview.total_records.toLocaleString("ar-EG")}</strong></div><div><span>آخر نسخة ناجحة</span><strong>{resetPreview.last_backup_at ? new Date(resetPreview.last_backup_at).toLocaleString("ar-EG") : "لا توجد"}</strong></div></div>
          <h4>ما سيتم الاحتفاظ به</h4><ul>{resetPreview.preserved.map(x=><li key={x}>{x}</li>)}</ul>
          <h4>ما سيتم تصفيره</h4><div className="data-table-wrap"><table><thead><tr><th>نوع السجلات</th><th>العدد</th></tr></thead><tbody>{Object.entries(resetPreview.counts).map(([name,count])=><tr key={name}><td>{resetLabels[name] || name}</td><td>{count.toLocaleString("ar-EG")}</td></tr>)}</tbody></table></div>
          <form className="journal-form" onSubmit={executeReset}>
            <label>كلمة مرور مدير النظام<input type="password" autoComplete="current-password" required value={resetPassword} onChange={e=>setResetPassword(e.target.value)}/></label>
            <label>اكتب عبارة التأكيد حرفيًا<input required value={resetPhrase} onChange={e=>setResetPhrase(e.target.value)} placeholder="تصفير الحركات"/></label>
            <div className="form-actions form-wide"><button className="danger-button" type="submit" disabled={resetBusy || !resetPreview.backup_ready || resetPhrase !== "تصفير الحركات"}>{resetBusy ? "جارٍ التصفير…" : "تصفير الحركات التشغيلية"}</button></div>
          </form>
          {!resetPreview.backup_ready && <p className="asset-report-note">زر التصفير سيظل معطلًا حتى إنشاء نسخة ناجحة والتحقق من وجود ملفها خلال آخر 24 ساعة.</p>}
        </>}
      </section>}
    </>}
  </section>;
}
