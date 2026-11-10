// Package telemetry sets up the three signals every service emits: structured
// logs (stdout, JSON), traces (OTLP to the collector) and metrics (Prometheus,
// scraped from the ops port).
package telemetry

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

// TracerName identifies spans created by hand in this module.
const TracerName = "github.com/harshdeodhar/sre-agent/testbed"

// Tracer returns the tracer used for hand-written spans.
func Tracer() trace.Tracer {
	return otel.Tracer(TracerName)
}

// Setup installs the default logger and the global tracer provider for the
// named service. The returned function flushes buffered spans and must be
// called before the process exits.
//
// Tracing is configured through the standard OpenTelemetry environment
// variables (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_TRACES_SAMPLER, ...), which the
// SDK reads itself. If no endpoint is set, tracing is left as a no-op so the
// binaries and their tests run without a collector.
func Setup(ctx context.Context, service string) (shutdown func(context.Context) error, err error) {
	setupLogging(service)

	// W3C trace context is what carries a trace across HTTP, gRPC and (by hand,
	// see the events package) NATS message headers.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))

	// Export failures (collector down, for example) must not take the service
	// down or spam at error level; they are a telemetry problem, not a
	// request-serving problem.
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) {
		slog.Warn("opentelemetry error", "err", err)
	}))

	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		slog.Info("tracing disabled: OTEL_EXPORTER_OTLP_ENDPOINT is not set")
		return func(context.Context) error { return nil }, nil
	}

	// The exporter connects lazily, so this does not block or fail when the
	// collector is not up yet.
	exporter, err := otlptracegrpc.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("create OTLP trace exporter: %w", err)
	}

	res, err := resource.New(ctx, resource.WithAttributes(semconv.ServiceName(service)))
	if err != nil {
		return nil, fmt.Errorf("create trace resource: %w", err)
	}

	// The batch processor exports off the request path. Its queue is bounded,
	// so a dead collector costs dropped spans, not memory.
	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(provider)

	return provider.Shutdown, nil
}
