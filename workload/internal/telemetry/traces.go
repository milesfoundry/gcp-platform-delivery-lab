package telemetry

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"time"
	"unicode/utf8"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

type Traces struct {
	tracer   trace.Tracer
	provider *sdktrace.TracerProvider
}

func NewTraces(ctx context.Context, role, revision, environment, runID, endpoint string) (*Traces, error) {
	if endpoint == "" {
		return &Traces{tracer: noop.NewTracerProvider().Tracer("report-workshop")}, nil
	}
	traceResource, err := newTraceResource(role, revision, environment, runID)
	if err != nil {
		return nil, err
	}
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Host == "" || parsed.User != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, errors.New("invalid trace exporter endpoint")
	}
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(endpoint), otlptracehttp.WithTimeout(2*time.Second))
	if err != nil {
		return nil, err
	}
	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter, sdktrace.WithBatchTimeout(200*time.Millisecond), sdktrace.WithMaxQueueSize(1024), sdktrace.WithMaxExportBatchSize(128)),
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())),
		sdktrace.WithResource(traceResource),
	)
	return &Traces{tracer: provider.Tracer("report-workshop"), provider: provider}, nil
}

func newTraceResource(role, revision, environment, runID string) (*resource.Resource, error) {
	if !validResourceIdentity(environment) {
		return nil, errors.New("trace deployment environment is missing or invalid")
	}
	if !validResourceIdentity(runID) {
		return nil, errors.New("trace run ID is missing or invalid")
	}
	return resource.NewSchemaless(
		attribute.String("service.name", "report-workshop-"+role),
		attribute.String("service.version", revision),
		attribute.String("deployment.environment.name", environment),
		attribute.String("workshop.run_id", runID),
	), nil
}

func validResourceIdentity(value string) bool {
	if value == "" || len(value) > 64 || !utf8.ValidString(value) {
		return false
	}
	for index, char := range value {
		isAlphaNumeric := char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' || char >= '0' && char <= '9'
		if index == 0 && !isAlphaNumeric {
			return false
		}
		if !isAlphaNumeric && char != '.' && char != '_' && char != '-' {
			return false
		}
	}
	return true
}

func (t *Traces) Shutdown() {
	if t.provider == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := t.provider.Shutdown(ctx); err != nil {
		slog.Warn("trace shutdown failed", "category", "telemetry")
	}
}

func finish(span trace.Span, err error) {
	if err != nil {
		span.SetStatus(codes.Error, "operation failed")
	} else {
		span.SetStatus(codes.Ok, "")
	}
	// Raw database/export error messages can contain private connection details.
	span.End()
}

func (t *Traces) StartOperation(ctx context.Context, name string) (context.Context, func(error)) {
	ctx, span := t.tracer.Start(ctx, name)
	return ctx, func(err error) { finish(span, err) }
}

func (t *Traces) StartAttempt(ctx context.Context, j domain.Job) (context.Context, func(error)) {
	ctx = propagation.TraceContext{}.Extract(ctx, propagation.MapCarrier{"traceparent": j.TraceParent})
	ctx, span := t.tracer.Start(ctx, "process job", trace.WithSpanKind(trace.SpanKindConsumer),
		trace.WithAttributes(attribute.String("workshop.job_id", j.ID), attribute.Int("workshop.attempt", j.Attempt), attribute.String("workshop.fixture", j.Fixture)))
	return ctx, func(err error) { finish(span, err) }
}

func JobTraceID(parent string) string {
	ctx := propagation.TraceContext{}.Extract(context.Background(), propagation.MapCarrier{"traceparent": parent})
	span := trace.SpanContextFromContext(ctx)
	if !span.IsValid() {
		return ""
	}
	return span.TraceID().String()
}

func (t *Traces) InstrumentHTTP(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx := propagation.TraceContext{}.Extract(r.Context(), propagation.HeaderCarrier(r.Header))
		ctx, span := t.tracer.Start(ctx, "HTTP request", trace.WithSpanKind(trace.SpanKindServer))
		r = r.WithContext(ctx)
		if span.SpanContext().IsValid() {
			w.Header().Set("Trace-Id", span.SpanContext().TraceID().String())
		}
		wrapped := &response{ResponseWriter: w}
		next.ServeHTTP(wrapped, r)
		route := r.Pattern
		if route == "" {
			route = "unmatched"
		}
		span.SetName(route)
		status := wrapped.status
		if status == 0 {
			status = 200
		}
		span.SetAttributes(attribute.String("http.route", route), attribute.Int("http.response.status_code", status))
		if status >= 500 {
			span.SetStatus(codes.Error, "HTTP server error")
		}
		slog.Info("HTTP completed", "service", "api", "route", route, "status", status, "trace_id", span.SpanContext().TraceID().String())
		span.End()
	})
}

type tracedRepository struct {
	application.JobReaderWriter
	traces *Traces
}

func (t *Traces) Repository(s application.JobReaderWriter) application.JobReaderWriter {
	return tracedRepository{JobReaderWriter: s, traces: t}
}

func (s tracedRepository) Submit(ctx context.Context, key string, req domain.Request) (domain.Job, error) {
	ctx, end := s.traces.StartOperation(ctx, "persist job and outbox")
	carrier := propagation.MapCarrier{}
	propagation.TraceContext{}.Inject(ctx, carrier)
	req.TraceParent = carrier.Get("traceparent")
	job, err := s.JobReaderWriter.Submit(ctx, key, req)
	if err == nil {
		trace.SpanFromContext(ctx).SetAttributes(attribute.String("workshop.job_id", job.ID))
	}
	end(err)
	return job, err
}

func (s tracedRepository) Get(ctx context.Context, id string) (domain.Job, error) {
	ctx, end := s.traces.StartOperation(ctx, "read job")
	job, err := s.JobReaderWriter.Get(ctx, id)
	end(err)
	return job, err
}
