# Home Assistant Green is aarch64. Pin the official base image by immutable digest.
FROM ghcr.io/home-assistant/aarch64-base@sha256:5100b3a2e4741440e7bd587c124920fa3bf02287c4de3cb48aacef7049530ee1
RUN apk add --no-cache python3 py3-pip && python3 -m venv /opt/horus-venv
COPY requirements-runtime.txt /requirements-runtime.txt
RUN /opt/horus-venv/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes -r /requirements-runtime.txt
COPY manager.py /manager.py
COPY asset_manager.py /asset_manager.py
COPY asset_bridge.py /asset_bridge.py
COPY gallery_import.py /gallery_import.py
COPY run.sh /run.sh
RUN chmod 0755 /run.sh
CMD ["/run.sh"]
