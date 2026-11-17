FROM python:3.12.14-slim

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --no-cache-dir -r requirements.txt

COPY simulator/ ./simulator/

RUN groupadd --system appuser && useradd --system --gid appuser --no-create-home appuser

USER appuser

CMD ["python", "-m", "simulator"]
