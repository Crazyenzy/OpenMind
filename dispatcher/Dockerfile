FROM python:3.11-slim
LABEL org.openmind.app="OpenMind Dispatcher"
LABEL description="Central glue service for OpenMind distributed AI mesh"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY config.yaml .

RUN mkdir -p /tmp/pod-output

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
