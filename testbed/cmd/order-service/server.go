package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
	"github.com/harshdeodhar/sre-agent/testbed/internal/events"
	"github.com/harshdeodhar/sre-agent/testbed/internal/rpc"
	"github.com/harshdeodhar/sre-agent/testbed/internal/store"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

var (
	ordersCreated = promauto.NewCounter(prometheus.CounterOpts{
		Name: "orders_created_total",
		Help: "Orders stored successfully.",
	})
	eventsPublished = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "order_events_published_total",
		Help: "Attempts to publish an orders.created event, by outcome.",
	}, []string{"outcome"})
)

const (
	maxQuantity     = 100
	orderColumns    = `id::text, customer_id, sku, quantity, total_cents, status, created_at`
	customerPageLen = 20
)

type server struct {
	shopv1.UnimplementedOrderServiceServer

	pool             *pgxpool.Pool
	inventory        shopv1.InventoryServiceClient
	js               jetstream.JetStream
	inventoryTimeout time.Duration
	publishTimeout   time.Duration
}

func (s *server) CreateOrder(ctx context.Context, req *shopv1.CreateOrderRequest) (*shopv1.CreateOrderResponse, error) {
	if err := validateCreateOrder(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}

	// Step 1: reserve stock. This is the authoritative check; the cached stock
	// level the client may have read earlier is only a hint.
	reserveCtx, cancel := context.WithTimeout(ctx, s.inventoryTimeout)
	reserved, err := s.inventory.ReserveStock(reserveCtx, &shopv1.ReserveStockRequest{
		Sku: req.GetSku(), Quantity: req.GetQuantity(),
	})
	cancel()
	if err != nil {
		// Keep inventory-service's status code: "not enough stock" must stay a
		// caller error, and "unavailable" must stay a server error.
		st := status.Convert(err)
		return nil, status.Errorf(st.Code(), "reserve stock: %s", st.Message())
	}

	// Step 2: store the order.
	//
	// Known simplification: if this insert fails, the reservation from step 1
	// is not released. A production system would use a saga or a reservation
	// with an expiry. Here inventory-service restocks periodically, so the
	// leak is bounded, and the missing compensation is not what the testbed is
	// meant to exercise.
	order := &shopv1.Order{
		CustomerId: req.GetCustomerId(),
		Sku:        req.GetSku(),
		Quantity:   req.GetQuantity(),
		TotalCents: reserved.GetPriceCents() * int64(req.GetQuantity()),
		Status:     "confirmed",
	}
	var createdAt time.Time
	err = store.Observe(ctx, "orders.insert", func(ctx context.Context) error {
		return s.pool.QueryRow(ctx,
			`INSERT INTO orders (customer_id, sku, quantity, total_cents, status)
			 VALUES ($1, $2, $3, $4, $5)
			 RETURNING id::text, created_at`,
			order.GetCustomerId(), order.GetSku(), order.GetQuantity(), order.GetTotalCents(), order.GetStatus(),
		).Scan(&order.Id, &createdAt)
	})
	if err != nil {
		return nil, rpc.DependencyError("store order", err)
	}
	order.CreatedAt = timestamppb.New(createdAt)
	ordersCreated.Inc()

	// Step 3: announce it. The order is already committed, so a publish
	// failure does not fail the request (see publish).
	s.publish(ctx, order)

	return &shopv1.CreateOrderResponse{Order: order}, nil
}

