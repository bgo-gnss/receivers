-- Migration 007: Add power_type column to stations table
-- Allows filtering mains-powered stations from voltage graphs/alerts.
-- Values: 'battery' (default, solar/battery systems), 'mains' (AC/DC converter)
ALTER TABLE stations ADD COLUMN IF NOT EXISTS power_type VARCHAR(10) DEFAULT 'battery';
