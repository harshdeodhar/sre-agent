package main

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"net/http"
)

// Operation names, used as the "op" metric label.
const (
	opGetProduct     = "get_product"
	opCreateOrder    = "create_order"
	opGetOrder       = "get_order"
	opListCustOrders = "list_customer_orders"
)

// The traffic mix, in percent. Browsing dominates, as in a real shop; a
// minority of requests write.
const (
	pctGetProduct  = 70
	pctCreateOrder = 20
	pctGetOrder    = 5
	// The remaining 5% list a customer's orders.
)

// request is one HTTP call the generator will make.
type request struct {
	op     string
	method string
	path   string
	body   []byte
}

// picker chooses the next request. It is not safe for concurrent use; the
// generator calls it from a single goroutine.
type picker struct {
	rng           *rand.Rand
	skus          *rand.Zipf
	customerCount int
}

func newPicker(skuCount, customerCount int) *picker {
	// A random seed per process: the generator is background traffic, not a
	// reproducible experiment, and replicas should not send identical streams.
	rng := rand.New(rand.NewPCG(rand.Uint64(), rand.Uint64())) //nolint:gosec // load shaping, not security
	return &picker{
		rng: rng,
		// Zipf-distributed SKU popularity: a few products get most of the
		// traffic, as in a real catalogue. That is what makes the cache
		// worthwhile and gives it a realistic hit rate. With s=1.1 over 200
		// SKUs the most popular product gets roughly a fifth of requests.
		skus:          rand.NewZipf(rng, 1.1, 1, uint64(skuCount-1)), //nolint:gosec // skuCount validated positive
		customerCount: customerCount,
	}
}

// opFor maps a roll in [0,100) to an operation according to the traffic mix.
func opFor(roll int) string {
	switch {
	case roll < pctGetProduct:
		return opGetProduct
	case roll < pctGetProduct+pctCreateOrder:
		return opCreateOrder
	case roll < pctGetProduct+pctCreateOrder+pctGetOrder:
		return opGetOrder
	default:
		return opListCustOrders
	}
}

// next builds the next request. recentOrder returns the ID of an order the
// generator created earlier, if it has one.
func (p *picker) next(recentOrder func(n int) (string, bool)) request {
	op := opFor(p.rng.IntN(100))

	if op == opGetOrder {
		id, ok := recentOrder(p.rng.IntN(1 << 16))
		if ok {
			return request{op: op, method: http.MethodGet, path: "/api/orders/" + id}
		}
		// Nothing created yet (just started): browse instead.
		op = opGetProduct
	}

	switch op {
	case opCreateOrder:
		body, _ := json.Marshal(map[string]any{ // cannot fail: strings and ints only
			"customer_id": p.customerID(),
			"sku":         p.sku(),
			"quantity":    1 + p.rng.IntN(3),
		})
		return request{op: op, method: http.MethodPost, path: "/api/orders", body: body}
	case opListCustOrders:
		return request{op: op, method: http.MethodGet, path: "/api/customers/" + p.customerID() + "/orders"}
	default:
		return request{op: opGetProduct, method: http.MethodGet, path: "/api/products/" + p.sku()}
	}
}

// sku returns a product ID in the form used by the seed data (SKU-0001...).
func (p *picker) sku() string {
	return fmt.Sprintf("SKU-%04d", p.skus.Uint64()+1)
}

// customerID returns a customer in the form used by the seed data
// (cust-00001...), uniformly distributed.
func (p *picker) customerID() string {
	return fmt.Sprintf("cust-%05d", 1+p.rng.IntN(p.customerCount))
}
