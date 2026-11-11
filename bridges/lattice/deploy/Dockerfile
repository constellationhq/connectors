FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58
ARG VERSION=dev
LABEL org.opencontainers.image.source=https://github.com/constellationhq/connectors \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.licenses=Apache-2.0
# Built from the repository root: docker build -f bridges/lattice/deploy/Dockerfile .
# sgp4 (the [lattice] extra, pinned by requirements.txt) places satellites as Constellation's pass
# engine does (PUBLISH_SATELLITES, which stops at start without it).
COPY bridges/lattice/deploy/requirements.txt /tmp/requirements.txt
COPY clients/python /src
RUN pip install --no-cache-dir -c /tmp/requirements.txt "/src[lattice]" && rm -rf /src /tmp/requirements.txt
# State (the feed cursor, what was acted on) lives in this volume: a new container on the same
# volume resumes where the last one stopped; with none, a first start begins at the newest run.
RUN mkdir -p /var/lib/constellation && chown 65534:65534 /var/lib/constellation
ENV STATE_DIR=/var/lib/constellation
VOLUME /var/lib/constellation
USER 65534:65534
CMD ["python", "-u", "-m", "constellation_space.bridges.lattice"]
