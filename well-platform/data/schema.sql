-- Generated from data/orm.py by scripts/generate_schema_sql.py — do not hand-edit.
-- Run: python3 scripts/generate_schema_sql.py > data/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;


CREATE TABLE ingestion_runs (
	id UUID NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	scope_description VARCHAR(256) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	records_fetched INTEGER NOT NULL, 
	records_upserted INTEGER NOT NULL, 
	error_message TEXT, 
	PRIMARY KEY (id)
);

CREATE TABLE metric_thresholds (
	metric_code VARCHAR(32) NOT NULL, 
	unit VARCHAR(16) NOT NULL, 
	warning_value FLOAT, 
	critical_value FLOAT, 
	PRIMARY KEY (metric_code)
);

CREATE TABLE wells (
	id UUID NOT NULL, 
	api_number VARCHAR(24) NOT NULL, 
	api_number_is_synthetic BOOLEAN NOT NULL, 
	rrc_object_id INTEGER, 
	identity_method VARCHAR(24) NOT NULL, 
	source_feature_count INTEGER NOT NULL, 
	well_number VARCHAR(16), 
	lease_name VARCHAR(128), 
	latitude FLOAT NOT NULL, 
	longitude FLOAT NOT NULL, 
	elevation_ft FLOAT, 
	geom geometry(POINT,4326) NOT NULL, 
	county_fips VARCHAR(3), 
	county_name VARCHAR(64), 
	operator_name VARCHAR(128), 
	rrc_operator_number VARCHAR(16), 
	regulatory_status VARCHAR(40) NOT NULL, 
	regulatory_status_raw VARCHAR(64), 
	is_orphaned BOOLEAN NOT NULL, 
	operational_status VARCHAR(20), 
	location_reliability_code VARCHAR(4), 
	location_source VARCHAR(128), 
	data_source VARCHAR(32) NOT NULL, 
	source_synced_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_wells_geom ON wells USING gist (geom);

CREATE INDEX ix_wells_operational_status_not_null ON wells (operational_status) WHERE operational_status IS NOT NULL;

CREATE UNIQUE INDEX ix_wells_api_number ON wells (api_number);

CREATE INDEX ix_wells_regulatory_status ON wells (regulatory_status);

CREATE INDEX ix_wells_county_fips ON wells (county_fips);

CREATE INDEX ix_wells_is_orphaned ON wells (is_orphaned);

CREATE INDEX ix_wells_operator_name ON wells (operator_name);

CREATE TABLE raw_well_features (
	id UUID NOT NULL, 
	source_layer VARCHAR(20) NOT NULL, 
	rrc_object_id INTEGER NOT NULL, 
	ingestion_run_id UUID, 
	fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	raw_api VARCHAR(20) NOT NULL, 
	gis_api5 VARCHAR(8), 
	well_number VARCHAR(16), 
	symnum INTEGER, 
	symbol_description VARCHAR(64), 
	reliab VARCHAR(4), 
	location_source VARCHAR(128), 
	latitude FLOAT, 
	longitude FLOAT, 
	latitude_nad27 FLOAT, 
	longitude_nad27 FLOAT, 
	county_name_hint VARCHAR(64), 
	county_fips_hint VARCHAR(3), 
	is_orphaned_hint BOOLEAN NOT NULL, 
	well_id UUID, 
	promoted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_raw_well_features_source_objectid UNIQUE (source_layer, rrc_object_id), 
	FOREIGN KEY(ingestion_run_id) REFERENCES ingestion_runs (id) ON DELETE SET NULL, 
	FOREIGN KEY(well_id) REFERENCES wells (id) ON DELETE SET NULL
);

CREATE INDEX ix_raw_well_features_county_name_hint ON raw_well_features (county_name_hint);

CREATE INDEX ix_raw_well_features_well_id ON raw_well_features (well_id);

CREATE INDEX ix_raw_well_features_unpromoted ON raw_well_features (county_name_hint) WHERE well_id IS NULL;

CREATE TABLE sensor_devices (
	id UUID NOT NULL, 
	well_id UUID NOT NULL, 
	external_id VARCHAR(64) NOT NULL, 
	device_type VARCHAR(32) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	installed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_seen_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(well_id) REFERENCES wells (id) ON DELETE CASCADE, 
	UNIQUE (external_id)
);

CREATE INDEX ix_sensor_devices_well_id ON sensor_devices (well_id);

CREATE TABLE metric_readings (
	id UUID NOT NULL, 
	well_id UUID NOT NULL, 
	device_id UUID, 
	metric_code VARCHAR(32) NOT NULL, 
	value FLOAT NOT NULL, 
	unit VARCHAR(16) NOT NULL, 
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	ingested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(well_id) REFERENCES wells (id) ON DELETE CASCADE, 
	FOREIGN KEY(device_id) REFERENCES sensor_devices (id) ON DELETE SET NULL
);

CREATE INDEX ix_metric_readings_well_metric_time ON metric_readings (well_id, metric_code, recorded_at);
