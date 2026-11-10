# The netem helper: Alpine plus `tc` and one script. Built by `make image` and
# loaded into the cluster the same way as the services image.
FROM alpine:3.22.2

# iproute2-tc provides `tc`. `nsenter` is part of Alpine's busybox.
RUN apk add --no-cache iproute2-tc

COPY pod-netem.sh /usr/local/bin/pod-netem
RUN chmod 0755 /usr/local/bin/pod-netem

# The pod does nothing until the injector runs `pod-netem` in it.
ENTRYPOINT ["sleep", "infinity"]
