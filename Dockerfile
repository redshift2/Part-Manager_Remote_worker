FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    python3 \
    python3-venv \
    libgl1 \
    libglib2.0-0 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libfontconfig1 \
    libfreetype6 \
    libegl1 \
    libopengl0 \
    && rm -rf /var/lib/apt/lists/*

# Add the FreeCAD 1.1 AppImage to the build context.
COPY FreeCAD_1.1.4-Linux-x86_64-py311.AppImage /tmp/FreeCAD.AppImage

# Extract FreeCAD 1.1.4 without requiring FUSE
RUN chmod +x /tmp/FreeCAD.AppImage \
    && cd /opt \
    && /tmp/FreeCAD.AppImage --appimage-extract \
    && mv squashfs-root freecad \
    && rm -f /tmp/FreeCAD.AppImage

# Wrapper for FreeCAD's command-line executable.
RUN printf '#!/bin/sh\nexec /opt/freecad/usr/bin/freecadcmd "$@"\n' \
    > /usr/local/bin/FreeCADCmd \
    && chmod +x /usr/local/bin/FreeCADCmd

# Python environment for the PLM worker.
RUN python3 -m venv /opt/pm-venv \
    && /opt/pm-venv/bin/pip install --no-cache-dir \
       requests cryptography trimesh numpy

RUN useradd --system --create-home \
    --home-dir /home/pmworker \
    --uid 10001 pmworker \
    && mkdir -p /var/lib/pm-worker \
    && chown pmworker:pmworker /var/lib/pm-worker

WORKDIR /app

COPY --chown=pmworker:pmworker worker/worker.py /app/worker.py
COPY --chown=pmworker:pmworker entrypoint.py /app/entrypoint.py

USER pmworker

ENV PATH="/opt/pm-venv/bin:${PATH}"
ENV FREECAD_CMD="/usr/local/bin/FreeCADCmd"
ENV QT_QPA_PLATFORM="offscreen"

ENTRYPOINT ["python3", "/app/entrypoint.py"]
