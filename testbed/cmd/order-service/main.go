// order-service owns orders. Creating one reserves stock in inventory-service,
// stores the order in Postgres and publishes an orders.created event to NATS
// JetStream.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/nats-io/nats.go/jetstream"
	"golang.org/x/sync/errgroup"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
	"github.com/harshdeodhar/sre-agent/testbed/internal/config"
	"github.com/harshdeodhar/sre-agent/testbed/internal/events"
	"github.com/harshdeodhar/sre-agent/testbed/internal/ops"
	"github.com/harshdeodhar/sre-agent/testbed/internal/retry"
	"github.com/harshdeodhar/sre-agent/testbed/internal/rpc"
	"github.com/harshdeodhar/sre-agent/testbed/internal/store"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

const service = "order-service"

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
		grpcAddr      = env.String("GRPC_ADDR", ":9000")
		opsAddr       = env.String("OPS_ADDR", ":8081")
		inventoryAddr = env.Required("INVENTORY_SERVICE_ADDR")
		natsURL       = env.Required("NATS_URL")
		dbConfig      = store.ConfigFromEnv(&env)
		// Each downstream call gets its own budget inside the request's overall
		// deadline, so one slow dependency cannot consume all of it.
		inventoryTimeout = env.Duration("INVENTORY_TIMEOUT", time.Second)
		publishTimeout   = env.Duration("PUBLISH_TIMEOUT", 500*time.Millisecond)
		startupTimeout   = env.Duration("STARTUP_TIMEOUT", 30*time.Second)
	)
	if err := env.Err(); err != nil {
		return fmt.Errorf("invalid configuration: %w", err)
	}

	opsServer := ops.NewServer(opsAddr)
	g, ctx := errgroup.WithContext(ctx)
	// The ops port comes up first so the pod is scrapeable and answers its
	// liveness probe while it waits for dependencies.
	g.Go(func() error { return opsServer.Run(ctx) })

	pool, err := store.Open(ctx, service, dbConfig)
	if err != nil {
		return err
	}
	defer pool.Close()

	nc, err := events.Connect(ctx, natsURL, service, startupTimeout)
	if err != nil {
		return err
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		return fmt.Errorf("create JetStream client: %w", err)
	}
	err = retry.Until(ctx, "jetstream stream", startupTimeout, func(ctx context.Context) error {
		_, err := events.EnsureStream(ctx, js)
		return err
	})
	if err != nil {
		return err
	}

	inventoryConn, err := rpc.Dial(inventoryAddr)
	if err != nil {
		return err
	}
	defer func() { _ = inventoryConn.Close() }()

	grpcServer := rpc.NewServer()
	shopv1.RegisterOrderServiceServer(grpcServer, &server{
		pool:             pool,
		inventory:        shopv1.NewInventoryServiceClient(inventoryConn),
		js:               js,
		inventoryTimeout: inventoryTimeout,
		publishTimeout:   publishTimeout,
	})

	slog.Info("starting", "grpc_addr", grpcAddr, "ops_addr", opsAddr, "inventory_service", inventoryAddr,
		"db_max_conns", dbConfig.MaxConns, "inventory_timeout", inventoryTimeout.String(), "publish_timeout", publishTimeout.String())

	g.Go(func() error { return rpc.Serve(ctx, grpcServer, grpcAddr) })
	opsServer.SetReady(true)

	err = g.Wait()
	slog.Info("stopped")
	return err
}
