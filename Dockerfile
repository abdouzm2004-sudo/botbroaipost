FROM python:3.11-slim

# مكان عمل التطبيق
WORKDIR /app

# تثبيت الأدوات الأساسية
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc build-essential libssl-dev libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# نسخ ملفات المشروع
COPY . /app

# تحديث pip وتثبيت المكتبات
RUN python -m pip install --upgrade pip
RUN pip install -r requirements.txt

# متغير المنفذ (Render يستخدم PORT)
ENV PORT=8000
EXPOSE 8000

# تشغيل التطبيق — هنا أصل المشكلة وتم إصلاحها
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
