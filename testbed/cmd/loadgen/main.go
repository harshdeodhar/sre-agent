// loadgen sends a steady, realistic mix of requests to the api-gateway so the
// testbed always has traffic to observe.
//
// It is an open-loop generator: requests start on a fixed schedule whether or
// not earlier ones have finished. A closed-loop generator (N workers, each
// waiting for its response before sending the next) slows down when the system
// slows down, which hides exactly the latency problems the testbed exists to
// show.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/harshdeodhar/sre-agent/testbed/internal/config"
	"github.com/harshdeodhar/sre-agent/testbed/internal/ops"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

const service = "loadgen"

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// The load generator plays the part of external clients, so it does not
	// export traces: a trace should start at the gateway, as it would for a
	// real user. Setup still configures structured logging.
	shutdownTelemetry, err := telemetry.Setup(ctx, service)
	if err != nil {
		return err
	}
	defer func() { _ = shutdownTelemetry(context.Background()) }()

	var env config.Env
	var (
		opsAddr        = env.String("OPS_ADDR", ":8081")
		targetURL      = env.Required("TARGET_URL")
		rps            = env.Float("RPS", 10)
		maxInFlight    = env.Int("MAX_IN_FLIGHT", 64)
		requestTimeout = env.Duration("REQUEST_TIMEOUT", 5*time.Second)
		skuCount       = env.Int("SKU_COUNT", 200)
		customerCount  = env.Int("CUSTOMER_COUNT", 5000)
		summaryEvery   = env.Duration("SUMMARY_INTERVAL", 30*time.Second)
	)
	if err := env.Err(); err != nil {
		return fmt.Errorf("invalid configuration: %w", err)
	}
	if rps <= 0 || maxInFlight < 1 || skuCount < 1 || customerCount < 1 {
		return fmt.Errorf("invalid configuration: RPS, MAX_IN_FLIGHT, SKU_COUNT and CUSTOMER_COUNT must be positive")
	}

	gen := &generator{
		baseURL: strings.TrimRight(targetURL, "/"),
		client: &http.Client{
			Timeout: requestTimeout,
			Transport: &http.Transport{
				MaxIdleConns:        maxInFlight,
				MaxIdleConnsPerHost: maxInFlight,
				// Recycle connections regularly so traffic spreads across
				// gateway replicas as they come and go, instead of sticking to
				// whichever pods existed when the generator started.
				IdleConnTimeout: 15 * time.Second,
			},
		},
		picker:   newPicker(skuCount, customerCount),
		inFlight: make(chan struct{}, maxInFlight),
	}

	opsServer := ops.NewServer(opsAddr)
	g, ctx := errgroup.WithContext(ctx)
	g.Go(func() error { return opsServer.Run(ctx) })
	g.Go(func() error { gen.run(ctx, rps); return nil })
	g.Go(func() error { gen.logSummaries(ctx, summaryEvery); return nil })
	opsServer.SetReady(true)

	slog.Info("starting", "target", targetURL, "rps", rps, "max_in_flight", maxInFlight,
		"request_timeout", requestTimeout.String())

	err = g.Wait()
	slog.Info("stopped")
	return err
}
