FROM ghcr.io/home-assistant/aarch64-base@sha256:5100b3a2e4741440e7bd587c124920fa3bf02287c4de3cb48aacef7049530ee1
RUN apk add --no-cache python3
COPY watcher.py /watcher.py
COPY run.sh /run.sh
RUN chmod 0755 /run.sh
CMD ["/run.sh"]
