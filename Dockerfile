FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY . /app

RUN mkdir -p /app/state /app/metrics

CMD ["python", "scripts/scan_or_import_findings.py"]
