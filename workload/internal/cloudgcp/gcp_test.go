package cloudgcp

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"hash/crc32"
	"io"
	"mime"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/storage"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/emptypb"
)

type fakePubSub struct {
	pubsubpb.UnimplementedPublisherServer
	pubsubpb.UnimplementedSubscriberServer
	mu       sync.Mutex
	body     []byte
	acked    bool
	deadline int32
}

func (f *fakePubSub) Publish(_ context.Context, r *pubsubpb.PublishRequest) (*pubsubpb.PublishResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.body = r.Messages[0].Data
	return &pubsubpb.PublishResponse{MessageIds: []string{"synthetic"}}, nil
}
func (f *fakePubSub) Pull(context.Context, *pubsubpb.PullRequest) (*pubsubpb.PullResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return &pubsubpb.PullResponse{ReceivedMessages: []*pubsubpb.ReceivedMessage{{AckId: "synthetic", Message: &pubsubpb.PubsubMessage{Data: f.body}}}}, nil
}
func (f *fakePubSub) Acknowledge(context.Context, *pubsubpb.AcknowledgeRequest) (*emptypb.Empty, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.acked = true
	return &emptypb.Empty{}, nil
}
func (f *fakePubSub) ModifyAckDeadline(_ context.Context, r *pubsubpb.ModifyAckDeadlineRequest) (*emptypb.Empty, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deadline = r.AckDeadlineSeconds
	return &emptypb.Empty{}, nil
}
func (f *fakePubSub) status() (int32, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.deadline, f.acked
}

func TestGCPWireRequests(t *testing.T) {
	ctx := context.Background()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fake := &fakePubSub{}
	pubsubpb.RegisterPublisherServer(server, fake)
	pubsubpb.RegisterSubscriberServer(server, fake)
	go server.Serve(listener)
	defer server.Stop()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	pub, err := pubsub.NewTopicAdminClient(ctx, option.WithGRPCConn(connection))
	if err != nil {
		t.Fatal(err)
	}
	sub, err := pubsub.NewSubscriptionAdminClient(ctx, option.WithGRPCConn(connection))
	if err != nil {
		t.Fatal(err)
	}
	var stored []byte
	var storedMu sync.Mutex
	httpServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		storedMu.Lock()
		defer storedMu.Unlock()
		switch r.Method {
		case "POST":
			if r.URL.Query().Get("ifGenerationMatch") != "0" {
				t.Error("missing create-only condition")
			}
			if stored != nil {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(412)
				io.WriteString(w, `{"error":{"code":412,"message":"exists"}}`)
				return
			}
			_, params, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
			parts := multipart.NewReader(r.Body, params["boundary"])
			metadata, err := parts.NextPart()
			if err != nil {
				t.Error(err)
				return
			}
			var object map[string]any
			_ = json.NewDecoder(metadata).Decode(&object)
			data, err := parts.NextPart()
			if err != nil {
				t.Error(err)
				return
			}
			stored, _ = io.ReadAll(data)
			var checksum [4]byte
			binary.BigEndian.PutUint32(checksum[:], crc32.Checksum(stored, crc32.MakeTable(crc32.Castagnoli)))
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{"bucket": "report-test", "name": object["name"], "generation": "1", "size": strconv.Itoa(len(stored)), "crc32c": base64.StdEncoding.EncodeToString(checksum[:])})
		case "GET":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(stored)
		default:
			t.Error("unexpected GCS operation")
			w.WriteHeader(400)
		}
	}))
	defer httpServer.Close()
	objects, err := storage.NewClient(ctx, option.WithEndpoint(httpServer.URL), option.WithoutAuthentication(), storage.WithJSONReads(), storage.WithDisabledClientMetrics())
	if err != nil {
		t.Fatal(err)
	}
	a := &Adapter{pub, sub, objects, "projects/test/topics/report-test", "projects/test/subscriptions/report-test", "report-test"}
	defer a.Close()
	if err = a.Publish(ctx, application.Envelope{Version: 1, JobID: "12345678901234567890123456789012"}); err != nil {
		t.Fatal(err)
	}
	d, err := a.Receive(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if deadline, _ := fake.status(); deadline != 60 {
		t.Fatal("missing ack lease")
	}
	if err = d.Retry(ctx, 30*time.Second); err != nil {
		t.Fatal(err)
	}
	if err = d.Ack(ctx); err != nil {
		t.Fatal("missing ack")
	}
	if _, acked := fake.status(); !acked {
		t.Fatal("missing ack")
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
	storedMu.Lock()
	stored = []byte("corrupt")
	storedMu.Unlock()
	if _, err = a.Put(ctx, j, report); err == nil {
		t.Fatal("accepted corrupt object")
	}
}
func TestInvalidGCPConfig(t *testing.T) {
	if _, err := New(context.Background(), "bad", "report-test", "report-test", "report-test"); err == nil {
		t.Fatal("invalid project accepted")
	}
}