// publish sends the orders.created event and waits for JetStream to
// acknowledge it, within publishTimeout.
//
// Known simplification: the database write and the publish are not atomic. If
// NATS is unreachable the order exists but its event is lost (and the failure
// is logged and counted). The standard fix is a transactional outbox; it is
// left out to keep the service small.
func (s *server) publish(ctx context.Context, order *shopv1.Order) {
	ctx, span := telemetry.Tracer().Start(ctx, "publish "+events.SubjectOrderCreated,
		trace.WithSpanKind(trace.SpanKindProducer))
	defer span.End()

	data, err := json.Marshal(events.OrderCreated{
		OrderID:    order.GetId(),
		CustomerID: order.GetCustomerId(),
		SKU:        order.GetSku(),
		Quantity:   order.GetQuantity(),
		TotalCents: order.GetTotalCents(),
		CreatedAt:  order.GetCreatedAt().AsTime(),
	})
	if err == nil {
		msg := &nats.Msg{Subject: events.SubjectOrderCreated, Data: data, Header: nats.Header{}}
		events.InjectTrace(ctx, msg.Header)

		pubCtx, cancel := context.WithTimeout(ctx, s.publishTimeout)
		defer cancel()
		// The order ID as message ID lets JetStream drop a duplicate if a
		// publish is ever retried.
		_, err = s.js.PublishMsg(pubCtx, msg, jetstream.WithMsgID(order.GetId()))
	}

	if err != nil {
		eventsPublished.WithLabelValues("error").Inc()
		span.RecordError(err)
		span.SetStatus(otelcodes.Error, err.Error())
		slog.ErrorContext(ctx, "publish order event failed; order is stored but no notification will be sent",
			"order_id", order.GetId(), "err", err)
		return
	}
	eventsPublished.WithLabelValues("ok").Inc()
}

func (s *server) GetOrder(ctx context.Context, req *shopv1.GetOrderRequest) (*shopv1.GetOrderResponse, error) {
	if !isUUID(req.GetId()) {
		return nil, status.Error(codes.InvalidArgument, "order id must be a UUID")
	}

	var orders []*shopv1.Order
	err := store.Observe(ctx, "orders.get", func(ctx context.Context) error {
		rows, err := s.pool.Query(ctx, `SELECT `+orderColumns+` FROM orders WHERE id = $1::uuid`, req.GetId())
		if err != nil {
			return err
		}
		orders, err = pgx.CollectRows(rows, scanOrder)
		return err
	})
	if err != nil {
		return nil, rpc.DependencyError("load order", err)
	}
	if len(orders) == 0 {
		return nil, status.Error(codes.NotFound, "order not found")
	}
	return &shopv1.GetOrderResponse{Order: orders[0]}, nil
}

func (s *server) ListCustomerOrders(ctx context.Context, req *shopv1.ListCustomerOrdersRequest) (*shopv1.ListCustomerOrdersResponse, error) {
	if req.GetCustomerId() == "" {
		return nil, status.Error(codes.InvalidArgument, "customer_id is required")
	}

	var orders []*shopv1.Order
	// Served by the index orders_customer_created_idx (customer_id, created_at
	// DESC). Without it this is a scan of the whole orders table.
	err := store.Observe(ctx, "orders.list_by_customer", func(ctx context.Context) error {
		rows, err := s.pool.Query(ctx,
			`SELECT `+orderColumns+` FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMIT $2`,
			req.GetCustomerId(), customerPageLen)
		if err != nil {
			return err
		}
		orders, err = pgx.CollectRows(rows, scanOrder)
		return err
	})
	if err != nil {
		return nil, rpc.DependencyError("list orders", err)
	}
	return &shopv1.ListCustomerOrdersResponse{Orders: orders}, nil
}

func scanOrder(row pgx.CollectableRow) (*shopv1.Order, error) {
	var (
		o         shopv1.Order
		createdAt time.Time
	)
	if err := row.Scan(&o.Id, &o.CustomerId, &o.Sku, &o.Quantity, &o.TotalCents, &o.Status, &createdAt); err != nil {
		return nil, err
	}
	o.CreatedAt = timestamppb.New(createdAt)
	return &o, nil
}

func validateCreateOrder(req *shopv1.CreateOrderRequest) error {
	switch {
	case req.GetCustomerId() == "":
		return errors.New("customer_id is required")
	case req.GetSku() == "":
		return errors.New("sku is required")
	case req.GetQuantity() < 1 || req.GetQuantity() > maxQuantity:
		return errors.New("quantity must be between 1 and 100")
	}
	return nil
}

// isUUID reports whether s has the canonical 8-4-4-4-12 hex form. Checking
// here turns a malformed ID into a 400 instead of a database error.
func isUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i, c := range s {
		switch i {
		case 8, 13, 18, 23:
			if c != '-' {
				return false
			}
		default:
			isHex := (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
			if !isHex {
				return false
			}
		}
	}
	return true
}
