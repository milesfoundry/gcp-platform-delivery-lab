package cloudaws

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

func TestAWSWireRequests(t *testing.T) {
	var mu sync.Mutex
	var stored []byte
	var sent, acked, retried bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if target := r.Header.Get("X-Amz-Target"); target != "" {
			var input map[string]any
			if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
				t.Error(err)
			}
			w.Header().Set("Content-Type", "application/x-amz-json-1.0")
			switch {
			case strings.HasSuffix(target, "SendMessage"):
				sent = true
				if _, err := application.DecodeEnvelope([]byte(input["MessageBody"].(string))); err != nil {
					t.Error(err)
				}
				io.WriteString(w, `{"MessageId":"synthetic"}`)
			case strings.HasSuffix(target, "ReceiveMessage"):
				if input["VisibilityTimeout"] != float64(60) {
					t.Error("missing visibility bound")
				}
				io.WriteString(w, `{"Messages":[{"Body":"{\"version\":1,\"job_id\":\"12345678901234567890123456789012\"}","ReceiptHandle":"synthetic-receipt"}]}`)
			case strings.HasSuffix(target, "DeleteMessage"):
				acked = true
				io.WriteString(w, `{}`)
			case strings.HasSuffix(target, "ChangeMessageVisibility"):
				retried = true
				io.WriteString(w, `{}`)
			default:
				t.Error("unexpected SQS operation")
				w.WriteHeader(400)
			}
			return
		}
		switch r.Method {
		case "PUT":
			if r.Header.Get("If-None-Match") != "*" {
				t.Error("missing immutable condition")
			}
			if stored != nil {
				w.Header().Set("Content-Type", "application/xml")
				w.WriteHeader(412)
				io.WriteString(w, `<Error><Code>PreconditionFailed</Code></Error>`)
				return
			}
			stored, _ = io.ReadAll(r.Body)
		case "GET":
			_, _ = w.Write(stored)
		default:
			t.Error("unexpected object operation")
			w.WriteHeader(400)
		}
	}))
	defer server.Close()
	cfg := aws.Config{Region: "eu-west-2", Credentials: aws.NewCredentialsCache(credentials.NewStaticCredentialsProvider("synthetic", "synthetic", "")), HTTPClient: server.Client()}
	a := &Adapter{queue: sqs.NewFromConfig(cfg, func(o *sqs.Options) { o.BaseEndpoint = aws.String(server.URL) }), objects: s3.NewFromConfig(cfg, func(o *s3.Options) { o.BaseEndpoint = aws.String(server.URL); o.UsePathStyle = true }), queueURL: server.URL + "/queue", bucket: "report-test"}
	ctx := context.Background()
	if err := a.Publish(ctx, application.Envelope{Version: 1, JobID: "12345678901234567890123456789012"}); err != nil {
		t.Fatal(err)
	}
	d, err := a.Receive(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = application.DecodeEnvelope(d.Body()); err != nil {
		t.Fatal(err)
	}
	if err = d.Retry(ctx, 30*time.Second); err != nil {
		t.Fatal(err)
	}
	if err = d.Ack(ctx); err != nil {
		t.Fatal(err)
	}
	j := domain.Job{ID: "12345678901234567890123456789012", Attempt: 1}
	report, _ := domain.Analyze("tiny-v1")
	first, err := a.Put(ctx, j, report)
	if err != nil {
		t.Fatal(err)
	}
	second, err := a.Put(ctx, j, report)
	if err != nil || first != second {
		t.Fatal("immutable retry failed", err)
	}
	mu.Lock()
	stored = []byte("corrupt")
	mu.Unlock()
	if _, err = a.Put(ctx, j, report); err == nil {
		t.Fatal("accepted corrupt object")
	}
	mu.Lock()
	defer mu.Unlock()
	if !sent || !acked || !retried {
		t.Fatal("missing delivery operations")
	}
}
func TestInvalidAWSConfig(t *testing.T) {
	if _, err := New(context.Background(), "eu-west-1", "https://foreign.example/queue", "report-test"); err == nil {
		t.Fatal("foreign region accepted")
	}
}
