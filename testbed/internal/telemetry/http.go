package telemetry

import (
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"go.opentelemetry.io/otel/trace"
)

var (
	httpRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_server_requests_total",
		Help: "HTTP requests handled, by method, route pattern and status code.",
	}, []string{"method", "route", "code"})

	httpDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_server_request_duration_seconds",
		Help:    "HTTP request latency, by method and route pattern.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "route"})
)

// statusRecorder remembers the status code a handler wrote.
type statusRecorder struct {
	http.ResponseWriter
	code int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.code = code
	r.ResponseWriter.WriteHeader(code)
}

// InstrumentHTTP records request metrics and writes one access-log line per
// request. It must wrap the ServeMux directly, with no middleware in between
// that copies the request: the mux stores the matched pattern on the request it
// is given, and that pattern is the "route" label.
//
// The label is the pattern ("/api/orders/{id}"), never the raw path. Raw paths
// contain IDs, and one time series per order ID would grow without bound.
func InstrumentHTTP(mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, code: http.StatusOK}

		mux.ServeHTTP(rec, r)

		route := r.Pattern
		if route == "" {
			route = "unmatched"
		}
		// The server span was started by otelhttp before the route was known.
		// Rename it now so traces read "GET /api/orders/{id}".
		trace.SpanFromContext(r.Context()).SetName(r.Method + " " + route)

		elapsed := time.Since(start)
		httpRequests.WithLabelValues(r.Method, route, strconv.Itoa(rec.code)).Inc()
		httpDuration.WithLabelValues(r.Method, route).Observe(elapsed.Seconds())

		level := slog.LevelInfo
		if rec.code >= 500 {
			level = slog.LevelError
		}
		slog.Log(r.Context(), level, "http request",
			"method", r.Method,
			"route", route,
			"path", r.URL.Path,
			"status", rec.code,
			"duration_ms", elapsed.Milliseconds(),
		)
	})
}
