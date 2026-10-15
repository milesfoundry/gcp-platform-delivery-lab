package postgres

import (
	"context"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

// Integration tests require an isolated disposable database, never a shared DB.
func TestDurability(t *testing.T) {
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("set TEST_DATABASE_URL to a disposable database")
	}
	ctx := context.Background()
	s, err := Open(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Pool.Close()
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Pool.Exec(ctx, "TRUNCATE jobs CASCADE"); err != nil {
		t.Fatal(err)
	}
	req := domain.Request{Fixture: "tiny-v1", Algorithm: "tokens-v1", TraceParent: "00-11111111111111111111111111111111-2222222222222222-01"}
	var wg sync.WaitGroup
	ids := make(chan string, 12)
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			j, e := s.Submit(ctx, "same", req)
			if e != nil {
				t.Error(e)
				return
			}
			ids <- j.ID
		}()
	}
	wg.Wait()
	close(ids)
	var id string
	for got := range ids {
		if id != "" && id != got {
			t.Fatal("duplicate job")
		}
		id = got
	}
	if id == "" {
		t.Fatal("no jobs submitted")
	}
	repeat := req
	repeat.TraceParent = "00-33333333333333333333333333333333-4444444444444444-01"
	repeated, err := s.Submit(ctx, "same", repeat)
	if err != nil || repeated.TraceParent != req.TraceParent {
		t.Fatal("idempotent repeat overwrote submission trace provenance")
	}
	if _, err = s.Submit(ctx, "same", domain.Request{Fixture: "batch-v1", Algorithm: "tokens-v1"}); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflict: %v", err)
	}
	for range 2 {
		if err = s.Dispatch(ctx); err != nil {
			t.Fatal(err)
		}
	}
	first, err := s.Claim(ctx, "baseline", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if first.TraceParent != req.TraceParent {
		t.Fatal("trace parent lost during durable dispatch")
	}
	if _, err = s.Claim(ctx, "duplicate", time.Minute); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("active lease claimed: %v", err)
	}
	if _, err = s.Pool.Exec(ctx, "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=$1", id); err != nil {
		t.Fatal(err)
	}
	second, err := s.Claim(ctx, "recovery", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	report, err := domain.Analyze(req.Fixture)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Complete(ctx, first, report); !errors.Is(err, ErrStale) {
		t.Fatalf("stale writer accepted: %v", err)
	}
	if err = s.Complete(ctx, second, report); err != nil {
		t.Fatal(err)
	}
	if err = s.Complete(ctx, second, report); !errors.Is(err, ErrStale) {
		t.Fatalf("duplicate completion accepted: %v", err)
	}
	done, err := s.Get(ctx, id)
	if err != nil || done.State != "succeeded" || done.Attempt != 2 {
		t.Fatalf("unexpected result: %+v %v", done, err)
	}
	if _, err = s.Submit(ctx, "exhausted", req); err != nil {
		t.Fatal(err)
	}
	if err = s.Dispatch(ctx); err != nil {
		t.Fatal(err)
	}
	for range 5 {
		j, e := s.Claim(ctx, "crash", time.Minute)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = s.Pool.Exec(ctx, "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=$1", j.ID); e != nil {
			t.Fatal(e)
		}
	}
	if _, err = s.Claim(ctx, "too-many", time.Minute); !errors.Is(err, domain.ErrNotFound) {
		t.Fatal(err)
	}
	var state string
	if err = s.Pool.QueryRow(ctx, "SELECT state FROM jobs WHERE idempotency_key='exhausted'").Scan(&state); err != nil || state != "failed" {
		t.Fatalf("exhaustion not committed: %s %v", state, err)
	}
}
