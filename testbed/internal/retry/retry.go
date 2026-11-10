// Package retry provides the one retry loop the services need: waiting for a
// dependency to become reachable at startup.
package retry

import (
	"context"
	"fmt"
	"log/slog"
	"time"
)

// Until calls fn until it succeeds, the timeout elapses, or ctx is cancelled.
//
// Services use it to connect to Postgres, Redis and NATS at startup. Retrying
// for a bounded time tolerates normal start-order races when the whole cluster
// comes up at once. Giving up afterwards is deliberate: the process exits,
// Kubernetes restarts it, and a dependency that is really unreachable (for
// example a wrong hostname) shows up as CrashLoopBackOff with a clear last log
// line, rather than a pod that looks Running but can never serve.
func Until(ctx context.Context, what string, timeout time.Duration, fn func(context.Context) error) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	const interval = time.Second
	var last error
	for attempt := 1; ; attempt++ {
		if last = fn(ctx); last == nil {
			return nil
		}
		slog.WarnContext(ctx, "dependency not ready, retrying", "dependency", what, "attempt", attempt, "err", last)

		select {
		case <-ctx.Done():
			return fmt.Errorf("%s not ready after %s: %w", what, timeout, last)
		case <-time.After(interval):
		}
	}
}
