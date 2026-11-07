FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common ca-certificates python3 python3-venv \
    && add-apt-repository -y universe \
    && apt-get update && apt-get install -y --no-install-recommends freecad \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/pm-venv && /opt/pm-venv/bin/pip install --no-cache-dir \
    requests cryptography trimesh numpy
RUN useradd --system --create-home --home-dir /home/pmworker --uid 10001 pmworker \
    && mkdir -p /var/lib/pm-worker && chown pmworker:pmworker /var/lib/pm-worker
WORKDIR /app
COPY --chown=pmworker:pmworker worker/worker.py /app/worker.py
COPY --chown=pmworker:pmworker entrypoint.py /app/entrypoint.py
USER pmworker
ENV PATH="/opt/pm-venv/bin:${PATH}"
ENTRYPOINT ["python3", "/app/entrypoint.py"]
