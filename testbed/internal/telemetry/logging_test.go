package telemetry

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"testing"

	"go.opentelemetry.io/otel/trace"
)

func TestLoggerAddsTraceIDs(t *testing.T) {
	traceID, _ := trace.TraceIDFromHex("0af7651916cd43dd8448eb211c80319c")
	spanID, _ := trace.SpanIDFromHex("b7ad6b7169203331")
	withSpan := trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: traceID, SpanID: spanID, TraceFlags: trace.FlagsSampled,
	}))

	tests := []struct {
		name        string
		ctx         context.Context
		wantTraceID any
	}{
		{"context with span", withSpan, traceID.String()},
		{"context without span", context.Background(), nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var buf bytes.Buffer
			// .With exercises WithAttrs: the trace IDs must survive it.
			logger := newLogger(&buf, "test-service", slog.LevelInfo).With("component", "x")
			logger.InfoContext(tt.ctx, "hello")

			var line map[string]any
			if err := json.Unmarshal(buf.Bytes(), &line); err != nil {
				t.Fatalf("log line is not JSON: %v\n%s", err, buf.String())
			}
			if line["service"] != "test-service" {
				t.Errorf("service = %v, want test-service", line["service"])
			}
			if line["trace_id"] != tt.wantTraceID {
				t.Errorf("trace_id = %v, want %v", line["trace_id"], tt.wantTraceID)
			}
		})
	}
}

func TestParseLevel(t *testing.T) {
	tests := map[string]slog.Level{
		"debug": slog.LevelDebug,
		"WARN":  slog.LevelWarn,
		"error": slog.LevelError,
		"":      slog.LevelInfo,
		"bogus": slog.LevelInfo,
	}
	for in, want := range tests {
		if got := parseLevel(in); got != want {
			t.Errorf("parseLevel(%q) = %v, want %v", in, got, want)
		}
	}
}
