package application

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

const testID = "12345678901234567890123456789012"

type testDelivery struct {
	body          []byte
	acks, retries int
	failAck       bool
}

func (d *testDelivery) Body() []byte { return d.body }
func (d *testDelivery) Ack(context.Context) error {
	d.acks++
	if d.failAck {
		return errors.New("ack outage")
	}
	return nil
}
func (d *testDelivery) Retry(context.Context, time.Duration) error { d.retries++; return nil }

type testQueue struct {
	delivery    *testDelivery
	publishes   int
	failPublish bool
}

func (q *testQueue) Publish(context.Context, Envelope) error {
	q.publishes++
	if q.failPublish {
		return errors.New("publish outage")
	}
	return nil
}
func (q *testQueue) Receive(context.Context) (Delivery, error) { return q.delivery, nil }

type testObjects struct {
	calls int
	fail  bool
}

func (o *testObjects) Put(_ context.Context, j domain.Job, r domain.Report) (domain.ReportObject, error) {
	o.calls++
	if o.fail {
		return domain.ReportObject{}, errors.New("object outage")
	}
	key, data, err := ResultContent(j, r)
	return domain.ReportObject{Provider: "gcs", Bucket: "report-test", Key: key, SHA256: domain.Hash(data)}, err
}

type testState struct {
	job                    domain.Job
	marks                  int
	token                  int
	failMark, failComplete bool
}

func (s *testState) Get(context.Context, string) (domain.Job, error) { return s.job, nil }
func (s *testState) ClaimPublication(context.Context) (string, int, error) {
	s.token++
	return s.job.ID, s.token, nil
}
func (s *testState) MarkPublished(context.Context, string, int) error {
	if s.failMark {
		return domain.ErrStale
	}
	s.marks++
	return nil
}
func (s *testState) ClaimJob(context.Context, string, string, time.Duration) (domain.Job, error) {
	s.job.Attempt++
	s.job.State = "running"
	return s.job, nil
}
func (s *testState) CompleteObject(_ context.Context, j domain.Job, r domain.Report, object domain.ReportObject) error {
	if s.failComplete {
		return domain.ErrStale
	}
	s.job.State = "succeeded"
	s.job.Object = &object
	return nil
}
func fixtureCloud() (*CloudQueue, *testState, *testQueue, *testObjects) {
	body, _ := json.Marshal(Envelope{1, testID})
	s := &testState{job: domain.Job{ID: testID, Fixture: "tiny-v1", Algorithm: "tokens-v1", State: "pending"}}
	q := &testQueue{delivery: &testDelivery{body: body}}
	o := &testObjects{}
	return &CloudQueue{State: s, Queue: q, Objects: o}, s, q, o
}

func TestCloudCrashWindows(t *testing.T) {
	ctx := context.Background()
	t.Run("publisher outage does not block existing delivery", func(t *testing.T) {
		q, s, n, o := fixtureCloud()
		q.BackgroundDispatch = true
		n.failPublish = true
		if q.PublishPending(ctx) == nil {
			t.Fatal("missing publish outage")
		}
		if _, err := ProcessOne(ctx, q, "source"); err != nil || s.job.State != "succeeded" || o.calls != 1 || n.delivery.acks != 1 {
			t.Fatal("publisher outage blocked consumption", err)
		}
	})
	t.Run("publish failure does not mark", func(t *testing.T) {
		q, s, n, _ := fixtureCloud()
		n.failPublish = true
		if q.Dispatch(ctx) == nil || s.marks != 0 {
			t.Fatal("failed publish marked")
		}
	})
	t.Run("publish mark failure permits duplicate", func(t *testing.T) {
		q, s, n, _ := fixtureCloud()
		s.failMark = true
		if q.Dispatch(ctx) == nil {
			t.Fatal("missing mark failure")
		}
		s.failMark = false
		if err := q.Dispatch(ctx); err != nil || n.publishes != 2 || s.marks != 1 {
			t.Fatal("duplicate retry missing")
		}
	})
	for _, failure := range []string{"object", "commit", "ack", "none"} {
		t.Run(failure, func(t *testing.T) {
			q, s, n, o := fixtureCloud()
			o.fail = failure == "object"
			s.failComplete = failure == "commit"
			n.delivery.failAck = failure == "ack"
			_, err := ProcessOne(ctx, q, "source")
			if (err != nil) != (failure != "none") {
				t.Fatalf("result %v", err)
			}
			if failure == "object" || failure == "commit" {
				if n.delivery.acks != 0 || s.job.State == "succeeded" || len(q.receipts) != 0 {
					t.Fatal("acked before durable commit")
				}
				return
			}
			if s.job.State != "succeeded" || s.job.Object == nil {
				t.Fatal("missing committed reference")
			}
			n.delivery.failAck = false
			_, err = ProcessOne(ctx, q, "source")
			if err != nil || o.calls != 1 || n.delivery.acks != 2 {
				t.Fatal("terminal duplicate recomputed")
			}
		})
	}
	t.Run("poison remains unacknowledged", func(t *testing.T) {
		q, _, n, _ := fixtureCloud()
		n.delivery.body = []byte(`{"version":9,"job_id":"invalid"}`)
		if _, err := q.Claim(ctx, "source", time.Second); err == nil || n.delivery.acks != 0 || n.delivery.retries != 1 {
			t.Fatal("poison acknowledged")
		}
	})
	t.Run("stale attempt cannot use new receipt", func(t *testing.T) {
		q, _, _, o := fixtureCloud()
		old, _ := q.Claim(ctx, "old", time.Second)
		_, _ = q.Claim(ctx, "new", time.Second)
		report, _ := domain.Analyze("tiny-v1")
		if !errors.Is(q.Complete(ctx, old, report), domain.ErrStale) || o.calls != 0 {
			t.Fatal("stale receipt used")
		}
	})
}
func TestEnvelopeAndObjectIdentity(t *testing.T) {
	for _, body := range []string{`{"version":1,"job_id":"` + testID + `","secret":"x"}`, `{"version":1,"job_id":"` + testID + `"} {}`, `{"version":0,"job_id":"` + testID + `"}`} {
		if _, err := DecodeEnvelope([]byte(body)); err == nil {
			t.Fatal("accepted invalid envelope")
		}
	}
	report, _ := domain.Analyze("tiny-v1")
	a, _, _ := ResultContent(domain.Job{ID: testID, Attempt: 1}, report)
	b, _, _ := ResultContent(domain.Job{ID: testID, Attempt: 2}, report)
	if a == b {
		t.Fatal("attempt object overwritten")
	}
}
