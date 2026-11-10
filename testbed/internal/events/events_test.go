package events

import (
	"context"
	"testing"

	"github.com/nats-io/nats.go"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// TestTraceContextRoundTrip checks that a trace context written into NATS
// headers by the publisher is found again by the consumer. With the wrong
// carrier this fails silently in production: the consumer simply starts a new
// trace, and the publisher's and consumer's spans are never joined.
func TestTraceContextRoundTrip(t *testing.T) {
	previous := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() { otel.SetTextMapPropagator(previous) })

	want := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    trace.TraceID{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10},
		SpanID:     trace.SpanID{0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18},
		TraceFlags: trace.FlagsSampled,
	})

	headers := nats.Header{}
	InjectTrace(trace.ContextWithSpanContext(context.Background(), want), headers)

	// NATS header names are case-sensitive, so the exact name matters: this is
	// the W3C name, in the lower case the specification uses.
	if got := headers.Get("traceparent"); got == "" {
		t.Fatalf("no traceparent header after InjectTrace; headers: %v", headers)
	}

	got := trace.SpanContextFromContext(ExtractTrace(context.Background(), headers))
	if got.TraceID() != want.TraceID() {
		t.Errorf("trace ID = %s, want %s", got.TraceID(), want.TraceID())
	}
	if got.SpanID() != want.SpanID() {
		t.Errorf("span ID = %s, want %s", got.SpanID(), want.SpanID())
	}
	if !got.IsRemote() {
		t.Error("extracted span context is not marked remote")
	}
	if !got.IsSampled() {
		t.Error("sampling decision was lost")
	}
}

func TestExtractTraceWithoutHeaders(t *testing.T) {
	previous := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() { otel.SetTextMapPropagator(previous) })

	// A message published by something that does not trace (or a nil header
	// map) must yield a context with no parent, not a panic.
	for name, headers := range map[string]nats.Header{"nil": nil, "empty": {}} {
		t.Run(name, func(t *testing.T) {
			got := trace.SpanContextFromContext(ExtractTrace(context.Background(), headers))
			if got.IsValid() {
				t.Errorf("got a valid span context %v from %s headers", got, name)
			}
		})
	}
}
