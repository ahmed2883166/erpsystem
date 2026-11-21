import { lazy, Suspense, useEffect, useState } from "react";

const AuditPanel = lazy(() => import("./AuditPanel"));
type Metrics = {
  scope: string; sample_size: number; average_duration_ms: number; median_duration_ms: number;
  p95_duration_ms: number; error_count: number; slow_request_count: number;
  average_db_queries: number; total_db_duration_ms: number;
  recent: { route: string; method: string; status_code: number; duration_ms: number; db_queries: number; db_duration_ms: number }[];
};
const number = (value: number) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });

export default function SystemMonitorPanel({ companyId }: { companyId: number | null }) {
  const [tab, setTab] = useState<"performance" | "audit">("performance");
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/system/performance/", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(response.status === 403 ? "مراقبة الأداء متاحة لمدير النظام فقط." : `تعذر تحميل مؤشرات الأداء (HTTP ${response.status}).`);
      setMetrics(await response.json() as Metrics);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل مؤشرات الأداء."); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (tab === "performance") void load(); }, [tab]);
  return <section className="work-panel system-monitor">
    <div className="work-panel-head"><div><h2>مركز مراقبة النظام</h2><p>مراقبة زمن الاستجابة واستعلامات قاعدة البيانات مع سجل التدقيق المنفصل</p></div>
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
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>المسار</th><th>الطريقة</th><th>HTTP</th><th>الاستجابة ms</th><th>استعلامات DB</th><th>زمن DB ms</th></tr></thead><tbody>
          {metrics.recent.map((item, index) => <tr key={index}><td dir="ltr">{item.route}</td><td>{item.method}</td><td>{item.status_code}</td><td>{number(item.duration_ms)}</td><td>{item.db_queries}</td><td>{number(item.db_duration_ms)}</td></tr>)}
          {!metrics.recent.length && <tr><td colSpan={6}>ستظهر القياسات بعد تنفيذ طلبات على النظام.</td></tr>}
        </tbody></table></div>
        <p className="asset-report-note">القياسات داخل ذاكرة عملية التطبيق الحالية فقط، وتُمسح عند إعادة تشغيلها، وقد تختلف بين العمال عند تشغيل عدة workers. لا يتم حفظ نصوص SQL أو محتوى الطلبات أو كلمات المرور. العرض متاح لمدير النظام فقط. مراقبة CPU/RAM واتصالات PostgreSQL التفصيلية تحتاج تكاملًا تشغيليًا إضافيًا.</p>
      </>}
    </>}
  </section>;
}
