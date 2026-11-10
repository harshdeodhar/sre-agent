-- Seed data. The load generator assumes these ID ranges (SKU_COUNT and
-- CUSTOMER_COUNT in deploy/loadgen): SKU-0001..SKU-0200, cust-00001..cust-05000.

INSERT INTO stock (sku, name, available, restock_level, price_cents)
SELECT 'SKU-' || lpad(i::text, 4, '0'),
       'Product ' || i,
       500,
       500,
       500 + (i * 37) % 9500
FROM generate_series(1, 200) AS i;

-- Order history: 300,000 past orders spread over 5,000 customers and 90 days.
-- A table this size is what makes the database behave like a database: an
-- indexed lookup takes well under a millisecond, while a query that has to
-- scan the table takes tens of milliseconds. An empty table would hide that
-- difference completely.
INSERT INTO orders (customer_id, sku, quantity, total_cents, status, created_at)
SELECT g.customer_id, g.sku, g.quantity, g.quantity * s.price_cents, 'confirmed', g.created_at
FROM (
    SELECT 'cust-' || lpad((1 + floor(random() * 5000))::int::text, 5, '0') AS customer_id,
           'SKU-'  || lpad((1 + floor(random() * 200))::int::text, 4, '0')  AS sku,
           (1 + floor(random() * 3))::int                                   AS quantity,
           now() - random() * interval '90 days'                            AS created_at
    FROM generate_series(1, 300000)
) AS g
JOIN stock AS s USING (sku);

-- Refresh planner statistics now, so the first queries after startup are
-- planned with knowledge of the seeded data.
ANALYZE;
