FROM --platform=$BUILDPLATFORM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG REVISION=development
ARG ANALYSIS_VARIANT=baseline
ARG TARGETOS
ARG TARGETARCH
RUN --mount=type=cache,target=/root/.cache/go-build go run ./cmd/licenses /licenses
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -trimpath -ldflags="-s -w -X main.revision=${REVISION} -X main.analysisVariant=${ANALYSIS_VARIANT}" -o /workshop ./cmd/workshop
FROM scratch
ARG REVISION=development
ARG ANALYSIS_VARIANT=baseline
LABEL org.opencontainers.image.source="https://github.com/milesfoundry/gcp-platform-delivery-lab" \
      org.opencontainers.image.revision=${REVISION} \
      uk.whitt.lab.analysis-variant=${ANALYSIS_VARIANT}
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /workshop /usr/local/bin/workshop
COPY --from=build /licenses /usr/share/licenses/report-workshop
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/workshop"]
CMD ["api"]
