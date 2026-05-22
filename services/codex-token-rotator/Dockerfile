FROM python:3.12-slim-bookworm

ARG OP_VERSION=2.30.3
ARG TARGETARCH=amd64

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_linux_${TARGETARCH}_v${OP_VERSION}.zip" -o /tmp/op.zip \
    && unzip /tmp/op.zip -d /usr/local/bin \
    && rm /tmp/op.zip \
    && chmod +x /usr/local/bin/op \
    && apt-get purge -y --auto-remove curl unzip

# The op CLI walks getpwuid() to identify the "current user" before its
# strict ownership checks on session/config dirs. Without a passwd entry,
# the check spuriously fails even when files are owned by the running uid.
RUN echo 'rotator:x:65532:65532::/home/rotator:/sbin/nologin' >> /etc/passwd \
    && mkdir -p /home/rotator \
    && chown 65532:65532 /home/rotator

COPY services/codex-token-rotator/rotate.py /app/rotate.py

USER 65532:65532
WORKDIR /app

ENTRYPOINT ["python", "/app/rotate.py"]
