FROM golang:1.22 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -o /bridge . && mkdir /state

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /bridge /bridge
# The cursor and the approvals carried out live in a volume the bridge's user (nonroot, 65532) can
# write, so a rebuilt container resumes where it stopped and never acts twice.
COPY --from=build --chown=65532:65532 /state /var/lib/constellation
ENV CURSOR_FILE=/var/lib/constellation/cursor.txt
VOLUME /var/lib/constellation
ENTRYPOINT ["/bridge"]
