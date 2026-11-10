package main

import (
	"testing"

	shopv1 "github.com/harshdeodhar/sre-agent/testbed/gen/shop/v1"
)

func TestValidateCreateOrder(t *testing.T) {
	tests := []struct {
		name    string
		req     *shopv1.CreateOrderRequest
		wantErr bool
	}{
		{"valid", &shopv1.CreateOrderRequest{CustomerId: "c", Sku: "s", Quantity: 1}, false},
		{"max quantity", &shopv1.CreateOrderRequest{CustomerId: "c", Sku: "s", Quantity: 100}, false},
		{"missing customer", &shopv1.CreateOrderRequest{Sku: "s", Quantity: 1}, true},
		{"missing sku", &shopv1.CreateOrderRequest{CustomerId: "c", Quantity: 1}, true},
		{"zero quantity", &shopv1.CreateOrderRequest{CustomerId: "c", Sku: "s"}, true},
		{"negative quantity", &shopv1.CreateOrderRequest{CustomerId: "c", Sku: "s", Quantity: -1}, true},
		{"too many", &shopv1.CreateOrderRequest{CustomerId: "c", Sku: "s", Quantity: 101}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateCreateOrder(tt.req)
			if (err != nil) != tt.wantErr {
				t.Errorf("validateCreateOrder() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestIsUUID(t *testing.T) {
	tests := map[string]bool{
		"0af76519-16cd-43dd-8448-eb211c80319c": true,
		"0AF76519-16CD-43DD-8448-EB211C80319C": true,
		"":                                     false,
		"not-a-uuid":                           false,
		"0af7651916cd43dd8448eb211c80319c":     false, // no hyphens
		"0af76519-16cd-43dd-8448-eb211c80319":  false, // one short
		"0af76519-16cd-43dd-8448-eb211c80319g": false, // not hex
		"0af76519x16cd-43dd-8448-eb211c80319c": false, // wrong separator
	}
	for in, want := range tests {
		if got := isUUID(in); got != want {
			t.Errorf("isUUID(%q) = %v, want %v", in, got, want)
		}
	}
}
