package main

import (
	"errors"
	"testing"
)

func TestDecodeEvent(t *testing.T) {
	tests := []struct {
		name          string
		data          string
		wantMalformed bool
	}{
		{"valid", `{"order_id":"o-1","customer_id":"c-1","sku":"SKU-0001","quantity":2}`, false},
		{"not json", `not json`, true},
		{"missing order id", `{"customer_id":"c-1"}`, true},
		{"missing customer id", `{"order_id":"o-1"}`, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			evt, err := decodeEvent([]byte(tt.data))
			// A malformed event must be recognisable as such, because the
			// worker terminates those instead of retrying them.
			if got := errors.Is(err, errMalformed); got != tt.wantMalformed {
				t.Fatalf("malformed = %v (err %v), want %v", got, err, tt.wantMalformed)
			}
			if !tt.wantMalformed && evt.OrderID != "o-1" {
				t.Errorf("OrderID = %q, want o-1", evt.OrderID)
			}
		})
	}
}
