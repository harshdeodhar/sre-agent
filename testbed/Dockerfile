# syntax=docker/dockerfile:1

# One image holds all five testbed binaries; each Deployment picks its binary
# with `command`. On a small machine this means one build, one image to load
# into the cluster, and one set of layers shared by every pod.

FROM golang:1.26.2-alpine AS build
WORKDIR /src

# Dependencies first, so editing source does not re-download modules.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
# CGO off gives static binaries that run on a base image with no libc.
# -s -w drops the symbol table and debug info: measured on linux/arm64, the
# five binaries go from 139 MB to 96 MB in total (30-31% each). Stack traces
# still show function names and line numbers; only debuggers lose out.
# The cache mounts keep the module and build caches between builds.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/ ./cmd/...

# distroless/static: CA certificates, tzdata and a non-root user, and nothing
# else. No shell and no package manager in the running containers.
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/ /usr/local/bin/
# The numeric ID of distroless's "nonroot" user. Kubernetes can only enforce
# runAsNonRoot when the image declares its user as a number.
USER 65532:65532
