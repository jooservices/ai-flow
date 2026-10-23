CREATE TABLE IF NOT EXISTS control_backup_config (
    id                    boolean PRIMARY KEY DEFAULT true CHECK (id),
    schedule              text NOT NULL DEFAULT 'daily' CHECK (schedule IN ('daily','weekly')),
    destination_kind      text NOT NULL DEFAULT 'local' CHECK (destination_kind IN ('local','mounted','s3')),
    destination_path      text NOT NULL DEFAULT '/srv/aiflow/backups',
    s3_endpoint           text NOT NULL DEFAULT '',
    s3_bucket             text NOT NULL DEFAULT '',
    s3_prefix             text NOT NULL DEFAULT '',
    s3_region              text NOT NULL DEFAULT 'us-east-1',
    retention_days        integer NOT NULL DEFAULT 30 CHECK (retention_days BETWEEN 1 AND 3650),
    key_downloaded_at     timestamptz,
    backup_requested_at   timestamptz,
    last_backup_at        timestamptz,
    last_backup_name      text NOT NULL DEFAULT '',
    last_backup_status    text NOT NULL DEFAULT 'never' CHECK (last_backup_status IN ('never','running','success','failed')),
    last_backup_detail    text NOT NULL DEFAULT '',
    last_test_at          timestamptz,
    last_test_status      text NOT NULL DEFAULT 'never' CHECK (last_test_status IN ('never','success','failed')),
    last_test_detail      text NOT NULL DEFAULT '',
    last_test_components  jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at            timestamptz NOT NULL DEFAULT now()
);
INSERT INTO control_backup_config (id, destination_path) VALUES (true, '/srv/aiflow/backups')
ON CONFLICT (id) DO NOTHING;
