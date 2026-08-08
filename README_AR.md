# بوت تدوير كلمة مرور Netflix + SMSBower

## Railway
1. ارفع الملفات إلى GitHub.
2. أنشئ Railway Service من المستودع. وجود `Dockerfile` يجعل Railway يبني Chromium + Playwright تلقائياً.
3. أضف Variables من `.env.example`.
4. أضف Volume مركب على `/data` حتى تبقى إعدادات Toggle وكلمة المرور الافتراضية محفوظة بعد إعادة التشغيل.

## البريد
- `MAIL_MODE=generator`: يستخدم صندوق Generator.email مثل `https://generator.email/inbox9/a0yjib@5xu.vn`.
- `MAIL_MODE=imap`: يحتاج `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` لصندوق يستقبل رسائل حساباتك.

## الأمان
- لا يسمح بتغيير كلمة مرور أي بريد إلا إذا كان ضمن `NETFLIX_ALLOWED_EMAILS` أو نطاقه ضمن `NETFLIX_ALLOWED_DOMAINS`.
- روابط إعادة التعيين لا تظهر للمستخدم ولا تُطبع في السجل.
- يتم قبول روابط HTTPS التابعة لـ Netflix فقط.
