// notification-worker consumes orders.created events from NATS JetStream and
// "sends" a notification for each: it waits for a simulated provider call, then
// records the notification in Postgres.
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

	"github.com/harshdeodhar/sre-agent/testbed/internal/config"
	"github.com/harshdeodhar/sre-agent/testbed/internal/events"
	"github.com/harshdeodhar/sre-agent/testbed/internal/ops"
	"github.com/harshdeodhar/sre-agent/testbed/internal/retry"
	"github.com/harshdeodhar/sre-agent/testbed/internal/store"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

const service = "notification-worker"

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
		opsAddr  = env.String("OPS_ADDR", ":8081")
		natsURL  = env.Required("NATS_URL")
		dbConfig = store.ConfigFromEnv(&env)
		// How long one simulated call to the notification provider takes. With
		// a single sequential consumer, throughput is about 1/sendDelay.
		sendDelay      = env.Duration("SEND_DELAY", 25*time.Millisecond)
		processTimeout = env.Duration("PROCESS_TIMEOUT", 5*time.Second)
		retryDelay     = env.Duration("RETRY_DELAY", 2*time.Second)
		startupTimeout = env.Duration("STARTUP_TIMEOUT", 30*time.Second)
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

	nc, err := events.Connect(ctx, natsURL, service, startupTimeout)
	if err != nil {
		return err
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		return fmt.Errorf("create JetStream client: %w", err)
	}

	var consumer jetstream.Consumer
	err = retry.Until(ctx, "jetstream consumer", startupTimeout, func(ctx context.Context) error {
		stream, err := events.EnsureStream(ctx, js)
		if err != nil {
			return err
		}
		consumer, err = stream.CreateOrUpdateConsumer(ctx, jetstream.ConsumerConfig{
			Durable: events.ConsumerName,
			// Explicit acks give at-least-once delivery: a message is
			// redelivered unless the worker confirms it was handled.
			AckPolicy: jetstream.AckExplicitPolicy,
			AckWait:   30 * time.Second,
			// After five failed attempts JetStream stops redelivering, so one
			// poison message cannot block the stream forever.
			MaxDeliver: 5,
			// Cap on unacknowledged messages handed out at once. Beyond this
			// the backlog stays in the stream, where it is visible as pending.
			MaxAckPending: 100,
		})
		return err
	})
	if err != nil {
		return err
	}

	w := &worker{pool: pool, sendDelay: sendDelay, processTimeout: processTimeout, retryDelay: retryDelay}

	slog.Info("starting", "ops_addr", opsAddr, "stream", events.StreamName, "consumer", events.ConsumerName,
		"send_delay", sendDelay.String(), "db_max_conns", dbConfig.MaxConns)

	// Consume delivers messages to w.handle one at a time on a single
	// goroutine, so the worker processes sequentially. Scaling out means more
	// replicas sharing the same durable consumer.
	consuming, err := consumer.Consume(w.handle)
	if err != nil {
		return fmt.Errorf("start consuming: %w", err)
	}
	opsServer.SetReady(true)

	g.Go(func() error {
		<-ctx.Done()
		// Stop fetching; unacknowledged messages are redelivered to another
		// replica (or this one after restart) once AckWait passes.
		consuming.Stop()
		return nil
	})

	err = g.Wait()
	slog.Info("stopped")
	return err
}
