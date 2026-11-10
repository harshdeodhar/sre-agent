// Package events defines the order event stream shared by its publisher
// (order-service) and its consumer (notification-worker).
package events

import (
	"context"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"

	"github.com/harshdeodhar/sre-agent/testbed/internal/retry"
)

const (
	// StreamName is the JetStream stream holding order events.
	StreamName = "ORDERS"
	// SubjectOrderCreated is published once per stored order.
	SubjectOrderCreated = "orders.created"
	// ConsumerName is the durable consumer used by notification-worker. All
	// worker replicas share it, so JetStream spreads messages across them.
	ConsumerName = "notification-worker"
)

// OrderCreated is the payload of SubjectOrderCreated, encoded as JSON.
type OrderCreated struct {
	OrderID    string    `json:"order_id"`
	CustomerID string    `json:"customer_id"`
	SKU        string    `json:"sku"`
	Quantity   int32     `json:"quantity"`
	TotalCents int64     `json:"total_cents"`
	CreatedAt  time.Time `json:"created_at"`
}

// Connect connects to NATS, retrying for up to timeout while the server comes
// up. After that the client reconnects on its own forever.
func Connect(ctx context.Context, url, clientName string, timeout time.Duration) (*nats.Conn, error) {
	var nc *nats.Conn
	err := retry.Until(ctx, "nats", timeout, func(context.Context) error {
		var err error
		nc, err = nats.Connect(url,
			nats.Name(clientName),
			nats.MaxReconnects(-1),
			nats.ReconnectWait(time.Second),
		)
		return err
	})
	if err != nil {
		return nil, err
	}
	return nc, nil
}

// EnsureStream creates the ORDERS stream if it does not exist. Both the
// publisher and the consumer call it, so neither depends on the other having
// started first; the call is idempotent.
func EnsureStream(ctx context.Context, js jetstream.JetStream) (jetstream.Stream, error) {
	stream, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:     StreamName,
		Subjects: []string{"orders.>"},
		Storage:  jetstream.FileStorage,
		// Bounded on both age and size: a stuck consumer produces visible lag,
		// not an ever-growing disk.
		MaxAge:   time.Hour,
		MaxBytes: 64 << 20,
	})
	if err != nil {
		return nil, fmt.Errorf("ensure stream %s: %w", StreamName, err)
	}
	return stream, nil
}

// InjectTrace writes the trace context of ctx into message headers, so the
// consumer's spans join the trace of the request that produced the event.
// NATS has no automatic instrumentation here; this is the same W3C
// traceparent header HTTP and gRPC carry, set by hand.
func InjectTrace(ctx context.Context, h nats.Header) {
	otel.GetTextMapPropagator().Inject(ctx, headerCarrier(h))
}

// ExtractTrace returns ctx carrying the trace context found in message
// headers, if any.
func ExtractTrace(ctx context.Context, h nats.Header) context.Context {
	return otel.GetTextMapPropagator().Extract(ctx, headerCarrier(h))
}

// headerCarrier adapts NATS headers to OpenTelemetry's propagation interface.
//
// OpenTelemetry ships propagation.HeaderCarrier for net/http headers, and
// nats.Header has the same underlying type, so that carrier compiles here. It
// is wrong, though. HTTP header names are case-insensitive, and that carrier
// writes and reads the canonical HTTP form "Traceparent". NATS header names
// are case-sensitive, and nats-server rewrites this particular header to the
// lower-case "traceparent" the W3C specification asks for (server/msgtrace.go,
// genHeaderMapIfTraceHeadersPresent). The consumer's lookup of "Traceparent"
// then misses, silently, and every consumed message starts a new trace.
// nats.Header's own Get and Set match names exactly, so the header is written
// as "traceparent" and read as "traceparent".
type headerCarrier nats.Header

var _ propagation.TextMapCarrier = headerCarrier{}

func (c headerCarrier) Get(key string) string { return nats.Header(c).Get(key) }

func (c headerCarrier) Set(key, value string) { nats.Header(c).Set(key, value) }

func (c headerCarrier) Keys() []string {
	keys := make([]string, 0, len(c))
	for k := range c {
		keys = append(keys, k)
	}
	return keys
}
