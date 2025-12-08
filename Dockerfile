FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc build-essential libssl-dev libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY . /app

RUN python -m pip install --upgrade pip
RUN pip install -r requirements.txt

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "main:fastapp", "--host", "0.0.0.0", "--port", "8000"]
