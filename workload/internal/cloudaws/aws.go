// Package cloudaws implements existing-resource SQS/S3 ports, never provisioning.
package cloudaws

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/smithy-go"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/application"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

type Adapter struct {
	queue            *sqs.Client
	objects          *s3.Client
	queueURL, bucket string
}

func New(ctx context.Context, region, queueURL, bucket string) (*Adapter, error) {
	if region != "eu-west-2" || !regexp.MustCompile(`^https://sqs\.eu-west-2\.amazonaws\.com/[0-9]{12}/report-[a-z0-9-]{1,60}$`).MatchString(queueURL) || !regexp.MustCompile(`^report-[a-z0-9-]{3,55}$`).MatchString(bucket) {
		return nil, errors.New("invalid London AWS resource configuration")
	}
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(region))
	if err != nil {
		return nil, err
	}
	return &Adapter{queue: sqs.NewFromConfig(cfg), objects: s3.NewFromConfig(cfg), queueURL: queueURL, bucket: bucket}, nil
}
func (a *Adapter) Publish(ctx context.Context, envelope application.Envelope) error {
	body, err := json.Marshal(envelope)
	if err != nil {
		return err
	}
	_, err = a.queue.SendMessage(ctx, &sqs.SendMessageInput{QueueUrl: aws.String(a.queueURL), MessageBody: aws.String(string(body))})
	return err
}

type delivery struct {
	adapter *Adapter
	body    []byte
	receipt string
}

func (d *delivery) Body() []byte { return d.body }
func (d *delivery) Ack(ctx context.Context) error {
	_, err := d.adapter.queue.DeleteMessage(ctx, &sqs.DeleteMessageInput{QueueUrl: aws.String(d.adapter.queueURL), ReceiptHandle: aws.String(d.receipt)})
	return err
}
func (d *delivery) Retry(ctx context.Context, delay time.Duration) error {
	_, err := d.adapter.queue.ChangeMessageVisibility(ctx, &sqs.ChangeMessageVisibilityInput{QueueUrl: aws.String(d.adapter.queueURL), ReceiptHandle: aws.String(d.receipt), VisibilityTimeout: int32(delay.Seconds())})
	return err
}
func (a *Adapter) Receive(ctx context.Context) (application.Delivery, error) {
	out, err := a.queue.ReceiveMessage(ctx, &sqs.ReceiveMessageInput{QueueUrl: aws.String(a.queueURL), MaxNumberOfMessages: 1, WaitTimeSeconds: 1, VisibilityTimeout: 60})
	if err != nil {
		return nil, err
	}
	if len(out.Messages) == 0 {
		return nil, domain.ErrNotFound
	}
	m := out.Messages[0]
	if m.Body == nil || m.ReceiptHandle == nil {
		return nil, errors.New("incomplete SQS delivery")
	}
	return &delivery{a, []byte(*m.Body), *m.ReceiptHandle}, nil
}
func (a *Adapter) Put(ctx context.Context, j domain.Job, report domain.Report) (domain.ReportObject, error) {
	key, body, err := application.ResultContent(j, report)
	if err != nil {
		return domain.ReportObject{}, err
	}
	_, err = a.objects.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(a.bucket), Key: aws.String(key), Body: bytes.NewReader(body), ContentType: aws.String("application/json"), IfNoneMatch: aws.String("*")})
	if err != nil {
		var api smithy.APIError
		if !errors.As(err, &api) || api.ErrorCode() != "PreconditionFailed" {
			return domain.ReportObject{}, err
		}
		out, readErr := a.objects.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(a.bucket), Key: aws.String(key)})
		if readErr != nil {
			return domain.ReportObject{}, readErr
		}
		existing, readErr := io.ReadAll(io.LimitReader(out.Body, 4097))
		_ = out.Body.Close()
		if readErr != nil {
			return domain.ReportObject{}, readErr
		}
		if !bytes.Equal(existing, body) {
			return domain.ReportObject{}, errors.New("immutable object mismatch")
		}
	}
	return domain.ReportObject{Provider: "s3", Bucket: a.bucket, Key: key, SHA256: domain.Hash(body)}, nil
}
