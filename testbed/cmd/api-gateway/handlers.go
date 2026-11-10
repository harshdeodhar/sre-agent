package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
)

type gateway struct {
	orders    shopv1.OrderServiceClient
	inventory shopv1.InventoryServiceClient
	timeout   time.Duration
}

func (g *gateway) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/products/{sku}", g.getProduct)
	mux.HandleFunc("POST /api/orders", g.createOrder)
	mux.HandleFunc("GET /api/orders/{id}", g.getOrder)
	mux.HandleFunc("GET /api/customers/{id}/orders", g.listCustomerOrders)
	return mux
}

// JSON shapes of the public API. They are separate from the protobuf messages
// so the internal RPC contract can change without changing the public one.
type productJSON struct {
	SKU        string `json:"sku"`
	Name       string `json:"name"`
	Available  int32  `json:"available"`
	PriceCents int64  `json:"price_cents"`
}

type orderJSON struct {
	ID         string    `json:"id"`
	CustomerID string    `json:"customer_id"`
	SKU        string    `json:"sku"`
	Quantity   int32     `json:"quantity"`
	TotalCents int64     `json:"total_cents"`
	Status     string    `json:"status"`
	CreatedAt  time.Time `json:"created_at"`
}

type createOrderJSON struct {
	CustomerID string `json:"customer_id"`
	SKU        string `json:"sku"`
	Quantity   int32  `json:"quantity"`
}

type errorJSON struct {
	Error string `json:"error"`
}

func toOrderJSON(o *shopv1.Order) orderJSON {
	return orderJSON{
		ID:         o.GetId(),
		CustomerID: o.GetCustomerId(),
		SKU:        o.GetSku(),
		Quantity:   o.GetQuantity(),
		TotalCents: o.GetTotalCents(),
		Status:     o.GetStatus(),
		CreatedAt:  o.GetCreatedAt().AsTime(),
	}
}

func (g *gateway) getProduct(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), g.timeout)
	defer cancel()

	res, err := g.inventory.GetStock(ctx, &shopv1.GetStockRequest{Sku: r.PathValue("sku")})
	if err != nil {
		writeUpstreamError(ctx, w, "inventory-service", err)
		return
	}
	writeJSON(ctx, w, http.StatusOK, productJSON{
		SKU: res.GetSku(), Name: res.GetName(), Available: res.GetAvailable(), PriceCents: res.GetPriceCents(),
	})
}

func (g *gateway) createOrder(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), g.timeout)
	defer cancel()

	var body createOrderJSON
	// Cap the body so a client cannot make the gateway buffer arbitrary input.
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<10)).Decode(&body); err != nil {
		writeJSON(ctx, w, http.StatusBadRequest, errorJSON{Error: "request body must be a JSON order"})
		return
	}

	res, err := g.orders.CreateOrder(ctx, &shopv1.CreateOrderRequest{
		CustomerId: body.CustomerID, Sku: body.SKU, Quantity: body.Quantity,
	})
	if err != nil {
		writeUpstreamError(ctx, w, "order-service", err)
		return
	}
	writeJSON(ctx, w, http.StatusCreated, toOrderJSON(res.GetOrder()))
}

func (g *gateway) getOrder(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), g.timeout)
	defer cancel()

	res, err := g.orders.GetOrder(ctx, &shopv1.GetOrderRequest{Id: r.PathValue("id")})
	if err != nil {
		writeUpstreamError(ctx, w, "order-service", err)
		return
	}
	writeJSON(ctx, w, http.StatusOK, toOrderJSON(res.GetOrder()))
}

func (g *gateway) listCustomerOrders(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), g.timeout)
	defer cancel()

	res, err := g.orders.ListCustomerOrders(ctx, &shopv1.ListCustomerOrdersRequest{CustomerId: r.PathValue("id")})
	if err != nil {
		writeUpstreamError(ctx, w, "order-service", err)
		return
	}
	orders := make([]orderJSON, 0, len(res.GetOrders()))
	for _, o := range res.GetOrders() {
		orders = append(orders, toOrderJSON(o))
	}
	writeJSON(ctx, w, http.StatusOK, orders)
}

// httpStatus maps a gRPC status code from an upstream to the HTTP status the
// client sees. The split that matters for alerting is 4xx versus 5xx: 4xx
// means the request was wrong, 5xx means the system failed.
func httpStatus(code codes.Code) int {
	switch code {
	case codes.OK:
		return http.StatusOK
	case codes.InvalidArgument, codes.OutOfRange:
		return http.StatusBadRequest
	case codes.NotFound:
		return http.StatusNotFound
	case codes.AlreadyExists, codes.FailedPrecondition:
		// FailedPrecondition is how the services report "not enough stock".
		return http.StatusConflict
	case codes.Canceled:
		// 499 "client closed request" (nginx's convention): the caller went
		// away, which is not a server failure.
		return 499
	case codes.DeadlineExceeded:
		return http.StatusGatewayTimeout
	case codes.Unavailable, codes.ResourceExhausted:
		return http.StatusServiceUnavailable
	default:
		return http.StatusInternalServerError
	}
}

// writeUpstreamError converts a gRPC error into an HTTP error response and
// logs server-side failures with the upstream that produced them.
func writeUpstreamError(ctx context.Context, w http.ResponseWriter, upstream string, err error) {
	st := status.Convert(err)
	code := httpStatus(st.Code())
	if code >= http.StatusInternalServerError {
		slog.ErrorContext(ctx, "upstream call failed",
			"upstream", upstream, "grpc_code", st.Code().String(), "err", st.Message())
	}
	writeJSON(ctx, w, code, errorJSON{Error: st.Message()})
}

func writeJSON(ctx context.Context, w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		// Headers are already sent, so all that is left is to record it.
		slog.WarnContext(ctx, "write response failed", "err", err)
	}
}
