FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY package.json /app/package.json
COPY package-lock.json /app/package-lock.json
COPY tsconfig.json /app/tsconfig.json

RUN python -m pip install --no-cache-dir -r /app/requirements.txt
RUN npm ci

COPY . /app

RUN npm run build:dashboard
RUN mkdir -p /app/state /app/metrics

EXPOSE 8000 8001

CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
