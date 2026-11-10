package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/harshdeodhar/sre-agent/testbed/internal/events"
	"github.com/harshdeodhar/sre-agent/testbed/internal/store"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

var (
	processed = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "notifications_processed_total",
		Help: "Order events handled, by outcome (ok, error, malformed).",
	}, []string{"outcome"})

	processingTime = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "notification_processing_seconds",
		Help:    "Time to handle one order event.",
		Buckets: prometheus.DefBuckets,
	})

	// End-to-end lag: from the order being stored to its notification being
	// sent. This is the number a customer would feel. The buckets run to ten
	// minutes because a backlog is measured in minutes, not milliseconds.
	deliveryLag = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "notification_delivery_lag_seconds",
		Help:    "Delay between an order being created and its notification being sent.",
		Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600},
	})
)

// errMalformed marks an event that can never be processed, however often it
// is redelivered.
var errMalformed = errors.New("malformed event")

type worker struct {
	pool           *pgxpool.Pool
	sendDelay      time.Duration
	processTimeout time.Duration
	retryDelay     time.Duration
}

// handle processes one message and tells JetStream what to do with it:
// acknowledge on success, terminate if it can never succeed, otherwise ask for
// redelivery after a delay.
func (w *worker) handle(msg jetstream.Msg) {
	// Continue the trace started by the HTTP request that created the order.
	ctx := events.ExtractTrace(context.Background(), msg.Headers())
	ctx, span := telemetry.Tracer().Start(ctx, "process "+events.SubjectOrderCreated,
		trace.WithSpanKind(trace.SpanKindConsumer))
	defer span.End()

	ctx, cancel := context.WithTimeout(ctx, w.processTimeout)
	defer cancel()

	start := time.Now()
	err := w.process(ctx, msg.Data())
	processingTime.Observe(time.Since(start).Seconds())

	var ackErr error
	switch {
	case err == nil:
		processed.WithLabelValues("ok").Inc()
		ackErr = msg.Ack()
	case errors.Is(err, errMalformed):
		processed.WithLabelValues("malformed").Inc()
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		slog.ErrorContext(ctx, "dropping malformed order event", "err", err)
		ackErr = msg.Term()
	default:
		processed.WithLabelValues("error").Inc()
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		slog.ErrorContext(ctx, "processing order event failed, will retry", "err", err)
		ackErr = msg.NakWithDelay(w.retryDelay)
	}
	if ackErr != nil {
		// If the ack is lost, JetStream redelivers after AckWait. The insert
		// in process is idempotent, so a duplicate delivery is harmless.
		slog.WarnContext(ctx, "acknowledging message failed", "err", ackErr)
	}
}

func (w *worker) process(ctx context.Context, data []byte) error {
	evt, err := decodeEvent(data)
	if err != nil {
		return err
	}

	// Simulated call to an email/SMS provider.
	select {
	case <-time.After(w.sendDelay):
	case <-ctx.Done():
		return fmt.Errorf("send notification: %w", ctx.Err())
	}

	// At-least-once delivery means the same event can arrive twice. The
	// primary key on order_id plus ON CONFLICT DO NOTHING makes recording it
	// idempotent.
	err = store.Observe(ctx, "notifications.insert", func(ctx context.Context) error {
		_, err := w.pool.Exec(ctx,
			`INSERT INTO notifications (order_id, customer_id, channel)
			 VALUES ($1::uuid, $2, 'email')
			 ON CONFLICT (order_id) DO NOTHING`,
			evt.OrderID, evt.CustomerID)
		return err
	})
	if err != nil {
		return fmt.Errorf("record notification: %w", err)
	}

	deliveryLag.Observe(time.Since(evt.CreatedAt).Seconds())
	slog.DebugContext(ctx, "notification sent", "order_id", evt.OrderID, "customer_id", evt.CustomerID)
	return nil
}

func decodeEvent(data []byte) (events.OrderCreated, error) {
	var evt events.OrderCreated
	if err := json.Unmarshal(data, &evt); err != nil {
		return evt, fmt.Errorf("%w: %w", errMalformed, err)
	}
	if evt.OrderID == "" || evt.CustomerID == "" {
		return evt, fmt.Errorf("%w: order_id and customer_id are required", errMalformed)
	}
	return evt, nil
}
