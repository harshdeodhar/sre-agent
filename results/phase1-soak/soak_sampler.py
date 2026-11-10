"""Sample Tempo and Loki Go runtime metrics and host swap activity every 5 s."""

import subprocess
import sys
import time
import urllib.request

TARGETS = {"tempo": "http://127.0.0.1:3200/metrics", "loki": "http://127.0.0.1:3100/metrics"}
WANTED = (
    "go_memstats_heap_alloc_bytes",
    "go_memstats_heap_inuse_bytes",
    "go_memstats_next_gc_bytes",
    "process_resident_memory_bytes",
    "go_gc_duration_seconds_count",
)


def swap_pages() -> int:
    """Pages swapped in plus out since boot, from vm_stat (16 KiB pages on Apple silicon)."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    total = 0
    for line in out.splitlines():
        if line.startswith(("Swapins", "Swapouts")):
            total += int(line.split(":")[1].strip().rstrip("."))
    return total


minutes = float(sys.argv[1])
peaks = {t: dict.fromkeys(WANTED, 0.0) for t in TARGETS}
first_gc: dict[str, float] = {}
samples = failures = 0
end = time.time() + minutes * 60
with open("/tmp/sre-prof/soak.csv", "w") as out, open("/tmp/sre-prof/swap.csv", "w") as swap:
    out.write("unix,target," + ",".join(WANTED) + "\n")
    swap.write("unix,swap_pages_total\n")
    while time.time() < end:
        swap.write(f"{time.time():.0f},{swap_pages()}\n")
        swap.flush()
        for target, url in TARGETS.items():
            try:
                text = urllib.request.urlopen(url, timeout=4).read().decode()
            except Exception:
                failures += 1
                continue
            row = {}
            for line in text.splitlines():
                name, _, value = line.partition(" ")
                if name in WANTED:
                    row[name] = float(value)
            first_gc.setdefault(target, row["go_gc_duration_seconds_count"])
            for k, v in row.items():
                peaks[target][k] = max(peaks[target][k], v)
            out.write(
                f"{time.time():.0f},{target}," + ",".join(f"{row[k]:.0f}" for k in WANTED) + "\n"
            )
            samples += 1
        time.sleep(5)
print(f"samples: {samples}, failed fetches: {failures}, minutes: {minutes}")
for target, p in peaks.items():
    gcs = p["go_gc_duration_seconds_count"] - first_gc[target]
    print(
        f"{target}: peak heap_alloc {p['go_memstats_heap_alloc_bytes'] / 2**20:.0f} MiB, "
        f"peak heap_inuse {p['go_memstats_heap_inuse_bytes'] / 2**20:.0f} MiB, "
        f"peak next_gc {p['go_memstats_next_gc_bytes'] / 2**20:.0f} MiB, "
        f"peak RSS {p['process_resident_memory_bytes'] / 2**20:.0f} MiB, GC cycles {gcs:.0f}"
    )
