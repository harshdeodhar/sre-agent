package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
	"github.com/harshdeodhar/sre-agent/testbed/internal/rpc"
	"github.com/harshdeodhar/sre-agent/testbed/internal/store"
)

var (
	cacheRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "inventory_cache_requests_total",
		Help: "Stock cache lookups, by result (hit, miss, error).",
	}, []string{"result"})
	stockReservations = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "inventory_reservations_total",
		Help: "Stock reservation attempts, by outcome.",
	}, []string{"outcome"})
	restockedItems = promauto.NewCounter(prometheus.CounterOpts{
		Name: "inventory_restocked_items_total",
		Help: "Stock rows topped up by the periodic restock.",
	})
)

type server struct {
	shopv1.UnimplementedInventoryServiceServer

	pool     *pgxpool.Pool
	cache    *redis.Client
	cacheTTL time.Duration
}

// stockItem is the cached form of a stock row.
type stockItem struct {
	Name       string `json:"name"`
	Available  int32  `json:"available"`
	PriceCents int64  `json:"price_cents"`
}

func cacheKey(sku string) string {
	return "stock:" + sku
}

// GetStock is cache-aside: try Redis, fall back to Postgres, then populate
// Redis. A cached stock level can be up to cacheTTL stale; that is acceptable
// because it is only informational. ReserveStock is the authoritative check.
func (s *server) GetStock(ctx context.Context, req *shopv1.GetStockRequest) (*shopv1.GetStockResponse, error) {
	sku := req.GetSku()
	if sku == "" {
		return nil, status.Error(codes.InvalidArgument, "sku is required")
	}

	item, ok := s.cacheGet(ctx, sku)
	if !ok {
		var err error
		item, err = s.loadStock(ctx, sku)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, status.Errorf(codes.NotFound, "unknown sku %q", sku)
		}
		if err != nil {
			return nil, rpc.DependencyError("load stock", err)
		}
		s.cacheSet(ctx, sku, item)
	}

	return &shopv1.GetStockResponse{
		Sku: sku, Name: item.Name, Available: item.Available, PriceCents: item.PriceCents,
	}, nil
}

// cacheGet returns the cached item, if there is one. A cache failure is
// logged and counted but otherwise treated as a miss: the cache must never be
// the reason a read fails.
func (s *server) cacheGet(ctx context.Context, sku string) (stockItem, bool) {
	raw, err := s.cache.Get(ctx, cacheKey(sku)).Bytes()
	switch {
	case errors.Is(err, redis.Nil):
		cacheRequests.WithLabelValues("miss").Inc()
		return stockItem{}, false
	case err != nil:
		cacheRequests.WithLabelValues("error").Inc()
		slog.WarnContext(ctx, "cache read failed, falling back to database", "sku", sku, "err", err)
		return stockItem{}, false
	}

	var item stockItem
	if err := json.Unmarshal(raw, &item); err != nil {
		cacheRequests.WithLabelValues("error").Inc()
		slog.WarnContext(ctx, "cache entry is not valid JSON, falling back to database", "sku", sku, "err", err)
		return stockItem{}, false
	}
	cacheRequests.WithLabelValues("hit").Inc()
	return item, true
}

func (s *server) cacheSet(ctx context.Context, sku string, item stockItem) {
	raw, err := json.Marshal(item)
	if err == nil {
		err = s.cache.Set(ctx, cacheKey(sku), raw, s.cacheTTL).Err()
	}
	if err != nil {
		slog.WarnContext(ctx, "cache write failed", "sku", sku, "err", err)
	}
}

func (s *server) loadStock(ctx context.Context, sku string) (stockItem, error) {
	var item stockItem
	err := store.Observe(ctx, "stock.get", func(ctx context.Context) error {
		return s.pool.QueryRow(ctx,
			`SELECT name, available, price_cents FROM stock WHERE sku = $1`, sku,
		).Scan(&item.Name, &item.Available, &item.PriceCents)
	})
	return item, err
}

// ReserveStock decrements stock in a single conditional UPDATE. Doing the
// check and the decrement in one statement means two concurrent reservations
// can never both take the last unit; there is no read-then-write race to lock
// around.
func (s *server) ReserveStock(ctx context.Context, req *shopv1.ReserveStockRequest) (*shopv1.ReserveStockResponse, error) {
	sku, qty := req.GetSku(), req.GetQuantity()
	if sku == "" || qty < 1 {
		stockReservations.WithLabelValues("invalid").Inc()
		return nil, status.Error(codes.InvalidArgument, "sku and a positive quantity are required")
	}

	var price int64
	var remaining int32
	err := store.Observe(ctx, "stock.reserve", func(ctx context.Context) error {
		return s.pool.QueryRow(ctx,
			`UPDATE stock SET available = available - $2
			 WHERE sku = $1 AND available >= $2
			 RETURNING price_cents, available`, sku, qty,
		).Scan(&price, &remaining)
	})

	if errors.Is(err, pgx.ErrNoRows) {
		// The UPDATE matched nothing: either the SKU does not exist or there
		// is not enough stock. Look once more to tell the caller which.
		if _, lookupErr := s.loadStock(ctx, sku); errors.Is(lookupErr, pgx.ErrNoRows) {
			stockReservations.WithLabelValues("unknown_sku").Inc()
			return nil, status.Errorf(codes.NotFound, "unknown sku %q", sku)
		}
		stockReservations.WithLabelValues("insufficient_stock").Inc()
		return nil, status.Errorf(codes.FailedPrecondition, "insufficient stock for %q", sku)
	}
	if err != nil {
		stockReservations.WithLabelValues("error").Inc()
		return nil, rpc.DependencyError("reserve stock", err)
	}
	stockReservations.WithLabelValues("ok").Inc()

	// Drop the cached level so the next read sees the new one. Best effort: if
	// this fails the entry simply expires after cacheTTL.
	if err := s.cache.Del(ctx, cacheKey(sku)).Err(); err != nil {
		slog.WarnContext(ctx, "cache invalidation failed", "sku", sku, "err", err)
	}

	return &shopv1.ReserveStockResponse{PriceCents: price, Remaining: remaining}, nil
}

// restockLoop tops up low stock on a timer, standing in for supplier
// deliveries. Without it the steady order traffic would drain every SKU and the
// testbed's normal state would be "out of stock".
//
// Every replica runs this loop. That is safe because the UPDATE is idempotent:
// a second replica finds nothing left below the threshold.
func (s *server) restockLoop(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.restock(ctx)
		}
	}
}

func (s *server) restock(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	var restocked int64
	err := store.Observe(ctx, "stock.restock", func(ctx context.Context) error {
		tag, err := s.pool.Exec(ctx,
			`UPDATE stock SET available = restock_level WHERE available < restock_level / 5`)
		restocked = tag.RowsAffected()
		return err
	})
	if err != nil {
		slog.ErrorContext(ctx, "restock failed", "err", err)
		return
	}
	if restocked > 0 {
		restockedItems.Add(float64(restocked))
		slog.InfoContext(ctx, "restocked low items", "items", restocked)
	}
}
