// Package postgres provides durable job state and the local-only queue adapter.
package postgres

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/milesfoundry/gcp-platform-delivery-lab/workload/internal/domain"
)

//go:embed schema.sql
var schema string

var ErrConflict = domain.ErrConflict
var ErrStale = domain.ErrStale

type Job = domain.Job

type Store struct{ Pool *pgxpool.Pool }

func Open(ctx context.Context, url string) (*Store, error) {
	p, err := pgxpool.New(ctx, url)
	if err != nil {
		return nil, err
	}
	if err = p.Ping(ctx); err != nil {
		p.Close()
		return nil, err
	}
	return &Store{Pool: p}, nil
}

func (s *Store) Migrate(ctx context.Context) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(194001)"); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, schema); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Ready checks the additive schema without performing DDL from an app pod.
func (s *Store) Ready(ctx context.Context) error {
	var ready bool
	err := s.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='jobs' AND column_name='report_object') AND EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='outbox' AND column_name='token')`).Scan(&ready)
	if err != nil {
		return err
	}
	if !ready {
		return errors.New("migration not ready")
	}
	return nil
}

const fields = "id,fixture,algorithm,state,attempt,created_at,completed_at,report,trace_parent,report_object"

func scan(row pgx.Row) (Job, error) {
	var j Job
	err := row.Scan(&j.ID, &j.Fixture, &j.Algorithm, &j.State, &j.Attempt, &j.CreatedAt, &j.CompletedAt, &j.Report, &j.TraceParent, &j.Object)
	return j, err
}

func (s *Store) Get(ctx context.Context, id string) (Job, error) {
	j, err := scan(s.Pool.QueryRow(ctx, "SELECT "+fields+" FROM jobs WHERE id=$1", id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, domain.ErrNotFound
	}
	return j, err
}

func (s *Store) Submit(ctx context.Context, key string, req domain.Request) (Job, error) {
	if err := domain.Validate(req); err != nil {
		return Job{}, err
	}
	data, err := json.Marshal(req)
	if err != nil {
		return Job{}, err
	}
	hash := domain.Hash(data)
	var nonce [16]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		return Job{}, err
	}
	id := hex.EncodeToString(nonce[:])
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return Job{}, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, `INSERT INTO jobs(id,idempotency_key,request_hash,fixture,algorithm,trace_parent) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(idempotency_key) DO NOTHING`, id, key, hash, req.Fixture, req.Algorithm, req.TraceParent)
	if err != nil {
		return Job{}, err
	}
	var actual string
	if err = tx.QueryRow(ctx, "SELECT id,request_hash FROM jobs WHERE idempotency_key=$1", key).Scan(&id, &actual); err != nil {
		return Job{}, err
	}
	if actual != hash {
		return Job{}, ErrConflict
	}
	if _, err = tx.Exec(ctx, "INSERT INTO outbox(job_id) VALUES($1) ON CONFLICT DO NOTHING", id); err != nil {
		return Job{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Job{}, err
	}
	return s.Get(ctx, id)
}

// Dispatch uses a database-local atomic handoff. Cloud queues need their own
// publish/mark protocol and duplicate-delivery tests; this is not a cloud adapter.
func (s *Store) Dispatch(ctx context.Context) error {
	_, err := s.Pool.Exec(ctx, `WITH pending AS (
 SELECT job_id FROM outbox WHERE published_at IS NULL ORDER BY job_id LIMIT 100 FOR UPDATE SKIP LOCKED
), queued AS (
 INSERT INTO local_queue(job_id) SELECT job_id FROM pending ON CONFLICT DO NOTHING
) UPDATE outbox SET published_at=now() WHERE job_id IN (SELECT job_id FROM pending)`)
	return err
}

func (s *Store) Claim(ctx context.Context, revision string, lease time.Duration) (Job, error) {
	return s.claim(ctx, revision, lease, "")
}

// ClaimJob uses the same DB fencing as local delivery; queue receipts grant no ownership.
func (s *Store) ClaimJob(ctx context.Context, id, revision string, lease time.Duration) (Job, error) {
	return s.claim(ctx, revision, lease, id)
}

func (s *Store) claim(ctx context.Context, revision string, lease time.Duration, target string) (Job, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return Job{}, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, `UPDATE jobs SET state='failed',completed_at=now() WHERE state='running' AND lease_until<=now() AND attempt>=5`)
	if err != nil {
		return Job{}, err
	}
	var id string
	query := `SELECT j.id FROM jobs j JOIN local_queue q ON q.job_id=j.id
 WHERE q.available_at<=now() AND j.attempt<5 AND (j.state='pending' OR (j.state='running' AND j.lease_until<=now()))
 ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1`
	var arguments []any
	if target != "" {
		query = `SELECT id FROM jobs WHERE id=$1 AND attempt<5 AND (state='pending' OR (state='running' AND lease_until<=now())) FOR UPDATE SKIP LOCKED`
		arguments = []any{target}
	}
	err = tx.QueryRow(ctx, query, arguments...).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		if commitErr := tx.Commit(ctx); commitErr != nil {
			return Job{}, commitErr
		}
		return Job{}, domain.ErrNotFound
	}
	if err != nil {
		return Job{}, err
	}
	j, err := scan(tx.QueryRow(ctx, `UPDATE jobs SET state='running',attempt=attempt+1,lease_until=now()+$2::interval,revision=$3 WHERE id=$1 RETURNING `+fields, id, fmt.Sprintf("%f seconds", lease.Seconds()), revision))
	if err != nil {
		return Job{}, err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO attempts(job_id,token,revision) VALUES($1,$2,$3)", j.ID, j.Attempt, revision); err != nil {
		return Job{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Job{}, err
	}
	return j, nil
}

func (s *Store) Complete(ctx context.Context, j Job, report domain.Report) error {
	return s.complete(ctx, j, report, nil)
}

func (s *Store) CompleteObject(ctx context.Context, j Job, report domain.Report, object domain.ReportObject) error {
	data, err := json.Marshal(report)
	if err != nil {
		return err
	}
	hash := domain.Hash(data)
	if (object.Provider != "gcs" && object.Provider != "s3") || object.Bucket == "" || object.SHA256 != hash || object.Key != fmt.Sprintf("reports/%s/attempt-%d/%s.json", j.ID, j.Attempt, hash) {
		return errors.New("invalid immutable object reference")
	}
	return s.complete(ctx, j, report, &object)
}

func (s *Store) complete(ctx context.Context, j Job, report domain.Report, object *domain.ReportObject) error {
	data, err := json.Marshal(report)
	if err != nil {
		return err
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `UPDATE jobs SET state='succeeded',report=$3,report_object=$4,completed_at=now(),lease_until=NULL WHERE id=$1 AND attempt=$2 AND state='running' AND lease_until>now()`, j.ID, j.Attempt, data, object)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return ErrStale
	}
	if _, err = tx.Exec(ctx, "UPDATE attempts SET completed_at=now() WHERE job_id=$1 AND token=$2", j.ID, j.Attempt); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "DELETE FROM local_queue WHERE job_id=$1", j.ID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ClaimPublication commits a short lease before any cloud network operation.
func (s *Store) ClaimPublication(ctx context.Context) (string, int, error) {
	var id string
	var token int
	err := s.Pool.QueryRow(ctx, `UPDATE outbox SET lease_until=now()+interval '30 seconds',token=token+1
 WHERE job_id=(SELECT job_id FROM outbox WHERE published_at IS NULL AND (lease_until IS NULL OR lease_until<=now())
 ORDER BY job_id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING job_id,token`).Scan(&id, &token)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", 0, domain.ErrNotFound
	}
	return id, token, err
}

func (s *Store) MarkPublished(ctx context.Context, id string, token int) error {
	tag, err := s.Pool.Exec(ctx, `UPDATE outbox SET published_at=now(),lease_until=NULL WHERE job_id=$1 AND token=$2 AND published_at IS NULL AND lease_until>now()`, id, token)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return domain.ErrStale
	}
	return nil
}
