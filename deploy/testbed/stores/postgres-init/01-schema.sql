-- Schema for the shop database. Runs once, when Postgres initialises an empty
-- data directory (files in /docker-entrypoint-initdb.d run in name order).

CREATE TABLE stock (
    sku           text    PRIMARY KEY,
    name          text    NOT NULL,
    -- The CHECK is a second line of defence: inventory-service already
    -- decrements with "WHERE available >= quantity", but the database refuses
    -- negative stock even if a bug gets past that.
    available     integer NOT NULL CHECK (available >= 0),
    restock_level integer NOT NULL,
    price_cents   bigint  NOT NULL
);

CREATE TABLE orders (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id text        NOT NULL,
    sku         text        NOT NULL REFERENCES stock (sku),
    quantity    integer     NOT NULL CHECK (quantity > 0),
    total_cents bigint      NOT NULL,
    status      text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Serves "list a customer's most recent orders" (order-service
-- ListCustomerOrders): the index is already in the order the query wants, so
-- Postgres reads the first 20 entries and stops.
CREATE INDEX orders_customer_created_idx ON orders (customer_id, created_at DESC);

-- One row per notified order. The primary key is what makes the worker's
-- insert idempotent under at-least-once delivery.
CREATE TABLE notifications (
    order_id    uuid        PRIMARY KEY,
    customer_id text        NOT NULL,
    channel     text        NOT NULL,
    sent_at     timestamptz NOT NULL DEFAULT now()
);
