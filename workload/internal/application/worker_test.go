package application

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

type queueStub struct {
	dispatchErr, claimErr, completeErr error
	completed                          bool
}

func (q *queueStub) Dispatch(context.Context) error { return q.dispatchErr }
func (q *queueStub) Claim(context.Context, string, time.Duration) (domain.Job, error) {
	return domain.Job{ID: "test", Fixture: "tiny-v1"}, q.claimErr
}
func (q *queueStub) Complete(_ context.Context, _ domain.Job, report domain.Report) error {
	q.completed = true
	if report.Tokens != 11 {
		return errors.New("incorrect report")
	}
	return q.completeErr
}

func TestProcessOne(t *testing.T) {
	for _, tc := range []struct {
		name                             string
		q                                queueStub
		wantJob, wantError, wantComplete bool
	}{
		{"success", queueStub{}, true, false, true},
		{"idle", queueStub{claimErr: domain.ErrNotFound}, false, false, false},
		{"dispatch fails", queueStub{dispatchErr: errors.New("dispatch")}, false, true, false},
		{"claim fails", queueStub{claimErr: errors.New("claim")}, false, true, false},
		{"stale", queueStub{completeErr: domain.ErrStale}, false, true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			job, err := ProcessOne(context.Background(), &tc.q, "test")
			if (job != nil) != tc.wantJob || (err != nil) != tc.wantError || tc.q.completed != tc.wantComplete {
				t.Fatalf("job=%v err=%v completed=%v", job, err, tc.q.completed)
			}
		})
	}
}

func TestProcessOneReportsPollBeforeLongProcessing(t *testing.T) {
	queue := &queueStub{}
	var observed error
	polled := false
	job, err := ProcessOneWithAnalyzerAndPoll(context.Background(), queue, "test", func(string) (domain.Report, error) {
		if !polled || observed != nil {
			t.Fatal("poll was not reported before analysis")
		}
		return domain.Report{Tokens: 11}, nil
	}, func(err error) { polled, observed = true, err })
	if err != nil || job == nil || !polled || observed != nil {
		t.Fatalf("job=%v err=%v polled=%v observed=%v", job, err, polled, observed)
	}
}

func TestProcessOneReportsFailedPoll(t *testing.T) {
	want := errors.New("private database error")
	queue := &queueStub{claimErr: want}
	var observed error
	_, err := ProcessOneWithAnalyzerAndPoll(context.Background(), queue, "test", domain.Analyze, func(err error) {
		observed = err
	})
	if !errors.Is(err, want) || !errors.Is(observed, want) {
		t.Fatalf("err=%v observed=%v", err, observed)
	}
}
