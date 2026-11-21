/*
 * مركز مراقبة النظام: عرض مؤشرات أداء الطلبات وقاعدة PostgreSQL والانتقال لسجل التدقيق.
 * تعرض القياسات المتاحة من الخادم مع توضيح حدود البيانات التي يوفرها.
 */
import { lazy, Suspense, useEffect, useState } from "react";

const AuditPanel = lazy(() => import("./AuditPanel"));
type Metrics = {
  scope: string; sample_size: number; average_duration_ms: number; median_duration_ms: number;
  p95_duration_ms: number; error_count: number; slow_request_count: number;
  average_db_queries: number; total_db_duration_ms: number;
  system: { available: boolean; cpu_percent?: number; memory_percent?: number; memory_total_mb?: number; memory_available_mb?: number };
  postgresql: { available: boolean; reason?: string; database_connections?: number; active_connections?: number; idle_connections?: number; max_connections?: number; database_size_mb?: number; connection_usage_percent?: number };
  recent: { route: string; method: string; status_code: number; duration_ms: number; db_queries: number; db_duration_ms: number }[];
};
const number = (value?: number) => value === undefined ? "—" : Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });

// شاشة مراقبة الأداء ومؤشرات قاعدة البيانات مع تبويب سجل التدقيق.
export default function SystemMonitorPanel({ companyId }: { companyId: number | null }) {
  const [tab, setTab] = useState<"performance" | "audit">("performance");
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // جلب آخر قياسات الأداء من الخادم عند فتح تبويب المراقبة.
const load = async () => {
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/system/performance/", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(response.status === 403 ? "مراقبة الأداء متاحة لمدير النظام فقط." : `تعذر تحميل مؤشرات الأداء (HTTP ${response.status}).`);
      setMetrics(await response.json() as Metrics);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل مؤشرات الأداء."); }
    finally { setBusy(false); }
  };
  // تحديث مؤشرات الأداء عند الانتقال إلى تبويب المراقبة.
useEffect(() => { if (tab === "performance") void load(); }, [tab]);
  return <section className="work-panel system-monitor">
    <div className="work-panel-head"><div><h2>مركز مراقبة النظام</h2><p>مراقبة زمن الاستجابة واستهلاك الجهاز واتصالات PostgreSQL مع سجل التدقيق المنفصل</p></div>
      {tab === "performance" && <button className="subtle-button" type="button" onClick={() => void load()} disabled={busy}>تحديث</button>}
    </div>
    <div className="monitor-tabs"><button className={tab === "performance" ? "active" : ""} onClick={() => setTab("performance")}>مراقبة الأداء</button><button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>سجل المراجعة والتدقيق</button></div>
    {tab === "audit" ? <Suspense fallback={<div className="table-empty">جارٍ تحميل سجل المراجعة…</div>}><AuditPanel companyId={companyId} /></Suspense> : <>
      {error && <div className="error-banner">{error}</div>}
      {metrics && <>
        <div className="metrics-grid monitor-kpis">
          <article className="metric-card"><div className="metric-top">الطلبات المقاسة</div><strong className="metric-value">{number(metrics.sample_size)}</strong><small>آخر عينة محفوظة في الذاكرة</small></article>
          <article className="metric-card"><div className="metric-top">متوسط الاستجابة</div><strong className="metric-value">{number(metrics.average_duration_ms)} ms</strong><small>لكل الطلبات المسجلة</small></article>
          <article className="metric-card"><div className="metric-top">P95 للاستجابة</div><strong className="metric-value">{number(metrics.p95_duration_ms)} ms</strong><small>95% من الطلبات أسرع من هذه القيمة</small></article>
          <article className="metric-card"><div className="metric-top">أخطاء الخادم</div><strong className="metric-value">{number(metrics.error_count)}</strong><small>HTTP 500 فأعلى في العينة</small></article>
          <article className="metric-card"><div className="metric-top">الطلبات البطيئة</div><strong className="metric-value">{number(metrics.slow_request_count)}</strong><small>أكثر من ثانية</small></article>
          <article className="metric-card"><div className="metric-top">متوسط استعلامات DB</div><strong className="metric-value">{number(metrics.average_db_queries)}</strong><small>استعلام لكل طلب</small></article>
          <article className="metric-card"><div className="metric-top">استهلاك المعالج CPU</div><strong className="metric-value">{metrics.system.available ? `${number(metrics.system.cpu_percent)}%` : "غير متاح"}</strong><small>استهلاك الجهاز وقت القياس</small></article>
          <article className="metric-card"><div className="metric-top">استهلاك الذاكرة RAM</div><strong className="metric-value">{metrics.system.available ? `${number(metrics.system.memory_percent)}%` : "غير متاح"}</strong><small>{metrics.system.available ? `متاح ${number(metrics.system.memory_available_mb)} من ${number(metrics.system.memory_total_mb)} MB` : "تعذر قراءة موارد الجهاز"}</small></article>
          <article className="metric-card"><div className="metric-top">اتصالات PostgreSQL</div><strong className="metric-value">{metrics.postgresql.available ? `${number(metrics.postgresql.database_connections)} / ${number(metrics.postgresql.max_connections)}` : "غير متاح"}</strong><small>{metrics.postgresql.available ? `${number(metrics.postgresql.active_connections)} نشط · ${number(metrics.postgresql.idle_connections)} خامل` : "تعذر قراءة إحصاءات قاعدة البيانات"}</small></article>
        </div>
        {metrics.postgresql.available && <div className="finance-balance-strip"><div><span>حجم قاعدة البيانات</span><strong>{number(metrics.postgresql.database_size_mb)} MB</strong></div><div><span>نسبة استخدام الاتصالات</span><strong>{number(metrics.postgresql.connection_usage_percent)}%</strong></div><div><span>إجمالي زمن استعلامات DB</span><strong>{number(metrics.total_db_duration_ms)} ms</strong></div></div>}
        <div className="data-table-wrap"><table><thead><tr><th>المسار</th><th>الطريقة</th><th>HTTP</th><th>الاستجابة ms</th><th>استعلامات DB</th><th>زمن DB ms</th></tr></thead><tbody>
          {metrics.recent.map((item, index) => <tr key={index}><td dir="ltr">{item.route}</td><td>{item.method}</td><td>{item.status_code}</td><td>{number(item.duration_ms)}</td><td>{item.db_queries}</td><td>{number(item.db_duration_ms)}</td></tr>)}
          {!metrics.recent.length && <tr><td colSpan={6}>ستظهر القياسات بعد تنفيذ طلبات على النظام.</td></tr>}
        </tbody></table></div>
        <p className="asset-report-note">قياسات الطلبات محفوظة في ذاكرة عملية التطبيق الحالية (آخر 250 طلبًا) وتُمسح عند إعادة تشغيلها، وقد تختلف بين العمال. CPU وRAM يعكسان الجهاز المضيف. إحصاءات PostgreSQL تخص قاعدة البيانات الحالية وقد تخضع لصلاحيات مستخدم قاعدة البيانات. لا يتم حفظ نصوص SQL أو محتوى الطلبات أو كلمات المرور. العرض متاح لمدير النظام فقط.</p>
      </>}
    </>}
  </section>;
}
