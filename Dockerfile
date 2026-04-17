FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/state /app/metrics

CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
