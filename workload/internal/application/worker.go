// Package application defines use cases and their infrastructure-independent ports.
package application

import (
	"context"
	"errors"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

type JobReaderWriter interface {
	Submit(context.Context, string, domain.Request) (domain.Job, error)
	Get(context.Context, string) (domain.Job, error)
}

type WorkQueue interface {
	Dispatch(context.Context) error
	Claim(context.Context, string, time.Duration) (domain.Job, error)
	Complete(context.Context, domain.Job, domain.Report) error
}

// AttemptObserver is an optional instrumentation port. No SDK enters the use case.
type AttemptObserver interface {
	StartAttempt(context.Context, domain.Job) (context.Context, func(error))
	StartOperation(context.Context, string) (context.Context, func(error))
}

// ProcessOne returns nil when no eligible work exists. Cloud adapters must retain
// the same fencing contract while qualifying their own delivery semantics.
func ProcessOne(ctx context.Context, queue WorkQueue, revision string, observers ...AttemptObserver) (result *domain.Job, err error) {
	return ProcessOneWithAnalyzer(ctx, queue, revision, domain.Analyze, observers...)
}

// ProcessOneWithAnalyzer supports release variants without coupling use cases
// to experiment configuration or infrastructure SDKs.
func ProcessOneWithAnalyzer(ctx context.Context, queue WorkQueue, revision string, analyze func(string) (domain.Report, error), observers ...AttemptObserver) (result *domain.Job, err error) {
	return ProcessOneWithAnalyzerAndPoll(ctx, queue, revision, analyze, nil, observers...)
}

// ProcessOneWithAnalyzerAndPoll reports a successful dependency poll as soon as
// dispatch and claim complete, independently of subsequent job processing time.
func ProcessOneWithAnalyzerAndPoll(ctx context.Context, queue WorkQueue, revision string, analyze func(string) (domain.Report, error), observePoll func(error), observers ...AttemptObserver) (result *domain.Job, err error) {
	if err := queue.Dispatch(ctx); err != nil {
		if observePoll != nil {
			observePoll(err)
		}
		return nil, err
	}
	job, err := queue.Claim(ctx, revision, 30*time.Second)
	if errors.Is(err, domain.ErrNotFound) {
		if observePoll != nil {
			observePoll(nil)
		}
		return nil, nil
	}
	if err != nil {
		if observePoll != nil {
			observePoll(err)
		}
		return nil, err
	}
	if observePoll != nil {
		observePoll(nil)
	}
	defer func() {
		if err != nil {
			if abandoner, ok := queue.(interface {
				Abandon(context.Context, domain.Job) error
			}); ok {
				_ = abandoner.Abandon(ctx, job)
			}
		}
	}()
	var observer AttemptObserver
	if len(observers) > 0 {
		observer = observers[0]
	}
	if observer != nil {
		var end func(error)
		ctx, end = observer.StartAttempt(ctx, job)
		defer func() { end(err) }()
	}
	analyzeEnd := func(error) {}
	if observer != nil {
		_, analyzeEnd = observer.StartOperation(ctx, "analyze documents")
	}
	report, err := analyze(job.Fixture)
	analyzeEnd(err)
	if err != nil {
		return nil, err
	}
	completeCtx := ctx
	completeEnd := func(error) {}
	if observer != nil {
		completeCtx, completeEnd = observer.StartOperation(ctx, "commit report")
	}
	err = queue.Complete(completeCtx, job, report)
	completeEnd(err)
	if err != nil {
		return nil, err
	}
	return &job, nil
}
