// Package store opens the Postgres connection pool and instruments queries.
package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/harshdeodhar/sre-agent/testbed/internal/config"
	"github.com/harshdeodhar/sre-agent/testbed/internal/retry"
	"github.com/harshdeodhar/sre-agent/testbed/internal/telemetry"
)

// Config is the Postgres connection configuration.
type Config struct {
	Host           string
	Port           int
	User           string
	Password       string
	Database       string
	MaxConns       int
	ConnectTimeout time.Duration
}

// ConfigFromEnv reads the DB_* environment variables.
func ConfigFromEnv(e *config.Env) Config {
	return Config{
		Host:           e.Required("DB_HOST"),
		Port:           e.Int("DB_PORT", 5432),
		User:           e.Required("DB_USER"),
		Password:       e.Required("DB_PASSWORD"),
		Database:       e.Required("DB_NAME"),
		MaxConns:       e.Int("DB_MAX_CONNS", 10),
		ConnectTimeout: e.Duration("DB_CONNECT_TIMEOUT", 30*time.Second),
	}
}

// Open creates the pool, waits until Postgres answers a ping, and registers
// pool metrics. The service name becomes the connection's application_name, so
// each service's sessions can be told apart in pg_stat_activity.
func Open(ctx context.Context, service string, cfg Config) (*pgxpool.Pool, error) {
	// Fields are set on the parsed config rather than formatted into a URL, so
	// the password never sits in a string that might be logged or need escaping.
	pc, err := pgxpool.ParseConfig("")
	if err != nil {
		return nil, fmt.Errorf("create pool config: %w", err)
	}
	pc.ConnConfig.Host = cfg.Host
	pc.ConnConfig.Port = uint16(cfg.Port) //nolint:gosec // a port from config; out-of-range fails at connect
	pc.ConnConfig.User = cfg.User
	pc.ConnConfig.Password = cfg.Password
	pc.ConnConfig.Database = cfg.Database
	pc.ConnConfig.ConnectTimeout = 5 * time.Second
	pc.ConnConfig.RuntimeParams["application_name"] = service
	pc.MaxConns = int32(cfg.MaxConns) //nolint:gosec // small positive integer from config

	pool, err := pgxpool.NewWithConfig(ctx, pc)
	if err != nil {
		return nil, fmt.Errorf("create pool: %w", err)
	}
	if err := retry.Until(ctx, "postgres", cfg.ConnectTimeout, pool.Ping); err != nil {
		pool.Close()
		return nil, err
	}

	registerPoolMetrics(pool)
	return pool, nil
}

// registerPoolMetrics exposes the pool's own counters. Acquired versus max
// connections, and how often and how long callers wait for a connection, are
// the direct evidence for pool exhaustion: queries can be fast in Postgres
// while requests are slow because they are queueing for a connection.
func registerPoolMetrics(pool *pgxpool.Pool) {
	gauge := func(name, help string, value func(*pgxpool.Stat) float64) {
		promauto.NewGaugeFunc(prometheus.GaugeOpts{Name: name, Help: help},
			func() float64 { return value(pool.Stat()) })
	}
	counter := func(name, help string, value func(*pgxpool.Stat) float64) {
		promauto.NewCounterFunc(prometheus.CounterOpts{Name: name, Help: help},
			func() float64 { return value(pool.Stat()) })
	}

	gauge("db_pool_max_connections", "Configured maximum size of the connection pool.",
		func(s *pgxpool.Stat) float64 { return float64(s.MaxConns()) })
	gauge("db_pool_acquired_connections", "Connections currently checked out by a query.",
		func(s *pgxpool.Stat) float64 { return float64(s.AcquiredConns()) })
	gauge("db_pool_idle_connections", "Open connections not currently in use.",
		func(s *pgxpool.Stat) float64 { return float64(s.IdleConns()) })
	counter("db_pool_acquires_total", "Successful connection acquisitions.",
		func(s *pgxpool.Stat) float64 { return float64(s.AcquireCount()) })
	counter("db_pool_empty_acquires_total", "Acquisitions that had to wait because the pool was empty.",
		func(s *pgxpool.Stat) float64 { return float64(s.EmptyAcquireCount()) })
	counter("db_pool_acquire_wait_seconds_total", "Total time spent waiting to acquire a connection.",
		func(s *pgxpool.Stat) float64 { return s.AcquireDuration().Seconds() })
	counter("db_pool_canceled_acquires_total", "Acquisitions abandoned because the caller's context ended first.",
		func(s *pgxpool.Stat) float64 { return float64(s.CanceledAcquireCount()) })
}

var queryDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
	Name:    "db_query_duration_seconds",
	Help:    "Time to run a named database operation, including waiting for a pooled connection.",
	Buckets: prometheus.DefBuckets,
}, []string{"query", "outcome"})

// Observe runs one named database operation inside a trace span and records
// its duration. The name is a fixed label chosen by the caller
// ("orders.insert"), never SQL text or parameters.
//
// The measured time is what the caller experiences, so it includes waiting for
// a connection from the pool as well as the query itself.
func Observe(ctx context.Context, query string, fn func(context.Context) error) error {
	ctx, span := telemetry.Tracer().Start(ctx, "db "+query, trace.WithSpanKind(trace.SpanKindClient))
	defer span.End()

	start := time.Now()
	err := fn(ctx)

	outcome := "ok"
	// "No rows" is a normal answer to a lookup, not a database failure.
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		outcome = "error"
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	queryDuration.WithLabelValues(query, outcome).Observe(time.Since(start).Seconds())
	return err
}
