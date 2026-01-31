-- Rollback migration 005: Restore fix_type columns to VARCHAR(15)

ALTER TABLE block_pvt_geodetic
    ALTER COLUMN fix_type TYPE VARCHAR(15);

ALTER TABLE block_pos_covariance
    ALTER COLUMN fix_type TYPE VARCHAR(15);
