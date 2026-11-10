package main

import (
	"encoding/json"
	"net/http"
	"regexp"
	"testing"
)

func TestOpFor(t *testing.T) {
	tests := []struct {
		roll int
		want string
	}{
		{0, opGetProduct},
		{69, opGetProduct},
		{70, opCreateOrder},
		{89, opCreateOrder},
		{90, opGetOrder},
		{94, opGetOrder},
		{95, opListCustOrders},
		{99, opListCustOrders},
	}
	for _, tt := range tests {
		if got := opFor(tt.roll); got != tt.want {
			t.Errorf("opFor(%d) = %q, want %q", tt.roll, got, tt.want)
		}
	}
}

func TestClassify(t *testing.T) {
	tests := []struct {
		status int
		want   string
	}{
		{http.StatusOK, outcomeOK},
		{http.StatusCreated, outcomeOK},
		{http.StatusNotFound, outcomeClientError},
		{http.StatusConflict, outcomeClientError},
		{http.StatusInternalServerError, outcomeServerError},
		{http.StatusGatewayTimeout, outcomeServerError},
	}
	for _, tt := range tests {
		if got := classify(tt.status); got != tt.want {
			t.Errorf("classify(%d) = %q, want %q", tt.status, got, tt.want)
		}
	}
}

// Every generated request must be one the gateway and the seed data accept:
// SKUs and customers in the seeded ranges, quantities the service allows.
func TestPickerGeneratesValidRequests(t *testing.T) {
	const skuCount, customerCount = 200, 5000
	p := newPicker(skuCount, customerCount)

	productPath := regexp.MustCompile(`^/api/products/SKU-(0[0-9]{3})$`)
	customerPath := regexp.MustCompile(`^/api/customers/cust-[0-9]{5}/orders$`)
	noOrders := func(int) (string, bool) { return "", false }
	oneOrder := func(int) (string, bool) { return "order-1", true }

	seen := map[string]int{}
	for i := range 5000 {
		recent := noOrders
		if i%2 == 0 {
			recent = oneOrder
		}
		r := p.next(recent)
		seen[r.op]++

		switch r.op {
		case opGetProduct:
			if !productPath.MatchString(r.path) {
				t.Fatalf("unexpected product path %q", r.path)
			}
		case opListCustOrders:
			if !customerPath.MatchString(r.path) {
				t.Fatalf("unexpected customer path %q", r.path)
			}
		case opGetOrder:
			if r.path != "/api/orders/order-1" {
				t.Fatalf("unexpected order path %q", r.path)
			}
		case opCreateOrder:
			var body struct {
				CustomerID string `json:"customer_id"`
				SKU        string `json:"sku"`
				Quantity   int    `json:"quantity"`
			}
			if err := json.Unmarshal(r.body, &body); err != nil {
				t.Fatalf("create order body is not JSON: %v", err)
			}
			if r.method != http.MethodPost || body.Quantity < 1 || body.Quantity > 3 ||
				body.CustomerID == "" || body.SKU == "" {
				t.Fatalf("unexpected create order request: %s %s", r.method, r.body)
			}
		default:
			t.Fatalf("unknown op %q", r.op)
		}
	}

	for _, op := range []string{opGetProduct, opCreateOrder, opGetOrder, opListCustOrders} {
		if seen[op] == 0 {
			t.Errorf("op %q never generated in 5000 requests", op)
		}
	}
}

func TestRecentOrders(t *testing.T) {
	var r recentOrders
	if _, ok := r.pick(3); ok {
		t.Fatal("pick on an empty ring returned an ID")
	}

	r.add("a")
	r.add("b")
	if id, _ := r.pick(0); id != "a" {
		t.Errorf("pick(0) = %q, want a", id)
	}
	if id, _ := r.pick(3); id != "b" {
		t.Errorf("pick(3) = %q, want b (3 mod 2 = 1)", id)
	}

	// Overfilling keeps the ring at its fixed size and overwrites the oldest.
	for range len(r.ids) {
		r.add("new")
	}
	if r.size != len(r.ids) {
		t.Errorf("size = %d, want %d", r.size, len(r.ids))
	}
	for i := range len(r.ids) {
		if id, _ := r.pick(i); id != "new" {
			t.Fatalf("pick(%d) = %q, want new", i, id)
		}
	}
}
