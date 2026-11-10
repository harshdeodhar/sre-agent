package rpc

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestDependencyError(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want codes.Code
	}{
		{"deadline", context.DeadlineExceeded, codes.DeadlineExceeded},
		{"wrapped deadline", fmt.Errorf("acquire: %w", context.DeadlineExceeded), codes.DeadlineExceeded},
		{"cancelled", context.Canceled, codes.Canceled},
		{"anything else", errors.New("connection refused"), codes.Internal},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := status.Code(DependencyError("store order", tt.err)); got != tt.want {
				t.Errorf("code = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestIsCallerError(t *testing.T) {
	callerFault := []codes.Code{codes.InvalidArgument, codes.NotFound, codes.FailedPrecondition, codes.Canceled}
	serverFault := []codes.Code{codes.Internal, codes.Unavailable, codes.DeadlineExceeded, codes.Unknown}
	for _, c := range callerFault {
		if !IsCallerError(c) {
			t.Errorf("IsCallerError(%v) = false, want true", c)
		}
	}
	for _, c := range serverFault {
		if IsCallerError(c) {
			t.Errorf("IsCallerError(%v) = true, want false", c)
		}
	}
}
