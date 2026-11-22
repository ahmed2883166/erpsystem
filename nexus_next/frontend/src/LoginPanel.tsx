/*
 * شاشة دخول Admrail ERP v2: جمع بيانات المستخدم وإرسال طلب المصادقة مع CSRF.
 * بعد نجاح تسجيل الدخول تُبلغ التطبيق الرئيسي لعرض وحدات الشركة المصرح بها.
 */
import { useState, type FormEvent } from "react";
import { authApi } from "./api";

type Props = { onLogin: () => void };

export default function LoginPanel({ onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // التحقق من بيانات الدخول وفتح جلسة آمنة باستخدام حماية CSRF.
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = await authApi.login(username.trim(), password);
      if (!result.authenticated) throw new Error("تعذر إنشاء جلسة الدخول.");
      setPassword("");
      onLogin();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تسجيل الدخول.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="auth-screen" dir="rtl">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand-mark">N</div>
        <p className="auth-eyebrow">Admrail ERP v2</p>
        <h1>تسجيل الدخول</h1>
        <p className="auth-description">أدخل بيانات حسابك للوصول إلى نظام إدارة الشركة.</p>
        <label>
          اسم المستخدم
          <input
            autoComplete="username"
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          كلمة المرور
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={busy}
          />
        </label>
        {error && <div className="error-banner" role="alert">{error}</div>}
        <button className="primary-button auth-submit" type="submit" disabled={busy || !username || !password}>
          {busy ? "جارٍ التحقق…" : "دخول إلى النظام"}
        </button>
      </form>
    </main>
  );
}
