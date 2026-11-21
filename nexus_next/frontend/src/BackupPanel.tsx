import { useEffect, useState, type FormEvent } from "react";

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

async function call<T>(method = "GET", body?: unknown): Promise<T> {
  const csrf = document.cookie.split("; ").find(x => x.startsWith("csrftoken="))?.split("=").slice(1).join("=") || "";
  const response = await fetch("/api/backup/configuration/", {
    method, credentials: "same-origin",
    headers: { Accept: "application/json", ...(body ? {"Content-Type":"application/json"} : {}), ...(method !== "GET" ? {"X-CSRFToken":decodeURIComponent(csrf)} : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.backup_path || "تعذر تنفيذ الطلب.");
  return data as T;
}

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

  const load = async () => {
    try {
      const value = await call<BackupConfig>();
      setConfig(value); setPath(value.backup_path); setSchedule(value.schedule);
      setEnabled(value.enabled); setDaily(value.daily_retention_days); setMonthly(value.monthly_retention_months);
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
    </>}
  </section>;
}
