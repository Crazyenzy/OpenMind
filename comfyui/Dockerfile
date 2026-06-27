# =============================================================================
# ComfyUI + ComfyUI-Distributed — Built from source
# =============================================================================
# The upstream robertvoy/ComfyUI-Distributed does NOT publish a Docker image.
# This Dockerfile builds ComfyUI from source and installs the Distributed
# extension as a custom node.
# =============================================================================

FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv git curl \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /workspace

# Clone ComfyUI
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /workspace/ComfyUI

# Install ComfyUI dependencies
RUN pip3 install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
RUN pip3 install --no-cache-dir -r /workspace/ComfyUI/requirements.txt

# Install ComfyUI-Distributed extension
RUN git clone --depth 1 https://github.com/robertvoy/ComfyUI-Distributed.git \
    /workspace/ComfyUI/custom_nodes/ComfyUI-Distributed

# Copy workflow templates
COPY comfyui_workflows/ /workspace/workflows/

WORKDIR /workspace/ComfyUI

EXPOSE 8188

# Default: start as master with CORS enabled
# Override COMFYUI_EXTRA_ARGS for worker mode
ENV COMFYUI_EXTRA_ARGS="--enable-cors-header --listen --port 8188"

CMD python3 main.py ${COMFYUI_EXTRA_ARGS}
