// Package rpc builds gRPC servers and clients with the same instrumentation
// and connection settings in every service.
package rpc

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"time"

	grpcprom "github.com/grpc-ecosystem/go-grpc-middleware/providers/prometheus"
	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/status"
)

// Standard gRPC metrics: grpc_server_handled_total, grpc_server_handling_seconds
// and the grpc_client_* equivalents, labelled by service, method and status
// code. Having both sides matters for diagnosis: a caller's view of latency
// includes the network and connection queueing, the callee's does not.
var (
	serverMetrics = grpcprom.NewServerMetrics(
		grpcprom.WithServerHandlingTimeHistogram(grpcprom.WithHistogramBuckets(prometheus.DefBuckets)),
	)
	clientMetrics = grpcprom.NewClientMetrics(
		grpcprom.WithClientHandlingTimeHistogram(grpcprom.WithHistogramBuckets(prometheus.DefBuckets)),
	)
)

func init() {
	prometheus.MustRegister(serverMetrics, clientMetrics)
}

// NewServer returns a gRPC server with tracing, metrics and error logging.
func NewServer() *grpc.Server {
	return grpc.NewServer(
		grpc.StatsHandler(otelgrpc.NewServerHandler()),
		grpc.ChainUnaryInterceptor(serverMetrics.UnaryServerInterceptor(), logErrors),

		// gRPC keeps one long-lived HTTP/2 connection per backend, and a client
		// only re-resolves DNS when a connection breaks. Without a maximum
		// connection age, a client would never learn about replicas added by a
		// scale-up. Closing connections every 30s (gracefully: in-flight RPCs
		// get 10s to finish) forces periodic re-resolution.
		grpc.KeepaliveParams(keepalive.ServerParameters{
			MaxConnectionAge:      30 * time.Second,
			MaxConnectionAgeGrace: 10 * time.Second,
		}),
	)
}

// Serve listens on addr and serves until ctx is cancelled, then stops
// gracefully. Call it after all services are registered on srv.
func Serve(ctx context.Context, srv *grpc.Server, addr string) error {
	lis, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", addr, err)
	}

	// Create every method/code series at zero so rate() queries and dashboards
	// have data before the first error ever happens.
	serverMetrics.InitializeMetrics(srv)

	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(lis) }()

	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		// GracefulStop waits for in-flight RPCs with no upper bound, so cap it.
		stopped := make(chan struct{})
		go func() { srv.GracefulStop(); close(stopped) }()
		select {
		case <-stopped:
		case <-time.After(10 * time.Second):
			srv.Stop()
		}
		return nil
	}
}

// Dial returns a client connection to target, which should be a
// "dns:///name:port" address of a headless Kubernetes Service.
//
// A normal ClusterIP Service load-balances per TCP connection. gRPC multiplexes
// every call over one connection, so through a ClusterIP all traffic from a
// client would land on a single replica. A headless Service returns the pod IPs
// themselves, and round_robin spreads calls across them.
//
// Connections are plaintext: this is a local testbed with no service mesh, and
// TLS between services is not what the project is about.
func Dial(target string) (*grpc.ClientConn, error) {
	conn, err := grpc.NewClient(target,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultServiceConfig(`{"loadBalancingConfig":[{"round_robin":{}}]}`),
		grpc.WithStatsHandler(otelgrpc.NewClientHandler()),
		grpc.WithChainUnaryInterceptor(clientMetrics.UnaryClientInterceptor()),
	)
	if err != nil {
		return nil, fmt.Errorf("create gRPC client for %s: %w", target, err)
	}
	return conn, nil
}

// logErrors logs every failed RPC with its status code. Failures caused by the
// caller's input are warnings; everything else is an error.
func logErrors(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	start := time.Now()
	resp, err := handler(ctx, req)
	if err != nil {
		code := status.Code(err)
		level := slog.LevelError
		if IsCallerError(code) {
			level = slog.LevelWarn
		}
		slog.Log(ctx, level, "rpc failed",
			"method", info.FullMethod,
			"code", code.String(),
			"duration_ms", time.Since(start).Milliseconds(),
			"err", err,
		)
	}
	return resp, err
}

// DependencyError converts a failure of a dependency (database, cache, broker)
// into a gRPC status. A deadline or cancellation keeps its own code, so a
// caller can tell "too slow" from "broken"; anything else is Internal.
func DependencyError(op string, err error) error {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return status.Errorf(codes.DeadlineExceeded, "%s: %v", op, err)
	case errors.Is(err, context.Canceled):
		return status.Errorf(codes.Canceled, "%s: %v", op, err)
	default:
		return status.Errorf(codes.Internal, "%s: %v", op, err)
	}
}

// IsCallerError reports whether code means the request itself was at fault
// (bad input, unknown ID, business rule), as opposed to the service or one of
// its dependencies failing.
func IsCallerError(code codes.Code) bool {
	switch code {
	case codes.InvalidArgument, codes.NotFound, codes.AlreadyExists,
		codes.FailedPrecondition, codes.OutOfRange, codes.Canceled:
		return true
	default:
		return false
	}
}
