// Package cloudgcp implements existing-resource regional Pub/Sub and GCS ports.
package cloudgcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/storage"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
	"google.golang.org/api/googleapi"
	"google.golang.org/api/option"
)

type Adapter struct {
	publisher                   *pubsub.TopicAdminClient
	subscriber                  *pubsub.SubscriptionAdminClient
	objects                     *storage.Client
	topic, subscription, bucket string
}

func New(ctx context.Context, project, topic, subscription, bucket string) (*Adapter, error) {
	if !regexp.MustCompile(`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`).MatchString(project) || !regexp.MustCompile(`^report-[a-z0-9-]{3,55}$`).MatchString(bucket) || !regexp.MustCompile(`^report-[a-z0-9-]{1,60}$`).MatchString(topic) || !regexp.MustCompile(`^report-[a-z0-9-]{1,60}$`).MatchString(subscription) {
		return nil, errors.New("invalid GCP resource configuration")
	}
	pub, err := pubsub.NewTopicAdminClient(ctx, option.WithEndpoint("europe-west2-pubsub.googleapis.com:443"))
	if err != nil {
		return nil, err
	}
	sub, err := pubsub.NewSubscriptionAdminClient(ctx, option.WithEndpoint("europe-west2-pubsub.googleapis.com:443"))
	if err != nil {
		_ = pub.Close()
		return nil, err
	}
	objects, err := storage.NewClient(ctx, storage.WithDisabledClientMetrics())
	if err != nil {
		_ = pub.Close()
		_ = sub.Close()
		return nil, err
	}
	return &Adapter{pub, sub, objects, "projects/" + project + "/topics/" + topic, "projects/" + project + "/subscriptions/" + subscription, bucket}, nil
}
func (a *Adapter) Close() { _ = a.publisher.Close(); _ = a.subscriber.Close(); _ = a.objects.Close() }
func (a *Adapter) Publish(ctx context.Context, envelope application.Envelope) error {
	body, err := json.Marshal(envelope)
	if err != nil {
		return err
	}
	_, err = a.publisher.Publish(ctx, &pubsubpb.PublishRequest{Topic: a.topic, Messages: []*pubsubpb.PubsubMessage{{Data: body}}})
	return err
}

type delivery struct {
	adapter *Adapter
	body    []byte
	receipt string
}

func (d *delivery) Body() []byte { return d.body }
func (d *delivery) Ack(ctx context.Context) error {
	return d.adapter.subscriber.Acknowledge(ctx, &pubsubpb.AcknowledgeRequest{Subscription: d.adapter.subscription, AckIds: []string{d.receipt}})
}
func (d *delivery) Retry(ctx context.Context, delay time.Duration) error {
	return d.adapter.subscriber.ModifyAckDeadline(ctx, &pubsubpb.ModifyAckDeadlineRequest{Subscription: d.adapter.subscription, AckIds: []string{d.receipt}, AckDeadlineSeconds: int32(delay.Seconds())})
}
func (a *Adapter) Receive(ctx context.Context) (application.Delivery, error) {
	pull, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	out, err := a.subscriber.Pull(pull, &pubsubpb.PullRequest{Subscription: a.subscription, MaxMessages: 1})
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || pull.Err() == context.DeadlineExceeded {
			return nil, domain.ErrNotFound
		}
		return nil, err
	}
	if len(out.ReceivedMessages) == 0 {
		return nil, domain.ErrNotFound
	}
	m := out.ReceivedMessages[0]
	if m.Message == nil || m.AckId == "" {
		return nil, errors.New("incomplete Pub/Sub delivery")
	}
	d := &delivery{a, m.Message.Data, m.AckId}
	if err = d.Retry(ctx, 60*time.Second); err != nil {
		return nil, err
	}
	return d, nil
}
func (a *Adapter) Put(ctx context.Context, j domain.Job, report domain.Report) (domain.ReportObject, error) {
	key, body, err := application.ResultContent(j, report)
	if err != nil {
		return domain.ReportObject{}, err
	}
	obj := a.objects.Bucket(a.bucket).Object(key)
	writer := obj.If(storage.Conditions{DoesNotExist: true}).NewWriter(ctx)
	writer.ContentType = "application/json"
	writer.ChunkSize = 0
	_, writeErr := writer.Write(body)
	err = writer.Close()
	if writeErr != nil {
		err = writeErr
	}
	if err != nil {
		var api *googleapi.Error
		if !errors.As(err, &api) || api.Code != 412 {
			return domain.ReportObject{}, err
		}
		reader, readErr := obj.NewReader(ctx)
		if readErr != nil {
			return domain.ReportObject{}, readErr
		}
		existing, readErr := io.ReadAll(io.LimitReader(reader, 4097))
		_ = reader.Close()
		if readErr != nil {
			return domain.ReportObject{}, readErr
		}
		if !bytes.Equal(existing, body) {
			return domain.ReportObject{}, errors.New("immutable object mismatch")
		}
	}
	return domain.ReportObject{Provider: "gcs", Bucket: a.bucket, Key: key, SHA256: domain.Hash(body)}, nil
}
