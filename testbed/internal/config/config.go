// Package config reads service configuration from environment variables.
//
// All configuration is environment-based so that a config change is a change
// to the Deployment spec, which Kubernetes records as a new rollout revision.
// That is what makes "recent deploy history" and "rollback" meaningful later.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"time"
)

// Env reads typed values from the environment and remembers every problem it
// sees. Callers read all their settings, then check Err once. Reporting every
// bad key in a single message makes a misconfigured pod's crash log
// self-explanatory instead of a fix-one-find-the-next loop.
type Env struct {
	errs []error
}

// String returns the value of key, or def if it is unset or empty.
func (e *Env) String(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// Required returns the value of key and records an error if it is unset.
func (e *Env) Required(key string) string {
	v := os.Getenv(key)
	if v == "" {
		e.errs = append(e.errs, fmt.Errorf("%s is required", key))
	}
	return v
}

// Int returns key parsed as an integer, or def if it is unset.
func (e *Env) Int(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		e.errs = append(e.errs, fmt.Errorf("%s=%q is not an integer", key, v))
		return def
	}
	return n
}

// Float returns key parsed as a float, or def if it is unset.
func (e *Env) Float(key string, def float64) float64 {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		e.errs = append(e.errs, fmt.Errorf("%s=%q is not a number", key, v))
		return def
	}
	return f
}

// Duration returns key parsed with time.ParseDuration ("250ms", "30s"), or def
// if it is unset.
func (e *Env) Duration(key string, def time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		e.errs = append(e.errs, fmt.Errorf("%s=%q is not a duration", key, v))
		return def
	}
	return d
}

// Err returns every problem recorded so far, joined, or nil.
func (e *Env) Err() error {
	return errors.Join(e.errs...)
}
