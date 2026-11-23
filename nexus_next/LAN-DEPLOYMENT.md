# Admrail ERP v2 — Windows LAN deployment

هذا دليل تشغيل شبكة داخلية خاصة. التطبيق لا يحتاج إلى الإنترنت بعد تجهيز Windows وPostgreSQL وملفات التشغيل، ولا يفتح منفذ Django/Waitress مباشرة على الشبكة.

## البنية

- PostgreSQL 16 على جهاز الخادم.
- Django/Waitress مربوط على `127.0.0.1:8000` فقط.
- Caddy يستقبل HTTPS على TCP 443 ويحوّل الطلبات محليًا إلى Waitress.
- أجهزة الموظفين تتصل بعنوان HTTPS داخلي ثابت.
- لا تستخدم `runserver` في الإنتاج ولا تفتح المنفذ 8000 على الشبكة.

## التجهيز مرة واحدة

1. خصص جهاز Windows 10/11 Pro ثابتًا للخادم، واحجز له عنوان IP ثابتًا من الراوتر أو DHCP reservation.
2. جهّز PostgreSQL 16 وقاعدة بيانات ومستخدمًا مخصصين للتطبيق. لا تستخدم حساب postgres اليومي للتطبيق.
3. ثبّت Caddy على جهاز الخادم، واجعل `caddy.exe` متاحًا في PATH. يمكن تجهيز ملف Caddy التنفيذي مسبقًا ونقله داخل الشبكة؛ لا يحتاج Caddy إلى اتصال إنترنت عند التشغيل بهذا الإعداد.
4. اختر اسم DNS داخليًا مثل `erp.company.lan`، واجعل DNS الداخلي أو ملف hosts على أجهزة الموظفين يربطه بعنوان IP الخادم.
5. انسخ `backend/.env.example` إلى `backend/.env`، ثم ضع:
   - `ADMRAIL_LAN_HOST=erp.company.lan`
   - `DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,erp.company.lan`
   - `CSRF_TRUSTED_ORIGINS=https://erp.company.lan`
   - `SESSION_COOKIE_SECURE=True`
   - `CSRF_COOKIE_SECURE=True`
   - `SECURE_SSL_REDIRECT=True`
   - `SECURE_HSTS_SECONDS=31536000`
   - `SECURE_HSTS_INCLUDE_SUBDOMAINS=True`
   - `DJANGO_HTTPS_TERMINATED_BY_PROXY=True`
   - `DJANGO_ENV=production` و`DJANGO_DEBUG=False`
   - كلمة مرور PostgreSQL قوية ومفتاح Django عشوائي طويل.
6. تأكد أن ملف تعريف الشبكة في Windows هو Private، وليس Public.

## تشغيل الإنتاج على الشبكة

افتح PowerShell كمسؤول داخل مجلد المشروع، ثم:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start-production.ps1 -EnableLan -ConfigureFirewall
```

هذا المشغل:
- يتحقق من إعدادات الإنتاج الآمنة قبل ترحيل قاعدة البيانات.
- ينشئ نسخة PostgreSQL احتياطية قبل أي migrations معلقة.
- يبني React ويطبق migrations ويجمع الملفات الثابتة.
- يشغل Caddy بشهادة داخلية من نوع TLS.
- يترك Waitress مربوطًا على localhost فقط.
- يفتح TCP 443 على ملف تعريف Private فقط عند تمرير `-ConfigureFirewall`.

إذا لم تستخدم `-ConfigureFirewall`، افتح TCP 443 يدويًا على ملف تعريف Private فقط. لا تفتح 8000 أو 5432 لأجهزة الموظفين؛ يجب أن يبقى PostgreSQL متاحًا محليًا للخادم فقط إلا إذا كان هناك تصميم قاعدة بيانات منفصل ومدروس.

## تثبيت شهادة HTTPS على أجهزة الموظفين

Caddy ينشئ سلطة شهادات داخلية محلية. بعد أول تشغيل، انسخ الشهادة العامة فقط من:

`%APPDATA%\Caddy\pki\authorities\local\root.crt`

إلى أجهزة الموظفين عبر قناة داخلية موثوقة، ثم افتح PowerShell كمسؤول على كل جهاز موظف ونفّذ:

```powershell
certutil -addstore -f Root .\root.crt
```

لا تنسخ ولا تشارك ملف المفتاح الخاص `root.key`. انسخ `root.crt` فقط. بعد تثبيت الشهادة وحل الاسم الداخلي، افتح:

`https://erp.company.lan`

إذا تغير مستخدم Windows الذي يشغل Caddy، قد يتغير مخزن الشهادات المحلي؛ حافظ على تشغيل Caddy تحت حساب خدمة ثابت، ووزّع شهادة الجذر العامة المطابقة لذلك الحساب على العملاء.

## التحقق قبل الاستخدام

- جرّب الدخول من جهاز الخادم ومن جهاز موظف منفصل.
- تأكد أن عنوان HTTPS لا يعرض تحذير شهادة بعد تثبيت الجذر.
- تأكد أن HTTP يتحول إلى HTTPS.
- تأكد أن منفذ 8000 غير متاح من جهاز موظف.
- اختبر تسجيل الدخول، CSRF، صلاحيات الفروع، والتقارير من جهازين متزامنين.
- فعّل النسخ الاحتياطي اليومي، واحتفظ بنسخة خارج جهاز الخادم، واختبر الاستعادة فعليًا قبل إدخال أرصدة أو قيود حقيقية.

## حدود النسخة

- هذا إعداد LAN آمن على مستوى TLS والربط المحلي، لكنه لا يغني عن اختبار حمل 10/20/30 مستخدمًا على جهاز الخادم الفعلي.
- لا يوجد إعداد تلقائي لـ DNS في الراوتر؛ مسؤول الشبكة يضيف سجل الاسم الداخلي أو hosts.
- لا تستخدم الشهادة الداخلية على الإنترنت العام.
- لا توصل قاعدة SQLite القديمة بهذه النسخة؛ قاعدة PostgreSQL مستقلة.
- لا تعتبر الحزمة معتمدة للإنتاج قبل اختبار النسخ الاحتياطي والاستعادة، والأداء، وصلاحيات المستخدمين على بيئة الشركة.
