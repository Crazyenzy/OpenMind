FROM python:3.11-slim
LABEL org.openmind.app="OpenMind Dispatcher"
LABEL description="Central glue service for OpenMind distributed AI mesh"

WORKDIR /app

# Install Python dependencies
COPY dispatcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy dispatcher code
COPY dispatcher/main.py .
COPY dispatcher/config.yaml .

# Copy workflow templates (main.py loads them via ../comfyui_workflows/)
COPY comfyui_workflows/ /app/comfyui_workflows/

RUN mkdir -p /tmp/pod-output

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
