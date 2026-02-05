-- Migration 008: Station Areas
-- Creates tables for station area groupings (volcanic and regional monitoring)

-- Area definitions table
CREATE TABLE IF NOT EXISTS station_areas (
    area_id VARCHAR(30) PRIMARY KEY,
    area_name VARCHAR(100) NOT NULL,
    area_type VARCHAR(20) NOT NULL CHECK (area_type IN ('volcanic', 'regional')),
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Station-to-area mapping (many-to-many, stations can belong to multiple areas)
CREATE TABLE IF NOT EXISTS station_area_members (
    area_id VARCHAR(30) REFERENCES station_areas(area_id) ON DELETE CASCADE,
    sid VARCHAR(10) NOT NULL,
    PRIMARY KEY (area_id, sid)
);

-- Create index for efficient lookups
CREATE INDEX IF NOT EXISTS idx_station_area_members_sid ON station_area_members(sid);
CREATE INDEX IF NOT EXISTS idx_station_areas_type ON station_areas(area_type);

-- Insert volcanic monitoring areas
INSERT INTO station_areas (area_id, area_name, area_type, description) VALUES
    ('svartsengi', 'Svartsengi', 'volcanic', 'Svartsengi/Sundhnúkur volcanic system'),
    ('hengill', 'Hengill', 'volcanic', 'Hengill volcanic system'),
    ('hekla', 'Hekla', 'volcanic', 'Hekla volcanic system'),
    ('katla', 'Katla', 'volcanic', 'Katla volcanic system (Mýrdalsjökull)'),
    ('eyjafjallajokull', 'Eyjafjallajökull', 'volcanic', 'Eyjafjallajökull volcanic system'),
    ('torfajokull', 'Torfajökull', 'volcanic', 'Torfajökull volcanic system'),
    ('grimsvotn', 'Grímsvötn', 'volcanic', 'Grímsvötn volcanic system'),
    ('bardarbunga', 'Bárðarbunga', 'volcanic', 'Bárðarbunga volcanic system (Holuhraun)'),
    ('oraefajokull', 'Öræfajökull', 'volcanic', 'Öræfajökull volcanic system'),
    ('askja', 'Askja', 'volcanic', 'Askja volcanic system'),
    ('krafla', 'Krafla', 'volcanic', 'Krafla volcanic system')
ON CONFLICT (area_id) DO NOTHING;

-- Insert regional monitoring areas
INSERT INTO station_areas (area_id, area_name, area_type, description) VALUES
    ('reykjanes', 'Reykjanes', 'regional', 'Reykjanes Peninsula'),
    ('south', 'South Iceland', 'regional', 'South Iceland coastal region'),
    ('north', 'North Iceland', 'regional', 'North Iceland'),
    ('east', 'East Iceland', 'regional', 'East Iceland'),
    ('west_westfjords', 'West / Westfjords', 'regional', 'West Iceland and Westfjords'),
    ('central_highlands', 'Central Highlands', 'regional', 'Central highland interior'),
    ('vatnajokull', 'Vatnajökull', 'regional', 'Vatnajökull glacier region')
ON CONFLICT (area_id) DO NOTHING;

-- Insert station memberships - Volcanic areas
-- Svartsengi
INSERT INTO station_area_members (area_id, sid) VALUES
    ('svartsengi', 'GRIV'), ('svartsengi', 'GRVM'), ('svartsengi', 'GRVV'),
    ('svartsengi', 'THOB'), ('svartsengi', 'THNA'), ('svartsengi', 'THOC'),
    ('svartsengi', 'SUND'), ('svartsengi', 'BLAL'), ('svartsengi', 'NBIO'),
    ('svartsengi', 'VOGC'), ('svartsengi', 'VOGS'), ('svartsengi', 'HAFC'),
    ('svartsengi', 'FEFC'), ('svartsengi', 'GONH')
ON CONFLICT DO NOTHING;

-- Hengill
INSERT INTO station_area_members (area_id, sid) VALUES
    ('hengill', 'HVER'), ('hengill', 'HVEH'), ('hengill', 'KLVC'),
    ('hengill', 'HELF'), ('hengill', 'NVEL')
ON CONFLICT DO NOTHING;

-- Hekla
INSERT INTO station_area_members (area_id, sid) VALUES
    ('hekla', 'HESA'), ('hekla', 'FJOC'), ('hekla', 'BALD'),
    ('hekla', 'BUDH'), ('hekla', 'FEDG'), ('hekla', 'NAMC'), ('hekla', 'ISAK')
ON CONFLICT DO NOTHING;

-- Katla
INSERT INTO station_area_members (area_id, sid) VALUES
    ('katla', 'GOLA'), ('katla', 'ENTC'), ('katla', 'HVOL'),
    ('katla', 'HRSC'), ('katla', 'AUST'), ('katla', 'SOHO')
ON CONFLICT DO NOTHING;

-- Eyjafjallajökull
INSERT INTO station_area_members (area_id, sid) VALUES
    ('eyjafjallajokull', 'FIM2'), ('eyjafjallajokull', 'THEY'), ('eyjafjallajokull', 'GOLA')
ON CONFLICT DO NOTHING;

-- Torfajökull
INSERT INTO station_area_members (area_id, sid) VALUES
    ('torfajokull', 'LAHC'), ('torfajokull', 'ALHV'), ('torfajokull', 'HRSC')
ON CONFLICT DO NOTHING;

-- Grímsvötn
INSERT INTO station_area_members (area_id, sid) VALUES
    ('grimsvotn', 'GFUM'), ('grimsvotn', 'GRFS'), ('grimsvotn', 'HAFS'),
    ('grimsvotn', 'JOKU'), ('grimsvotn', 'SKHA')
ON CONFLICT DO NOTHING;

-- Bárðarbunga
INSERT INTO station_area_members (area_id, sid) VALUES
    ('bardarbunga', 'DYNA'), ('bardarbunga', 'DYNC'), ('bardarbunga', 'DYNG'),
    ('bardarbunga', 'DYNY'), ('bardarbunga', 'KISA'), ('bardarbunga', 'VONC')
ON CONFLICT DO NOTHING;

-- Öræfajökull
INSERT INTO station_area_members (area_id, sid) VALUES
    ('oraefajokull', 'KVSK'), ('oraefajokull', 'SVIE'), ('oraefajokull', 'SVIN'),
    ('oraefajokull', 'SKHA'), ('oraefajokull', 'GFEL')
ON CONFLICT DO NOTHING;

-- Askja
INSERT INTO station_area_members (area_id, sid) VALUES
    ('askja', 'DYNY'), ('askja', 'OLAC'), ('askja', 'INSK')
ON CONFLICT DO NOTHING;

-- Krafla
INSERT INTO station_area_members (area_id, sid) VALUES
    ('krafla', 'HLFJ'), ('krafla', 'KRAC'), ('krafla', 'MYVA'), ('krafla', 'NAMC')
ON CONFLICT DO NOTHING;

-- Insert station memberships - Regional areas
-- Reykjanes
INSERT INTO station_area_members (area_id, sid) VALUES
    ('reykjanes', 'GRIV'), ('reykjanes', 'GRVM'), ('reykjanes', 'GRVV'),
    ('reykjanes', 'THOB'), ('reykjanes', 'THNA'), ('reykjanes', 'THOC'),
    ('reykjanes', 'SUND'), ('reykjanes', 'BLAL'), ('reykjanes', 'NBIO'),
    ('reykjanes', 'VOGC'), ('reykjanes', 'VOGS'), ('reykjanes', 'HAFC'),
    ('reykjanes', 'FEFC'), ('reykjanes', 'GONH'), ('reykjanes', 'KEIC'),
    ('reykjanes', 'ELDC'), ('reykjanes', 'KLVC'), ('reykjanes', 'HELF'),
    ('reykjanes', 'HVER'), ('reykjanes', 'HVEH'), ('reykjanes', 'GAKE'),
    ('reykjanes', 'RIFC'), ('reykjanes', 'ELEY'), ('reykjanes', 'HVAS'),
    ('reykjanes', 'AFST')
ON CONFLICT DO NOTHING;

-- South Iceland
INSERT INTO station_area_members (area_id, sid) VALUES
    ('south', 'SELF'), ('south', 'HELC'), ('south', 'HVOL'),
    ('south', 'VMEY'), ('south', 'THEY'), ('south', 'STOR'),
    ('south', 'KALT'), ('south', 'SKRO'), ('south', 'SKDA'),
    ('south', 'LAVI'), ('south', 'HAUD')
ON CONFLICT DO NOTHING;

-- North Iceland
INSERT INTO station_area_members (area_id, sid) VALUES
    ('north', 'AKUR'), ('north', 'MYVA'), ('north', 'HLFJ'),
    ('north', 'GMEY'), ('north', 'KOSK'), ('north', 'RHOF'),
    ('north', 'HOTJ'), ('north', 'HUSM'), ('north', 'BLON'),
    ('north', 'HRAC'), ('north', 'GRAN'), ('north', 'SIFJ'), ('north', 'SJUK')
ON CONFLICT DO NOTHING;

-- East Iceland
INSERT INTO station_area_members (area_id, sid) VALUES
    ('east', 'VOFJ'), ('east', 'SEY1'), ('east', 'SEY2'), ('east', 'SEY3'),
    ('east', 'SEY4'), ('east', 'SEY5'), ('east', 'SEY6'), ('east', 'SEY7'),
    ('east', 'SEY8'), ('east', 'SEY9'), ('east', 'SEYD'), ('east', 'INTA'),
    ('east', 'FAGD'), ('east', 'HAHV')
ON CONFLICT DO NOTHING;

-- West / Westfjords
INSERT INTO station_area_members (area_id, sid) VALUES
    ('west_westfjords', 'ISAF'), ('west_westfjords', 'ISFS'), ('west_westfjords', 'HOLV'),
    ('west_westfjords', 'BJTV'), ('west_westfjords', 'FTEY'), ('west_westfjords', 'HITA'),
    ('west_westfjords', 'FIHO')
ON CONFLICT DO NOTHING;

-- Central Highlands
INSERT INTO station_area_members (area_id, sid) VALUES
    ('central_highlands', 'HVEL'), ('central_highlands', 'LAHC'), ('central_highlands', 'ALHV'),
    ('central_highlands', 'VONC'), ('central_highlands', 'JOKU'), ('central_highlands', 'DYNA'),
    ('central_highlands', 'DYNC'), ('central_highlands', 'EYVI')
ON CONFLICT DO NOTHING;

-- Vatnajökull region
INSERT INTO station_area_members (area_id, sid) VALUES
    ('vatnajokull', 'GFUM'), ('vatnajokull', 'GRFS'), ('vatnajokull', 'HAFS'),
    ('vatnajokull', 'DYNA'), ('vatnajokull', 'DYNC'), ('vatnajokull', 'DYNG'),
    ('vatnajokull', 'DYNY'), ('vatnajokull', 'KISA'), ('vatnajokull', 'VONC'),
    ('vatnajokull', 'KVSK'), ('vatnajokull', 'SVIE'), ('vatnajokull', 'SVIN'),
    ('vatnajokull', 'SKHA'), ('vatnajokull', 'JOKU'), ('vatnajokull', 'GSIG'),
    ('vatnajokull', 'KVEC'), ('vatnajokull', 'OLAC'), ('vatnajokull', 'INSK')
ON CONFLICT DO NOTHING;

-- Create view for easy querying
CREATE OR REPLACE VIEW v_station_areas AS
SELECT
    sa.area_id,
    sa.area_name,
    sa.area_type,
    sa.description,
    sam.sid
FROM station_areas sa
JOIN station_area_members sam ON sa.area_id = sam.area_id
ORDER BY sa.area_type, sa.area_name, sam.sid;
