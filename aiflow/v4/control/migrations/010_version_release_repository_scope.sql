ALTER TABLE version_releases DROP CONSTRAINT IF EXISTS version_releases_pkey;
ALTER TABLE version_releases ADD PRIMARY KEY (project, version_id, repository);
