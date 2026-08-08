# النسخة المصححة لـ Railway

هذه النسخة Python فقط، وتم حذف تعارض Node.js الذي كان يجعل Railway يحاول تشغيل `npm start`.

## لماذا كان النشر يفشل؟
كان المستودع يحتوي `package.json` و`server.js` وداخل `railway.json` كان `startCommand` مضبوطاً على `npm start`، بينما البوت الجديد يعمل ببايثون وPlaywright.

## ما الذي تم إصلاحه؟
- Railway مجبر الآن على استخدام `Dockerfile` عبر `builder: DOCKERFILE`.
- لا يوجد `npm start` أو أي Start Command تابع لـ Node.
- التشغيل يتم من `CMD ["python", "bot.py"]` داخل Dockerfile.
- تثبيت Chromium وملحقات Playwright يتم أثناء Build.
- لا يوجد Healthcheck لمسار HTTP لأن هذا المشروع Telegram Bot worker وليس Web server.
- `bot.py` تم التحقق من Syntax الخاص به.

## مهم عند رفع الملفات إلى GitHub
يفضل حذف الملفات القديمة التالية من جذر المستودع إن كانت لا تزال موجودة:
- `package.json`
- `server.js`
- مجلد `public/`
- أي `railway.json` قديم

ثم ارفع ملفات هذه النسخة إلى جذر المستودع.

## Railway Variables
استخدم المتغيرات الموجودة في `.env.example`، ولا تضع أي أسرار داخل الكود.

## Railway Volume
للاحتفاظ بإعدادات كلمة المرور وخيار تسجيل الخروج بعد إعادة التشغيل، أضف Volume على:

`/data`

## إعدادات Railway القديمة
إذا سبق أن وضعت Custom Start Command يدوياً في Railway مثل `npm start`، احذفه من Settings > Deploy. مع Dockerfile يجب ترك Start Command فارغاً حتى يستخدم Railway أمر CMD الموجود في Dockerfile.
