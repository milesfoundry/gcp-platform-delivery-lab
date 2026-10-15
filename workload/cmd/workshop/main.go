package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/cloudaws"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/cloudgcp"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/httpapi"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/postgres"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/telemetry"
)

var revision = "development"
var analysisVariant = "baseline"

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx); err != nil {
		slog.Error("process failed", "category", "runtime")
		os.Exit(1)
	}
}

func run(ctx context.Context) error {
	if len(os.Args) != 2 {
		return errors.New("expected migrate, schema-check, api or worker")
	}
	mode := os.Args[1]
	if analysisVariant != "baseline" && analysisVariant != "regressed" {
		return errors.New("invalid compiled analysis variant")
	}
	if mode != "migrate" && mode != "schema-check" && mode != "api" && mode != "worker" {
		return errors.New("invalid mode")
	}
	url := os.Getenv("DATABASE_URL")
	if url == "" {
		return errors.New("DATABASE_URL is required")
	}
	connect, cancel := context.WithTimeout(ctx, 10*time.Second)
	s, err := postgres.Open(connect, url)
	cancel()
	if err != nil {
		return err
	}
	defer s.Pool.Close()
	if mode == "migrate" {
		return s.Migrate(ctx)
	}
	if mode == "schema-check" {
		return s.Ready(ctx)
	}
	metrics := telemetry.New(mode, revision)
	backend := os.Getenv("WORK_BACKEND")
	metrics.RequireDispatchHealth(mode == "worker" && backend != "" && backend != "local")
	metrics.SetVariant(analysisVariant)
	traces, err := telemetry.NewTraces(
		ctx,
		mode,
		revision,
		os.Getenv("DEPLOYMENT_ENVIRONMENT_NAME"),
		os.Getenv("WORKSHOP_RUN_ID"),
		os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
	)
	if err != nil {
		return err
	}
	defer traces.Shutdown()
	stopMetrics, err := metrics.Start(ctx, os.Getenv("METRICS_ADDR"), os.Getenv("PROFILE_ENABLED") == "true")
	if err != nil {
		return err
	}
	defer stopMetrics()
	if mode == "worker" {
		var queue application.WorkQueue = s
		switch os.Getenv("WORK_BACKEND") {
		case "", "local":
		case "gcp":
			adapter, err := cloudgcp.New(ctx, os.Getenv("GCP_PROJECT"), os.Getenv("PUBSUB_TOPIC"), os.Getenv("PUBSUB_SUBSCRIPTION"), os.Getenv("REPORT_BUCKET"))
			if err != nil {
				return err
			}
			defer adapter.Close()
			queue = &application.CloudQueue{State: s, Queue: adapter, Objects: adapter}
		case "aws":
			adapter, err := cloudaws.New(ctx, os.Getenv("AWS_REGION"), os.Getenv("SQS_QUEUE_URL"), os.Getenv("REPORT_BUCKET"))
			if err != nil {
				return err
			}
			queue = &application.CloudQueue{State: s, Queue: adapter, Objects: adapter}
		default:
			return errors.New("invalid WORK_BACKEND")
		}
		if cloud, ok := queue.(*application.CloudQueue); ok {
			cloud.BackgroundDispatch = true
			dispatchCtx, stopDispatch := context.WithCancel(ctx)
			finished := make(chan struct{})
			go func() {
				defer close(finished)
				ticker := time.NewTicker(200 * time.Millisecond)
				defer ticker.Stop()
				for {
					select {
					case <-dispatchCtx.Done():
						return
					case <-ticker.C:
						iteration, cancel := context.WithTimeout(dispatchCtx, 10*time.Second)
						err := cloud.PublishPending(iteration)
						cancel()
						metrics.ObserveDispatch(err)
						if err != nil {
							slog.Warn("dispatch iteration failed", "category", "dispatch")
						}
					}
				}
			}()
			defer func() { stopDispatch(); <-finished }()
		}
		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return nil
			case <-ticker.C:
				iteration, cancel := context.WithTimeout(ctx, 10*time.Second)
				err := process(iteration, queue, metrics, traces)
				cancel()
				if err != nil {
					slog.Warn("processing iteration failed", "category", "processing")
				}
			}
		}
	}
	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8080"
	}
	srv := &http.Server{Addr: addr, Handler: httpapi.Handler(traces.Repository(s), s.Ready, metrics.InstrumentHTTP, traces.InstrumentHTTP), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second}
	finished := make(chan struct{})
	defer close(finished)
	go func() {
		select {
		case <-ctx.Done():
			shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = srv.Shutdown(shutdown)
		case <-finished:
		}
	}()
	slog.Info("API listening", "address", addr, "revision", revision)
	err = srv.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func process(ctx context.Context, s application.WorkQueue, metrics *telemetry.Metrics, traces *telemetry.Traces) error {
	started := time.Now()
	passes := 1
	if analysisVariant == "regressed" {
		passes = 64
	}
	analyzer := func(fixture string) (domain.Report, error) { return domain.AnalyzeRepeated(fixture, passes) }
	j, err := application.ProcessOneWithAnalyzerAndPoll(ctx, s, revision, analyzer, metrics.ObservePoll, traces)
	attempt := 0
	if j != nil {
		attempt = j.Attempt
	}
	metrics.ObserveIteration(j != nil, attempt, err, time.Since(started))
	if err != nil {
		return err
	}
	if j != nil {
		slog.Info("job completed", "service", "worker", "job_id", j.ID, "attempt", j.Attempt, "revision", revision, "trace_id", telemetry.JobTraceID(j.TraceParent), "processing_ms", time.Since(started).Milliseconds())
	}
	return nil
}
