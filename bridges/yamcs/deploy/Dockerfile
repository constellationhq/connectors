FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ARG VERSION=dev
LABEL org.opencontainers.image.source=https://github.com/constellationhq/connectors \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.licenses=Apache-2.0
# Built from the repository root: docker build -f bridges/yamcs/deploy/Dockerfile .
COPY clients/python /src
RUN pip install --no-cache-dir /src && rm -rf /src
# State (the feed cursor, what was acted on) lives in this volume: a new container on the same
# volume resumes where the last one stopped; with none, a first start begins at the newest run.
RUN mkdir -p /var/lib/constellation && chown 65534:65534 /var/lib/constellation
ENV STATE_DIR=/var/lib/constellation
VOLUME /var/lib/constellation
USER 65534:65534
CMD ["python", "-u", "-m", "constellation_space.bridges.yamcs"]
