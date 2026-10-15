package application

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sync"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

// Envelope carries only versioned identity. Trace provenance remains in the DB.
type Envelope struct {
	Version int    `json:"version"`
	JobID   string `json:"job_id"`
}

var jobID = regexp.MustCompile(`^[0-9a-f]{32}$`)

func DecodeEnvelope(body []byte) (Envelope, error) {
	var envelope Envelope
	if len(body) > 1024 {
		return envelope, errors.New("invalid delivery envelope")
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&envelope) != nil || envelope.Version != 1 || !jobID.MatchString(envelope.JobID) {
		return Envelope{}, errors.New("invalid delivery envelope")
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return Envelope{}, errors.New("trailing delivery content")
	}
	return envelope, nil
}

type Delivery interface {
	Body() []byte
	Ack(context.Context) error
	Retry(context.Context, time.Duration) error
}
type Notifications interface {
	Publish(context.Context, Envelope) error
	Receive(context.Context) (Delivery, error)
}
type Objects interface {
	Put(context.Context, domain.Job, domain.Report) (domain.ReportObject, error)
}
type CloudState interface {
	Get(context.Context, string) (domain.Job, error)
	ClaimPublication(context.Context) (string, int, error)
	MarkPublished(context.Context, string, int) error
	ClaimJob(context.Context, string, string, time.Duration) (domain.Job, error)
	CompleteObject(context.Context, domain.Job, domain.Report, domain.ReportObject) error
}
type receipt struct {
	token    int
	delivery Delivery
}
type CloudQueue struct {
	State    CloudState
	Queue    Notifications
	Objects  Objects
	mu       sync.Mutex
	receipts map[string]receipt
	// Set once before starting the independent dispatcher/consumer loops.
	BackgroundDispatch bool
}

func (q *CloudQueue) Dispatch(ctx context.Context) error {
	if q.BackgroundDispatch {
		return nil
	}
	return q.PublishPending(ctx)
}

func (q *CloudQueue) PublishPending(ctx context.Context) error {
	id, token, err := q.State.ClaimPublication(ctx)
	if errors.Is(err, domain.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if !jobID.MatchString(id) {
		return errors.New("invalid outbox identifier")
	}
	if err = q.Queue.Publish(ctx, Envelope{Version: 1, JobID: id}); err != nil {
		return err
	}
	return q.State.MarkPublished(ctx, id, token)
}
func (q *CloudQueue) Claim(ctx context.Context, revision string, lease time.Duration) (domain.Job, error) {
	delivery, err := q.Queue.Receive(ctx)
	if err != nil {
		return domain.Job{}, err
	}
	envelope, err := DecodeEnvelope(delivery.Body())
	if err != nil {
		_ = delivery.Retry(ctx, 30*time.Second)
		return domain.Job{}, err
	}
	j, err := q.State.Get(ctx, envelope.JobID)
	if err != nil {
		_ = delivery.Retry(ctx, 30*time.Second)
		return domain.Job{}, err
	}
	if j.State == "succeeded" || j.State == "failed" {
		if err = delivery.Ack(ctx); err != nil {
			return domain.Job{}, err
		}
		return domain.Job{}, domain.ErrNotFound
	}
	j, err = q.State.ClaimJob(ctx, envelope.JobID, revision, lease)
	if err != nil {
		_ = delivery.Retry(ctx, lease)
		return domain.Job{}, err
	}
	q.mu.Lock()
	if q.receipts == nil {
		q.receipts = make(map[string]receipt)
	}
	q.receipts[j.ID] = receipt{j.Attempt, delivery}
	q.mu.Unlock()
	return j, nil
}
func (q *CloudQueue) take(j domain.Job) (Delivery, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	r, ok := q.receipts[j.ID]
	if !ok || r.token != j.Attempt {
		return nil, domain.ErrStale
	}
	delete(q.receipts, j.ID)
	return r.delivery, nil
}
func (q *CloudQueue) Complete(ctx context.Context, j domain.Job, report domain.Report) error {
	delivery, err := q.take(j)
	if err != nil {
		return err
	}
	object, err := q.Objects.Put(ctx, j, report)
	if err != nil {
		_ = delivery.Retry(ctx, 30*time.Second)
		return err
	}
	if err = q.State.CompleteObject(ctx, j, report, object); err != nil {
		_ = delivery.Retry(ctx, 30*time.Second)
		return err
	}
	return delivery.Ack(ctx)
}
func (q *CloudQueue) Abandon(ctx context.Context, j domain.Job) error {
	delivery, err := q.take(j)
	if err != nil {
		return err
	}
	return delivery.Retry(ctx, 30*time.Second)
}

// ResultContent names immutable objects by job, fenced attempt and actual bytes.
func ResultContent(j domain.Job, report domain.Report) (string, []byte, error) {
	if !jobID.MatchString(j.ID) || j.Attempt < 1 {
		return "", nil, errors.New("invalid result identity")
	}
	data, err := json.Marshal(report)
	if err != nil {
		return "", nil, err
	}
	return fmt.Sprintf("reports/%s/attempt-%d/%s.json", j.ID, j.Attempt, domain.Hash(data)), data, nil
}
