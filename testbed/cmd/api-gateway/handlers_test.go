package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
)

func TestHTTPStatus(t *testing.T) {
	tests := []struct {
		code codes.Code
		want int
	}{
		{codes.OK, http.StatusOK},
		{codes.InvalidArgument, http.StatusBadRequest},
		{codes.NotFound, http.StatusNotFound},
		{codes.FailedPrecondition, http.StatusConflict},
		{codes.Canceled, 499},
		{codes.DeadlineExceeded, http.StatusGatewayTimeout},
		{codes.Unavailable, http.StatusServiceUnavailable},
		{codes.Internal, http.StatusInternalServerError},
		{codes.Unknown, http.StatusInternalServerError},
	}
	for _, tt := range tests {
		t.Run(tt.code.String(), func(t *testing.T) {
			if got := httpStatus(tt.code); got != tt.want {
				t.Errorf("httpStatus(%v) = %d, want %d", tt.code, got, tt.want)
			}
		})
	}
}

// fakeInventory returns a canned response or error for GetStock.
type fakeInventory struct {
	shopv1.InventoryServiceClient
	res *shopv1.GetStockResponse
	err error
}

func (f fakeInventory) GetStock(context.Context, *shopv1.GetStockRequest, ...grpc.CallOption) (*shopv1.GetStockResponse, error) {
	return f.res, f.err
}

// fakeOrders records the request CreateOrder was called with.
type fakeOrders struct {
	shopv1.OrderServiceClient
	got *shopv1.CreateOrderRequest
}

func (f *fakeOrders) CreateOrder(_ context.Context, req *shopv1.CreateOrderRequest, _ ...grpc.CallOption) (*shopv1.CreateOrderResponse, error) {
	f.got = req
	return &shopv1.CreateOrderResponse{Order: &shopv1.Order{Id: "o-1", Sku: req.GetSku(), Quantity: req.GetQuantity()}}, nil
}

func TestGetProduct(t *testing.T) {
	tests := []struct {
		name     string
		inv      fakeInventory
		wantCode int
		wantBody string
	}{
		{
			name:     "found",
			inv:      fakeInventory{res: &shopv1.GetStockResponse{Sku: "SKU-0001", Name: "Product 1", Available: 5, PriceCents: 999}},
			wantCode: http.StatusOK,
			wantBody: `"sku":"SKU-0001"`,
		},
		{
			name:     "unknown sku is the client's problem",
			inv:      fakeInventory{err: status.Error(codes.NotFound, "unknown sku")},
			wantCode: http.StatusNotFound,
			wantBody: "unknown sku",
		},
		{
			name:     "upstream down is a server error",
			inv:      fakeInventory{err: status.Error(codes.Unavailable, "connection refused")},
			wantCode: http.StatusServiceUnavailable,
			wantBody: "connection refused",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gw := &gateway{inventory: tt.inv, timeout: time.Second}
			rec := httptest.NewRecorder()
			gw.routes().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/products/SKU-0001", nil))

			if rec.Code != tt.wantCode {
				t.Errorf("status = %d, want %d", rec.Code, tt.wantCode)
			}
			if !strings.Contains(rec.Body.String(), tt.wantBody) {
				t.Errorf("body = %s, want it to contain %s", rec.Body.String(), tt.wantBody)
			}
		})
	}
}

func TestCreateOrder(t *testing.T) {
	orders := &fakeOrders{}
	gw := &gateway{orders: orders, timeout: time.Second}

	body := `{"customer_id":"cust-00001","sku":"SKU-0002","quantity":3}`
	rec := httptest.NewRecorder()
	gw.routes().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/orders", strings.NewReader(body)))

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body %s", rec.Code, rec.Body.String())
	}
	if orders.got.GetCustomerId() != "cust-00001" || orders.got.GetSku() != "SKU-0002" || orders.got.GetQuantity() != 3 {
		t.Errorf("upstream request = %v, want the fields from the JSON body", orders.got)
	}
	var got orderJSON
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("response is not JSON: %v", err)
	}
	if got.ID != "o-1" {
		t.Errorf("response id = %q, want o-1", got.ID)
	}
}

func TestCreateOrderRejectsMalformedBody(t *testing.T) {
	orders := &fakeOrders{}
	gw := &gateway{orders: orders, timeout: time.Second}

	rec := httptest.NewRecorder()
	gw.routes().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/orders", strings.NewReader("not json")))

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
	if orders.got != nil {
		t.Error("order-service was called for a malformed body")
	}
}
