// api-gateway is the HTTP entry point of the testbed. It translates the public
// REST API into gRPC calls to order-service and inventory-service and holds no
// state of its own.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"golang.org/x/sync/errgroup"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
	"github.com/harshdeodhar/sre-agent/testbed/internal/config"
	"github.com/harshdeodhar/sre-agent/testbed/internal/ops"
	"github.com/harshdeodhar/sre-agent/testbed/internal/rpc"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

const service = "api-gateway"

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	shutdownTelemetry, err := telemetry.Setup(ctx, service)
	if err != nil {
		return err
	}
	defer func() { _ = shutdownTelemetry(context.Background()) }()

	var env config.Env
	var (
		httpAddr      = env.String("HTTP_ADDR", ":8080")
		opsAddr       = env.String("OPS_ADDR", ":8081")
		orderAddr     = env.Required("ORDER_SERVICE_ADDR")
		inventoryAddr = env.Required("INVENTORY_SERVICE_ADDR")
		// Upper bound on one request's upstream work. It travels downstream as
		// the gRPC deadline, so no service keeps working on a request the
		// gateway has already given up on.
		upstreamTimeout = env.Duration("UPSTREAM_TIMEOUT", 2*time.Second)
	)
	if err := env.Err(); err != nil {
		return fmt.Errorf("invalid configuration: %w", err)
	}

	orderConn, err := rpc.Dial(orderAddr)
	if err != nil {
		return err
	}
	defer func() { _ = orderConn.Close() }()
	inventoryConn, err := rpc.Dial(inventoryAddr)
	if err != nil {
		return err
	}
	defer func() { _ = inventoryConn.Close() }()

	gw := &gateway{
		orders:    shopv1.NewOrderServiceClient(orderConn),
		inventory: shopv1.NewInventoryServiceClient(inventoryConn),
		timeout:   upstreamTimeout,
	}

	// otelhttp is outermost so the server span exists (and is in the request
	// context) by the time InstrumentHTTP logs and the handlers call gRPC.
	handler := otelhttp.NewHandler(telemetry.InstrumentHTTP(gw.routes()), service)
	apiServer := &http.Server{Addr: httpAddr, Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	opsServer := ops.NewServer(opsAddr)

	slog.Info("starting", "http_addr", httpAddr, "ops_addr", opsAddr,
		"order_service", orderAddr, "inventory_service", inventoryAddr, "upstream_timeout", upstreamTimeout.String())

	g, ctx := errgroup.WithContext(ctx)
	g.Go(func() error { return opsServer.Run(ctx) })
	g.Go(func() error { return ops.ServeHTTP(ctx, apiServer) })
	// The gRPC connections are lazy, so the gateway is ready as soon as it
	// listens. If an upstream is down, requests fail with 503 and say so.
	opsServer.SetReady(true)

	err = g.Wait()
	slog.Info("stopped")
	return err
}
