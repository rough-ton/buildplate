# syntax=docker/dockerfile:1.7
#
# Buildplate: one container with OpenSCAD (Manifold engine), headless rendering,
# OpenSCAD libraries, the Python backend, web UI and MCP endpoint.
#
# The default base is the official OpenSCAD development image, which ships the
# Manifold geometry engine. Any Debian or Ubuntu image works as a base; if it has
# no openscad binary, the distribution package is installed instead.

ARG BASE_IMAGE=openscad/openscad:dev
FROM ${BASE_IMAGE}

ARG VERSION=0.1.0
ARG BOSL2_REF=master
ARG MCAD_REF=master

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BUILDPLATE_VERSION=${VERSION} \
    BUILDPLATE_DATA=/data \
    BUILDPLATE_PORT=8080 \
    BUILDPLATE_BUNDLED_LIBS=/opt/openscad-libraries \
    BUILDPLATE_DOCS=/app/docs \
    PATH=/opt/venv/bin:${PATH}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      xvfb xauth libgl1-mesa-dri \
      fonts-dejavu-core fonts-liberation2 \
      ca-certificates curl git tini util-linux \
 && (command -v openscad >/dev/null || apt-get install -y --no-install-recommends openscad) \
 && rm -rf /var/lib/apt/lists/*

# OpenSCAD libraries baked into the image. Add your own under /data/libraries.
RUN mkdir -p /opt/openscad-libraries \
 && git clone --depth 1 --branch ${BOSL2_REF} https://github.com/BelfrySCAD/BOSL2 /opt/openscad-libraries/BOSL2 \
 && git clone --depth 1 --branch ${MCAD_REF} https://github.com/openscad/MCAD /opt/openscad-libraries/MCAD \
 && rm -rf /opt/openscad-libraries/*/.git /opt/openscad-libraries/BOSL2/tutorials /opt/openscad-libraries/BOSL2/images

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

RUN groupadd -g 1000 buildplate 2>/dev/null || true \
 && useradd -u 1000 -g 1000 -d /data -s /usr/sbin/nologin buildplate 2>/dev/null || true

WORKDIR /app
COPY app ./app
COPY docs ./docs
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /data && chown 1000:1000 /data \
 && openscad --version

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${BUILDPLATE_PORT}/api/health" || exit 1

LABEL org.opencontainers.image.title="Buildplate" \
      org.opencontainers.image.description="Self-hosted AI design studio for 3D printing: describe a part, get validated parametric OpenSCAD and STL/3MF." \
      org.opencontainers.image.source="https://github.com/rough-ton/buildplate" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-m", "app.main"]
