// Package ops is the operational HTTP endpoint every service exposes on a
// port separate from its traffic port: Prometheus metrics and the Kubernetes
// liveness and readiness probes.
package ops

import (
	"context"
	"errors"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Server serves /metrics, /healthz and /readyz.
type Server struct {
	srv   *http.Server
	ready atomic.Bool
}

// NewServer returns an ops server listening on addr. It reports not-ready
// until SetReady(true) is called.
func NewServer(addr string) *Server {
	s := &Server{}
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.Handler())

	// Liveness: the process is up and its HTTP stack answers. It deliberately
	// checks nothing else. A liveness probe that fails when the database is
	// slow makes Kubernetes restart healthy pods and turns one problem into two.
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	// Readiness: startup finished (dependencies were reached at least once and
	// the traffic port is about to listen). It does not re-check dependencies
	// on every probe, for the same reason: if Postgres blips, marking every
	// replica unready removes the whole service instead of letting requests
	// fail individually and visibly.
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !s.ready.Load() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})

	s.srv = &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	return s
}

// SetReady flips the readiness probe.
func (s *Server) SetReady(ready bool) {
	s.ready.Store(ready)
}

// Run serves until ctx is cancelled, then shuts down gracefully.
func (s *Server) Run(ctx context.Context) error {
	return ServeHTTP(ctx, s.srv)
}

// ServeHTTP runs srv until ctx is cancelled, then gives in-flight requests up
// to ten seconds to finish. It returns nil on a clean shutdown.
func ServeHTTP(ctx context.Context, srv *http.Server) error {
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	select {
	case err := <-errc:
		// ListenAndServe only returns on failure here (for example the port is
		// taken), because nothing else has called Shutdown yet.
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil && !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	}
}
