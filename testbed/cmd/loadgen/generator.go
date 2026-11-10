package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	requestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "loadgen_requests_total",
		Help: "Requests sent, by operation and outcome as seen by the client.",
	}, []string{"op", "outcome"})

	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "loadgen_request_duration_seconds",
		Help:    "Client-side request latency, including time spent waiting on a timeout.",
		Buckets: prometheus.DefBuckets,
	}, []string{"op"})

	droppedTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "loadgen_dropped_total",
		Help: "Requests not sent because MAX_IN_FLIGHT requests were already outstanding.",
	})
)

// Outcomes as the client sees them. An HTTP 4xx is kept apart from a 5xx
// because a 409 "insufficient stock" is the system working as designed, while
// a 5xx or a transport error is not.
const (
	outcomeOK             = "ok"
	outcomeClientError    = "client_error"
	outcomeServerError    = "server_error"
	outcomeTransportError = "transport_error"
)

type generator struct {
	baseURL string
	client  *http.Client
	picker  *picker

	// inFlight bounds concurrency. Open-loop load against a stalled system
	// would otherwise pile up goroutines and sockets without limit; past the
	// bound, requests are counted as dropped instead of sent.
	inFlight chan struct{}

	// Counters for the periodic summary log line.
	sent, failed, dropped atomic.Int64

	recent recentOrders
}

// run starts one request every 1/rps seconds until ctx is cancelled.
func (g *generator) run(ctx context.Context, rps float64) {
	ticker := time.NewTicker(time.Duration(float64(time.Second) / rps))
	defer ticker.Stop()

	var wg sync.WaitGroup
	defer wg.Wait()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		// The request is chosen here, on the single scheduling goroutine, so
		// the random source needs no locking.
		req := g.picker.next(g.recent.pick)

		select {
		case g.inFlight <- struct{}{}:
		default:
			droppedTotal.Inc()
			g.dropped.Add(1)
			continue
		}
		wg.Go(func() {
			defer func() { <-g.inFlight }()
			g.send(ctx, req)
		})
	}
}

// send performs one request and records its outcome.
func (g *generator) send(ctx context.Context, r request) {
	start := time.Now()
	outcome, body := g.do(ctx, r)
	requestDuration.WithLabelValues(r.op).Observe(time.Since(start).Seconds())
	requestsTotal.WithLabelValues(r.op, outcome).Inc()

	g.sent.Add(1)
	if outcome == outcomeServerError || outcome == outcomeTransportError {
		g.failed.Add(1)
	}

	// Remember the IDs of orders we created so later "get order" requests ask
	// for orders that exist.
	if r.op == opCreateOrder && outcome == outcomeOK {
		var created struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(body, &created); err == nil && created.ID != "" {
			g.recent.add(created.ID)
		}
	}
}

func (g *generator) do(ctx context.Context, r request) (outcome string, body []byte) {
	var reqBody io.Reader
	if r.body != nil {
		reqBody = bytes.NewReader(r.body)
	}
	req, err := http.NewRequestWithContext(ctx, r.method, g.baseURL+r.path, reqBody)
	if err != nil {
		slog.ErrorContext(ctx, "building request failed", "op", r.op, "err", err)
		return outcomeTransportError, nil
	}
	if r.body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := g.client.Do(req)
	if err != nil {
		// Timeouts, refused connections, resets. Logged at debug: during an
		// incident this would otherwise be thousands of identical lines.
		slog.DebugContext(ctx, "request failed", "op", r.op, "err", err)
		return outcomeTransportError, nil
	}
	defer func() { _ = resp.Body.Close() }()

	// Read the whole body (bounded) so the connection can be reused.
	body, err = io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return outcomeTransportError, nil
	}
	return classify(resp.StatusCode), body
}

func classify(statusCode int) string {
	switch {
	case statusCode >= 500:
		return outcomeServerError
	case statusCode >= 400:
		return outcomeClientError
	default:
		return outcomeOK
	}
}

// logSummaries writes one line per interval with what the client experienced.
func (g *generator) logSummaries(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			sent, failed, dropped := g.sent.Swap(0), g.failed.Swap(0), g.dropped.Swap(0)
			slog.Info("traffic summary",
				"interval", every.String(),
				"sent", sent,
				"failed", failed,
				"dropped", dropped,
				"achieved_rps", fmt.Sprintf("%.1f", float64(sent)/every.Seconds()),
			)
		}
	}
}

// recentOrders is a small fixed-size ring of order IDs created by this
// generator, shared between the scheduling goroutine and request goroutines.
type recentOrders struct {
	mu   sync.Mutex
	ids  [256]string
	next int
	size int
}

func (r *recentOrders) add(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ids[r.next] = id
	r.next = (r.next + 1) % len(r.ids)
	if r.size < len(r.ids) {
		r.size++
	}
}

// pick returns the n-th remembered ID (modulo the number held), or false if
// none has been recorded yet.
func (r *recentOrders) pick(n int) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.size == 0 {
		return "", false
	}
	return r.ids[n%r.size], true
}
