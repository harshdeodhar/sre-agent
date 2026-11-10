// inventory-service owns stock levels. Reads go through a Redis cache backed
// by Postgres; reservations are atomic updates in Postgres.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/extra/redisotel/v9"
	"github.com/redis/go-redis/v9"
	"golang.org/x/sync/errgroup"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
	"github.com/harshdeodhar/sre-agent/testbed/internal/config"
	"github.com/harshdeodhar/sre-agent/testbed/internal/ops"
	"github.com/harshdeodhar/sre-agent/testbed/internal/retry"
	"github.com/harshdeodhar/sre-agent/testbed/internal/rpc"
	"github.com/harshdeodhar/sre-agent/testbed/internal/store"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

const service = "inventory-service"

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
		grpcAddr        = env.String("GRPC_ADDR", ":9000")
		opsAddr         = env.String("OPS_ADDR", ":8081")
		redisAddr       = env.Required("REDIS_ADDR")
		dbConfig        = store.ConfigFromEnv(&env)
		cacheTTL        = env.Duration("CACHE_TTL", 30*time.Second)
		cacheTimeout    = env.Duration("CACHE_TIMEOUT", 250*time.Millisecond)
		restockInterval = env.Duration("RESTOCK_INTERVAL", 30*time.Second)
	)
	if err := env.Err(); err != nil {
		return fmt.Errorf("invalid configuration: %w", err)
	}

	opsServer := ops.NewServer(opsAddr)
	g, ctx := errgroup.WithContext(ctx)
	g.Go(func() error { return opsServer.Run(ctx) })

	pool, err := store.Open(ctx, service, dbConfig)
	if err != nil {
		return err
	}
	defer pool.Close()

	cache := redis.NewClient(&redis.Options{
		Addr: redisAddr,
		// Short timeouts and no retries: the cache is an optimisation, so when
		// it misbehaves a read should pay for at most one short timeout and
		// then fall back to Postgres.
		DialTimeout:  cacheTimeout,
		ReadTimeout:  cacheTimeout,
		WriteTimeout: cacheTimeout,
		MaxRetries:   -1,
	})
	defer func() { _ = cache.Close() }()
	if err := redisotel.InstrumentTracing(cache); err != nil {
		return fmt.Errorf("instrument redis client: %w", err)
	}
	// Postgres is a hard dependency (no database, no service). Redis is a soft
	// one: wait briefly for it at startup, but start without it if need be.
	err = retry.Until(ctx, "redis", 10*time.Second, func(ctx context.Context) error {
		return cache.Ping(ctx).Err()
	})
	if err != nil {
		slog.Warn("starting without a reachable cache; reads will go to Postgres", "err", err)
	}

	srv := &server{pool: pool, cache: cache, cacheTTL: cacheTTL}
	grpcServer := rpc.NewServer()
	shopv1.RegisterInventoryServiceServer(grpcServer, srv)

	slog.Info("starting", "grpc_addr", grpcAddr, "ops_addr", opsAddr, "redis", redisAddr,
		"db_max_conns", dbConfig.MaxConns, "cache_ttl", cacheTTL.String(), "restock_interval", restockInterval.String())

	g.Go(func() error { return rpc.Serve(ctx, grpcServer, grpcAddr) })
	g.Go(func() error { srv.restockLoop(ctx, restockInterval); return nil })
	opsServer.SetReady(true)

	err = g.Wait()
	slog.Info("stopped")
	return err
}
