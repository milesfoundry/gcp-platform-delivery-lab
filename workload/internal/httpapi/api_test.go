package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

type fakeStore struct{}

func (fakeStore) Submit(context.Context, string, domain.Request) (domain.Job, error) {
	return domain.Job{ID: "example", State: "pending"}, nil
}
func (fakeStore) Get(context.Context, string) (domain.Job, error) {
	return domain.Job{}, domain.ErrNotFound
}

func TestContracts(t *testing.T) {
	h := Handler(fakeStore{}, func(context.Context) error { return nil })
	for _, tc := range []struct {
		name, method, path, key, body string
		want                          int
	}{
		{"health", "GET", "/healthz", "", "", 200},
		{"ready", "GET", "/readyz", "", "", 200},
		{"missing", "GET", "/v1/jobs/missing", "", "", 404},
		{"missing key", "POST", "/v1/jobs", "", `{"fixture":"tiny-v1","algorithm":"tokens-v1"}`, 400},
		{"valid", "POST", "/v1/jobs", "test", `{"fixture":"tiny-v1","algorithm":"tokens-v1"}`, 202},
		{"unknown field", "POST", "/v1/jobs", "test", `{"fixture":"tiny-v1","algorithm":"tokens-v1","url":"https://example.com"}`, 400},
		{"trailing JSON", "POST", "/v1/jobs", "test", `{"fixture":"tiny-v1","algorithm":"tokens-v1"}{}`, 400},
		{"unknown fixture", "POST", "/v1/jobs", "test", `{"fixture":"../../etc/passwd","algorithm":"tokens-v1"}`, 400},
		{"oversized", "POST", "/v1/jobs", "test", strings.Repeat("x", 4097), 400},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			r.Header.Set("Idempotency-Key", tc.key)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != tc.want {
				t.Fatalf("got %d want %d: %s", w.Code, tc.want, w.Body.String())
			}
		})
	}
	h = Handler(fakeStore{}, func(context.Context) error { return errors.New("private connection detail") })
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if w.Code != 503 || strings.Contains(w.Body.String(), "private") {
		t.Fatal(w.Body.String())
	}
}
