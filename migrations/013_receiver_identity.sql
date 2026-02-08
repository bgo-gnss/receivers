-- Migration 013: Add receiver identity columns to stations table
-- Stores firmware version, detected model, and serial number reported by receivers
-- during health checks. Enables mismatch detection when receivers are replaced.

BEGIN;

ALTER TABLE stations ADD COLUMN IF NOT EXISTS firmware_version VARCHAR(30);
ALTER TABLE stations ADD COLUMN IF NOT EXISTS detected_model VARCHAR(60);
ALTER TABLE stations ADD COLUMN IF NOT EXISTS serial_number VARCHAR(30);
ALTER TABLE stations ADD COLUMN IF NOT EXISTS identity_last_checked TIMESTAMPTZ;

COMMENT ON COLUMN stations.firmware_version IS 'Firmware version reported by receiver';
COMMENT ON COLUMN stations.detected_model IS 'Receiver model detected from device response';
COMMENT ON COLUMN stations.serial_number IS 'Serial number reported by receiver';
COMMENT ON COLUMN stations.identity_last_checked IS 'Last time receiver identity was verified';

COMMIT;
