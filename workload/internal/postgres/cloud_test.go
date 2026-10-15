package postgres

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

func TestCloudStateFences(t *testing.T) {
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("disposable database required")
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
	j, err := s.Submit(ctx, "cloud-state", domain.Request{Fixture: "tiny-v1", Algorithm: "tokens-v1"})
	if err != nil {
		t.Fatal(err)
	}
	id, token, err := s.ClaimPublication(ctx)
	if err != nil || id != j.ID {
		t.Fatal("missing publication lease")
	}
	if _, _, err = s.ClaimPublication(ctx); !errors.Is(err, domain.ErrNotFound) {
		t.Fatal("publication lease duplicated")
	}
	if _, err = s.Pool.Exec(ctx, "UPDATE outbox SET lease_until=now()-interval '1 second'"); err != nil {
		t.Fatal(err)
	}
	_, newToken, err := s.ClaimPublication(ctx)
	if err != nil || newToken <= token {
		t.Fatal("publication token did not advance")
	}
	if !errors.Is(s.MarkPublished(ctx, id, token), domain.ErrStale) {
		t.Fatal("stale publication marked")
	}
	if err = s.MarkPublished(ctx, id, newToken); err != nil {
		t.Fatal(err)
	}
	old, err := s.ClaimJob(ctx, j.ID, "old", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.ClaimJob(ctx, j.ID, "duplicate", time.Minute); !errors.Is(err, domain.ErrNotFound) {
		t.Fatal("active job leased twice")
	}
	if _, err = s.Pool.Exec(ctx, "UPDATE jobs SET lease_until=now()-interval '1 second'"); err != nil {
		t.Fatal(err)
	}
	current, err := s.ClaimJob(ctx, j.ID, "new", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	report, _ := domain.Analyze("tiny-v1")
	key, data, _ := application.ResultContent(old, report)
	object := domain.ReportObject{Provider: "s3", Bucket: "report-test", Key: key, SHA256: domain.Hash(data)}
	if !errors.Is(s.CompleteObject(ctx, old, report, object), domain.ErrStale) {
		t.Fatal("stale object selected")
	}
	key, data, _ = application.ResultContent(current, report)
	object.Key = key
	object.SHA256 = domain.Hash(data)
	if err = s.CompleteObject(ctx, current, report, object); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get(ctx, j.ID)
	if err != nil || got.State != "succeeded" || got.Object == nil || *got.Object != object {
		t.Fatal("selected object not persisted")
	}
}
