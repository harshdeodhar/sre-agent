#!/bin/sh
# Apply, show or remove `tc netem` on the network interface of another pod.
#
#   pod-netem <pod-uid> apply <netem arguments...>   e.g. delay 300ms 50ms
#   pod-netem <pod-uid> show
#   pod-netem <pod-uid> clear
#
# How it works: this container shares the node's PID namespace, so it can see
# every process on the node. It finds one that belongs to the target pod (the
# pod's UID is part of its cgroup path), enters that process's network
# namespace, and runs `tc` there. All containers of a pod share one network
# namespace, so any of its processes will do.
#
# netem shapes packets LEAVING the interface it is attached to. On a pod's
# eth0 that is everything the pod sends: its responses to callers and its own
# requests to its dependencies.
set -eu

if [ "$#" -lt 2 ]; then
    echo "usage: pod-netem <pod-uid> apply <netem arguments...> | show | clear" >&2
    exit 2
fi
uid="$1"
verb="$2"
shift 2

pid=""
for cgroup in /proc/[0-9]*/cgroup; do
    if grep -q "pod${uid}" "$cgroup" 2>/dev/null; then
        pid="${cgroup#/proc/}"
        pid="${pid%/cgroup}"
        break
    fi
done

if [ -z "$pid" ]; then
    # A pod that no longer exists has nothing to clear; for anything else a
    # missing pod is an error.
    if [ "$verb" = "clear" ]; then
        exit 0
    fi
    echo "pod-netem: no process found for pod ${uid}" >&2
    exit 3
fi

in_pod() {
    nsenter -t "$pid" -n "$@"
}

case "$verb" in
    apply)
        # `replace` so that applying twice does not fail.
        in_pod tc qdisc replace dev eth0 root netem "$@"
        ;;
    show)
        in_pod tc qdisc show dev eth0
        ;;
    clear)
        # Deleting a qdisc that is not there is an error, so look first.
        if in_pod tc qdisc show dev eth0 | grep -q netem; then
            in_pod tc qdisc del dev eth0 root
        fi
        ;;
    *)
        echo "pod-netem: unknown verb ${verb}" >&2
        exit 2
        ;;
esac
