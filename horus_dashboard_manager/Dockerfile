ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache python3 py3-pip && python3 -m venv /opt/horus-venv
COPY requirements-runtime.txt /requirements-runtime.txt
RUN /opt/horus-venv/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes -r /requirements-runtime.txt
COPY manager.py /manager.py
COPY run.sh /run.sh
RUN chmod 0755 /run.sh
CMD ["/run.sh"]
