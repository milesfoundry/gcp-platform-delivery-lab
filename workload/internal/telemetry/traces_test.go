package telemetry

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/httpapi"
)

func TestTraceResourceHasExplicitEnvironmentAndRunIdentity(t *testing.T) {
	got, err := newTraceResource("api", "revision-123", "staging", "run-456")
	if err != nil {
		t.Fatal(err)
	}
	attrs := map[string]string{}
	for _, attr := range got.Attributes() {
		attrs[string(attr.Key)] = attr.Value.AsString()
	}
	want := map[string]string{
		"service.name":                "report-workshop-api",
		"service.version":             "revision-123",
		"deployment.environment.name": "staging",
		"workshop.run_id":             "run-456",
	}
	for key, value := range want {
		if attrs[key] != value {
			t.Errorf("resource %s = %q, want %q", key, attrs[key], value)
		}
	}
}

func TestTraceResourceRejectsMissingOrUnsafeIdentity(t *testing.T) {
	const sensitive = "private@example.invalid"
	tests := []struct {
		name, environment, runID string
	}{
		{name: "missing environment", environment: "", runID: "run-1"},
		{name: "missing run ID", environment: "staging", runID: ""},
		{name: "unsafe environment", environment: sensitive, runID: "run-1"},
		{name: "unsafe run ID", environment: "staging", runID: "run/1"},
		{name: "oversized value", environment: "staging", runID: strings.Repeat("a", 65)},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := newTraceResource("api", "revision", tt.environment, tt.runID)
			if err == nil {
				t.Fatal("expected invalid trace identity to fail")
			}
			if strings.Contains(err.Error(), sensitive) || tt.runID != "" && strings.Contains(err.Error(), tt.runID) {
				t.Fatal("validation error exposed an identity value")
			}
		})
	}
}

func TestTraceNoOpDoesNotRequireIdentity(t *testing.T) {
	traces, err := NewTraces(context.Background(), "api", "revision", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if traces.provider != nil {
		t.Fatal("no-op traces unexpectedly created an exporter")
	}
}

func TestTraceExporterRequiresExplicitIdentity(t *testing.T) {
	_, err := NewTraces(context.Background(), "api", "revision", "", "run-1", "http://127.0.0.1:4318/v1/traces")
	if err == nil || !strings.Contains(err.Error(), "environment") {
		t.Fatalf("got error %v, want missing environment rejection", err)
	}
}

type traceStore struct{ job domain.Job }

func (s *traceStore) Submit(_ context.Context, _ string, r domain.Request) (domain.Job, error) {
	s.job = domain.Job{ID: "synthetic", Fixture: r.Fixture, TraceParent: r.TraceParent}
	return s.job, nil
}
func (s *traceStore) Get(context.Context, string) (domain.Job, error) { return s.job, nil }

func TestDurableParentAndRetryCorrelation(t *testing.T) {
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	defer provider.Shutdown(context.Background())
	traces := &Traces{tracer: provider.Tracer("test")}
	store := &traceStore{}
	handler := httpapi.Handler(traces.Repository(store), func(context.Context) error { return nil }, traces.InstrumentHTTP)
	request := httptest.NewRequest("POST", "/v1/jobs", strings.NewReader(`{"fixture":"tiny-v1","algorithm":"tokens-v1"}`))
	request.Header.Set("Idempotency-Key", "trace-test")
	request.Header.Set("Authorization", "SECRET-not-exported")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != 202 {
		t.Fatal(response.Body.String())
	}
	expectedTraceID := response.Header().Get("Trace-Id")
	if expectedTraceID == "" || JobTraceID(store.job.TraceParent) != expectedTraceID {
		t.Fatal("submission parent missing")
	}
	for attempt := 1; attempt <= 2; attempt++ {
		store.job.Attempt = attempt
		ctx, end := traces.StartAttempt(context.Background(), store.job)
		_, finish := traces.StartOperation(ctx, "analyze documents")
		finish(nil)
		end(nil)
	}
	spans := exporter.GetSpans()
	var persistedID string
	for _, span := range spans {
		if span.Name == "persist job and outbox" {
			persistedID = span.SpanContext.SpanID().String()
		}
		for _, attr := range span.Attributes {
			if strings.Contains(attr.Value.AsString(), "SECRET") {
				t.Fatal("private header exported")
			}
		}
	}
	attempts := 0
	for _, span := range spans {
		if span.Name == "process job" {
			attempts++
			if span.SpanContext.TraceID().String() != expectedTraceID || span.Parent.SpanID().String() != persistedID {
				t.Fatal("retry did not continue the original durable parent")
			}
		}
	}
	if attempts != 2 {
		t.Fatalf("got %d attempts", attempts)
	}
}
