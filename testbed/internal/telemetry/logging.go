package telemetry

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"

	"go.opentelemetry.io/otel/trace"
)

// setupLogging installs a JSON logger on stdout as the slog default. Every
// line carries the service name; lines logged with a context that has an
// active span also carry trace_id and span_id.
func setupLogging(service string) {
	slog.SetDefault(newLogger(os.Stdout, service, parseLevel(os.Getenv("LOG_LEVEL"))))
}

func newLogger(w io.Writer, service string, level slog.Level) *slog.Logger {
	base := slog.NewJSONHandler(w, &slog.HandlerOptions{Level: level})
	return slog.New(traceHandler{base}).With("service", service)
}

func parseLevel(s string) slog.Level {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

// traceHandler adds the active span's IDs to each record. This is the link
// between logs and traces: given a slow trace you can find its log lines, and
// given an error log line you can open its trace. It only works for calls that
// pass a context (slog.InfoContext and friends), so request-path code always
// uses the *Context variants.
type traceHandler struct {
	slog.Handler
}

func (h traceHandler) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.AddAttrs(
			slog.String("trace_id", sc.TraceID().String()),
			slog.String("span_id", sc.SpanID().String()),
		)
	}
	return h.Handler.Handle(ctx, r)
}

// WithAttrs and WithGroup must re-wrap, otherwise logger.With(...) would
// return the inner handler and silently drop the trace IDs.
func (h traceHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return traceHandler{h.Handler.WithAttrs(attrs)}
}

func (h traceHandler) WithGroup(name string) slog.Handler {
	return traceHandler{h.Handler.WithGroup(name)}
}
