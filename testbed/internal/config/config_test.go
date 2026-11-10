package config

import (
	"strings"
	"testing"
	"time"
)

func TestEnvDefaultsAndParsing(t *testing.T) {
	t.Setenv("T_STR", "hello")
	t.Setenv("T_INT", "42")
	t.Setenv("T_FLOAT", "2.5")
	t.Setenv("T_DUR", "250ms")

	var e Env
	if got := e.String("T_STR", "def"); got != "hello" {
		t.Errorf("String = %q, want hello", got)
	}
	if got := e.String("T_UNSET", "def"); got != "def" {
		t.Errorf("String default = %q, want def", got)
	}
	if got := e.Int("T_INT", 1); got != 42 {
		t.Errorf("Int = %d, want 42", got)
	}
	if got := e.Int("T_UNSET", 7); got != 7 {
		t.Errorf("Int default = %d, want 7", got)
	}
	if got := e.Float("T_FLOAT", 1); got != 2.5 {
		t.Errorf("Float = %v, want 2.5", got)
	}
	if got := e.Duration("T_DUR", time.Second); got != 250*time.Millisecond {
		t.Errorf("Duration = %v, want 250ms", got)
	}
	if err := e.Err(); err != nil {
		t.Errorf("Err = %v, want nil", err)
	}
}

func TestEnvReportsEveryProblem(t *testing.T) {
	t.Setenv("T_INT", "ten")
	t.Setenv("T_DUR", "soon")
	t.Setenv("T_FLOAT", "lots")

	var e Env
	e.Int("T_INT", 1)
	e.Duration("T_DUR", time.Second)
	e.Float("T_FLOAT", 1)
	e.Required("T_MISSING")

	err := e.Err()
	if err == nil {
		t.Fatal("Err = nil, want an error naming all four keys")
	}
	for _, key := range []string{"T_INT", "T_DUR", "T_FLOAT", "T_MISSING"} {
		if !strings.Contains(err.Error(), key) {
			t.Errorf("error %q does not mention %s", err, key)
		}
	}
}
