package main

import (
	"context"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
)

// Invalid requests must be rejected before the server touches Redis or
// Postgres. The server here has neither, so reaching a dependency would panic.
func TestRequestValidation(t *testing.T) {
	s := &server{}
	ctx := context.Background()

	tests := []struct {
		name string
		call func() error
	}{
		{"GetStock without sku", func() error {
			_, err := s.GetStock(ctx, &shopv1.GetStockRequest{})
			return err
		}},
		{"ReserveStock without sku", func() error {
			_, err := s.ReserveStock(ctx, &shopv1.ReserveStockRequest{Quantity: 1})
			return err
		}},
		{"ReserveStock with zero quantity", func() error {
			_, err := s.ReserveStock(ctx, &shopv1.ReserveStockRequest{Sku: "SKU-0001"})
			return err
		}},
		{"ReserveStock with negative quantity", func() error {
			_, err := s.ReserveStock(ctx, &shopv1.ReserveStockRequest{Sku: "SKU-0001", Quantity: -2})
			return err
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := status.Code(tt.call()); got != codes.InvalidArgument {
				t.Errorf("code = %v, want InvalidArgument", got)
			}
		})
	}
}

func TestCacheKey(t *testing.T) {
	if got := cacheKey("SKU-0001"); got != "stock:SKU-0001" {
		t.Errorf("cacheKey = %q", got)
	}
}
