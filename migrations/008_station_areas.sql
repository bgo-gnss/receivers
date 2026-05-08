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
    ('askja',            'Askja',             'volcanic', 'Askja volcanic system'),
    ('bardarbunga',      'Bárðarbunga',        'volcanic', 'Bárðarbunga volcanic system (Holuhraun)'),
    ('eyjafjallajokull', 'Eyjafjallajökull',   'volcanic', 'Eyjafjallajökull volcanic system'),
    ('grimsvotn',        'Grímsvötn',          'volcanic', 'Grímsvötn volcanic system'),
    ('hekla',            'Hekla',              'volcanic', 'Hekla volcanic system'),
    ('hengill',          'Hengill',            'volcanic', 'Hengill volcanic system'),
    ('katla',            'Katla',              'volcanic', 'Katla volcanic system (Mýrdalsjökull)'),
    ('krafla',           'Krafla',             'volcanic', 'Krafla volcanic system'),
    ('krysuvik',         'Krýsuvík',           'volcanic', 'Krýsuvík-Trölladyngja volcanic system'),
    ('oraefajokull',     'Öræfajökull',        'volcanic', 'Öræfajökull volcanic system'),
    ('svartsengi',       'Svartsengi',         'volcanic', 'Svartsengi/Sundhnúkur volcanic system'),
    ('torfajokull',      'Torfajökull',        'volcanic', 'Torfajökull volcanic system')
ON CONFLICT (area_id) DO NOTHING;

-- Insert regional monitoring areas
INSERT INTO station_areas (area_id, area_name, area_type, description) VALUES
    ('central_highlands', 'Central Highlands',   'regional', 'Central highland interior'),
    ('east',              'East Iceland',         'regional', 'East Iceland'),
    ('north',             'North Iceland',        'regional', 'North Iceland'),
    ('reykjanes',         'Reykjanes',            'regional', 'Reykjanes Peninsula'),
    ('south',             'South Iceland',        'regional', 'South Iceland coastal region'),
    ('vatnajokull',       'Vatnajökull',          'regional', 'Vatnajökull glacier region'),
    ('west_westfjords',   'West / Westfjords',    'regional', 'West Iceland and Westfjords')
ON CONFLICT (area_id) DO NOTHING;

-- Insert station memberships - Volcanic areas

-- Askja
INSERT INTO station_area_members (area_id, sid) VALUES
    ('askja', 'DYNG'), ('askja', 'DYNY'), ('askja', 'HRIC'), ('askja', 'INSK'),
    ('askja', 'JONC'), ('askja', 'KASC'), ('askja', 'LANH'), ('askja', 'MOFC'),
    ('askja', 'OLAC'), ('askja', 'TANC'), ('askja', 'THOC')
ON CONFLICT DO NOTHING;

-- Bárðarbunga
INSERT INTO station_area_members (area_id, sid) VALUES
    ('bardarbunga', 'DYNA'), ('bardarbunga', 'DYNC'), ('bardarbunga', 'DYNG'),
    ('bardarbunga', 'DYNY'), ('bardarbunga', 'GFUM'), ('bardarbunga', 'GJAC'),
    ('bardarbunga', 'GRVC'), ('bardarbunga', 'HAFS'), ('bardarbunga', 'HSKC'),
    ('bardarbunga', 'KISA'), ('bardarbunga', 'RJUC'), ('bardarbunga', 'URHC'),
    ('bardarbunga', 'VONC')
ON CONFLICT DO NOTHING;

-- Eyjafjallajökull
INSERT INTO station_area_members (area_id, sid) VALUES
    ('eyjafjallajokull', 'FIM2'), ('eyjafjallajokull', 'GOLA'),
    ('eyjafjallajokull', 'STE2'), ('eyjafjallajokull', 'THEY')
ON CONFLICT DO NOTHING;

-- Grímsvötn
INSERT INTO station_area_members (area_id, sid) VALUES
    ('grimsvotn', 'GFUM'), ('grimsvotn', 'GRFS'), ('grimsvotn', 'GRVC'),
    ('grimsvotn', 'HAFS'), ('grimsvotn', 'JOKU'), ('grimsvotn', 'SKHA')
ON CONFLICT DO NOTHING;

-- Hekla
INSERT INTO station_area_members (area_id, sid) VALUES
    ('hekla', 'BALD'), ('hekla', 'BUDH'), ('hekla', 'FEDG'), ('hekla', 'FJOC'),
    ('hekla', 'GLER'), ('hekla', 'HAUD'), ('hekla', 'HESA'), ('hekla', 'ISAK'),
    ('hekla', 'MJSK'), ('hekla', 'NAMC'), ('hekla', 'NORS'), ('hekla', 'SODU')
ON CONFLICT DO NOTHING;

-- Hengill
INSERT INTO station_area_members (area_id, sid) VALUES
    ('hengill', 'HELF'), ('hengill', 'HUSM'), ('hengill', 'HVEH'),
    ('hengill', 'HVER'), ('hengill', 'KLVC'), ('hengill', 'NVEL'), ('hengill', 'OLKE')
ON CONFLICT DO NOTHING;

-- Katla
INSERT INTO station_area_members (area_id, sid) VALUES
    ('katla', 'AUST'), ('katla', 'ENTC'), ('katla', 'FIM2'), ('katla', 'GOLA'),
    ('katla', 'GRFS'), ('katla', 'HRSC'), ('katla', 'HVOL'), ('katla', 'OFEL'),
    ('katla', 'RFEL'), ('katla', 'SOHO')
ON CONFLICT DO NOTHING;

-- Krafla
INSERT INTO station_area_members (area_id, sid) VALUES
    ('krafla', 'HLFJ'), ('krafla', 'KRAC'), ('krafla', 'MYVA'), ('krafla', 'NAMC')
ON CONFLICT DO NOTHING;

-- Krýsuvík
INSERT INTO station_area_members (area_id, sid) VALUES
    ('krysuvik', 'FAGD'), ('krysuvik', 'GONH'), ('krysuvik', 'KEIC'), ('krysuvik', 'KLVC'),
    ('krysuvik', 'KRIV'), ('krysuvik', 'MOHA'), ('krysuvik', 'ODDF'), ('krysuvik', 'STAN')
ON CONFLICT DO NOTHING;

-- Öræfajökull
INSERT INTO station_area_members (area_id, sid) VALUES
    ('oraefajokull', 'FAGC'), ('oraefajokull', 'GFEL'), ('oraefajokull', 'KOTC'),
    ('oraefajokull', 'KVIC'), ('oraefajokull', 'KVSK'), ('oraefajokull', 'ROTH'),
    ('oraefajokull', 'SKFC'), ('oraefajokull', 'SKHA'), ('oraefajokull', 'SLEC'),
    ('oraefajokull', 'SVIE'), ('oraefajokull', 'SVIN')
ON CONFLICT DO NOTHING;

-- Svartsengi
INSERT INTO station_area_members (area_id, sid) VALUES
    ('svartsengi', 'ASVE'), ('svartsengi', 'AUSV'), ('svartsengi', 'BLAL'),
    ('svartsengi', 'ELDC'), ('svartsengi', 'FEFC'), ('svartsengi', 'GEVK'),
    ('svartsengi', 'GONH'), ('svartsengi', 'GRIV'), ('svartsengi', 'GRVM'),
    ('svartsengi', 'GRVV'), ('svartsengi', 'HAFC'), ('svartsengi', 'HRAG'),
    ('svartsengi', 'HS02'), ('svartsengi', 'KAST'), ('svartsengi', 'LISF'),
    ('svartsengi', 'NAMC'), ('svartsengi', 'NBIO'), ('svartsengi', 'NORV'),
    ('svartsengi', 'ORFC'), ('svartsengi', 'SENG'), ('svartsengi', 'SKSH'),
    ('svartsengi', 'SUDV'), ('svartsengi', 'SUND'), ('svartsengi', 'THNA'),
    ('svartsengi', 'THOB'), ('svartsengi', 'THOC'), ('svartsengi', 'VMOS'),
    ('svartsengi', 'VOGC'), ('svartsengi', 'VOGS')
ON CONFLICT DO NOTHING;

-- Torfajökull
INSERT INTO station_area_members (area_id, sid) VALUES
    ('torfajokull', 'ALHV'), ('torfajokull', 'HRSC'), ('torfajokull', 'LAHC'),
    ('torfajokull', 'STHV'), ('torfajokull', 'TORK')
ON CONFLICT DO NOTHING;

-- Insert station memberships - Regional areas

-- Central Highlands
INSERT INTO station_area_members (area_id, sid) VALUES
    ('central_highlands', 'ALHV'), ('central_highlands', 'BLEI'), ('central_highlands', 'BUDH'),
    ('central_highlands', 'DYNA'), ('central_highlands', 'DYNC'), ('central_highlands', 'DYNG'),
    ('central_highlands', 'DYNY'), ('central_highlands', 'EYVI'), ('central_highlands', 'FEDG'),
    ('central_highlands', 'FITC'), ('central_highlands', 'FJOC'), ('central_highlands', 'GFUM'),
    ('central_highlands', 'GIGO'), ('central_highlands', 'GJAC'), ('central_highlands', 'GRVA'),
    ('central_highlands', 'GRVC'), ('central_highlands', 'GSIG'), ('central_highlands', 'HAFS'),
    ('central_highlands', 'HAUC'), ('central_highlands', 'HESA'), ('central_highlands', 'HRIC'),
    ('central_highlands', 'HSKC'), ('central_highlands', 'HVEL'), ('central_highlands', 'ICEB'),
    ('central_highlands', 'ICEC'), ('central_highlands', 'INSK'), ('central_highlands', 'ISAK'),
    ('central_highlands', 'JOKU'), ('central_highlands', 'JONC'), ('central_highlands', 'KASC'),
    ('central_highlands', 'KIDC'), ('central_highlands', 'KISA'), ('central_highlands', 'KVEC'),
    ('central_highlands', 'LAHC'), ('central_highlands', 'LANH'), ('central_highlands', 'LFEL'),
    ('central_highlands', 'MOFC'), ('central_highlands', 'NORS'), ('central_highlands', 'OLAC'),
    ('central_highlands', 'RJUC'), ('central_highlands', 'SKFC'), ('central_highlands', 'SKRO'),
    ('central_highlands', 'STKA'), ('central_highlands', 'SVIE'), ('central_highlands', 'SVIN'),
    ('central_highlands', 'TANC'), ('central_highlands', 'THOC'), ('central_highlands', 'URHC'),
    ('central_highlands', 'VONC')
ON CONFLICT DO NOTHING;

-- East Iceland
INSERT INTO station_area_members (area_id, sid) VALUES
    ('east', 'ALFD'), ('east', 'BALD'), ('east', 'EYVI'), ('east', 'FAGD'),
    ('east', 'HAHV'), ('east', 'HEID'), ('east', 'INTA'), ('east', 'LAVI'),
    ('east', 'SAUD'), ('east', 'SEY1'), ('east', 'SEY2'), ('east', 'SEY3'),
    ('east', 'SEY4'), ('east', 'SEY5'), ('east', 'SEY6'), ('east', 'SEY7'),
    ('east', 'SEY8'), ('east', 'SEY9'), ('east', 'SEYD'), ('east', 'VOFJ')
ON CONFLICT DO NOTHING;

-- North Iceland
INSERT INTO station_area_members (area_id, sid) VALUES
    ('north', 'AKUR'), ('north', 'ARHO'), ('north', 'BAUG'), ('north', 'BJTV'),
    ('north', 'BLON'), ('north', 'BRIK'), ('north', 'BRTT'), ('north', 'FTEY'),
    ('north', 'GAKE'), ('north', 'GJFV'), ('north', 'GJOG'), ('north', 'GMEY'),
    ('north', 'GRAN'), ('north', 'HEDI'), ('north', 'HELC'), ('north', 'HLFJ'),
    ('north', 'HOLV'), ('north', 'HOTJ'), ('north', 'HRAC'), ('north', 'HUSM'),
    ('north', 'ISAF'), ('north', 'ISFS'), ('north', 'KOSK'), ('north', 'KRAC'),
    ('north', 'KVIS'), ('north', 'MANA'), ('north', 'MYVA'), ('north', 'RHOF'),
    ('north', 'RHOL'), ('north', 'SAVI'), ('north', 'SIFJ'), ('north', 'SJUK'),
    ('north', 'THRC'), ('north', 'VARG'), ('north', 'VOFJ')
ON CONFLICT DO NOTHING;

-- Reykjanes Peninsula
INSERT INTO station_area_members (area_id, sid) VALUES
    ('reykjanes', 'AFST'), ('reykjanes', 'ASVE'), ('reykjanes', 'AUSV'),
    ('reykjanes', 'BLAL'), ('reykjanes', 'ELDC'), ('reykjanes', 'ELEY'),
    ('reykjanes', 'FAGD'), ('reykjanes', 'FEFC'), ('reykjanes', 'GAKE'),
    ('reykjanes', 'GEVK'), ('reykjanes', 'GONH'), ('reykjanes', 'GRIV'),
    ('reykjanes', 'GRVM'), ('reykjanes', 'GRVV'), ('reykjanes', 'HAFC'),
    ('reykjanes', 'HELF'), ('reykjanes', 'HERV'), ('reykjanes', 'HRAG'),
    ('reykjanes', 'HS02'), ('reykjanes', 'HVAS'), ('reykjanes', 'HVEH'),
    ('reykjanes', 'HVER'), ('reykjanes', 'HVSK'), ('reykjanes', 'KAST'),
    ('reykjanes', 'KEIC'), ('reykjanes', 'KLVC'), ('reykjanes', 'KRIV'),
    ('reykjanes', 'LISF'), ('reykjanes', 'MOHA'), ('reykjanes', 'NAMC'),
    ('reykjanes', 'NBIO'), ('reykjanes', 'NORV'), ('reykjanes', 'NYLA'),
    ('reykjanes', 'ODDF'), ('reykjanes', 'ORFC'), ('reykjanes', 'RIFC'),
    ('reykjanes', 'RVIT'), ('reykjanes', 'SAFH'), ('reykjanes', 'SENG'),
    ('reykjanes', 'SKSH'), ('reykjanes', 'STAN'), ('reykjanes', 'SUDV'),
    ('reykjanes', 'SUND'), ('reykjanes', 'SYRF'), ('reykjanes', 'THNA'),
    ('reykjanes', 'THOB'), ('reykjanes', 'THOC'), ('reykjanes', 'UNDH'),
    ('reykjanes', 'VMOS'), ('reykjanes', 'VOGC'), ('reykjanes', 'VOGS')
ON CONFLICT DO NOTHING;

-- South Iceland
INSERT INTO station_area_members (area_id, sid) VALUES
    ('south', 'ALHV'), ('south', 'AUST'), ('south', 'ELDV'), ('south', 'ENTC'),
    ('south', 'FIM2'), ('south', 'GOLA'), ('south', 'GRFS'), ('south', 'HAMR'),
    ('south', 'HAUD'), ('south', 'HELC'), ('south', 'HRSC'), ('south', 'HVOL'),
    ('south', 'KALF'), ('south', 'KALT'), ('south', 'LAVI'), ('south', 'MJSK'),
    ('south', 'OFEL'), ('south', 'RFEL'), ('south', 'SELF'), ('south', 'SKDA'),
    ('south', 'SKRO'), ('south', 'SNAE'), ('south', 'SOHO'), ('south', 'STE2'),
    ('south', 'STOR'), ('south', 'THEY'), ('south', 'TORK'), ('south', 'VMEY')
ON CONFLICT DO NOTHING;

-- Vatnajökull region
INSERT INTO station_area_members (area_id, sid) VALUES
    ('vatnajokull', 'ALFD'), ('vatnajokull', 'BALD'), ('vatnajokull', 'DYNA'),
    ('vatnajokull', 'DYNC'), ('vatnajokull', 'DYNG'), ('vatnajokull', 'DYNY'),
    ('vatnajokull', 'EYVI'), ('vatnajokull', 'FJOC'), ('vatnajokull', 'GFUM'),
    ('vatnajokull', 'GIGO'), ('vatnajokull', 'GJAC'), ('vatnajokull', 'GRFS'),
    ('vatnajokull', 'GRVC'), ('vatnajokull', 'GSIG'), ('vatnajokull', 'HAFS'),
    ('vatnajokull', 'HAHV'), ('vatnajokull', 'HAUC'), ('vatnajokull', 'HRIC'),
    ('vatnajokull', 'HSKC'), ('vatnajokull', 'ICEB'), ('vatnajokull', 'ICEC'),
    ('vatnajokull', 'INSK'), ('vatnajokull', 'INTA'), ('vatnajokull', 'JOKU'),
    ('vatnajokull', 'JONC'), ('vatnajokull', 'KALF'), ('vatnajokull', 'KASC'),
    ('vatnajokull', 'KIDC'), ('vatnajokull', 'KISA'), ('vatnajokull', 'KOTC'),
    ('vatnajokull', 'KVEC'), ('vatnajokull', 'KVIC'), ('vatnajokull', 'KVSK'),
    ('vatnajokull', 'LANH'), ('vatnajokull', 'MOFC'), ('vatnajokull', 'OLAC'),
    ('vatnajokull', 'RIFC'), ('vatnajokull', 'RJUC'), ('vatnajokull', 'ROTH'),
    ('vatnajokull', 'SAUD'), ('vatnajokull', 'SKFC'), ('vatnajokull', 'SKHA'),
    ('vatnajokull', 'SKRO'), ('vatnajokull', 'SLEC'), ('vatnajokull', 'SVIE'),
    ('vatnajokull', 'SVIN'), ('vatnajokull', 'TANC'), ('vatnajokull', 'THOC'),
    ('vatnajokull', 'URHC'), ('vatnajokull', 'VONC')
ON CONFLICT DO NOTHING;

-- West Iceland and Westfjords
INSERT INTO station_area_members (area_id, sid) VALUES
    ('west_westfjords', 'BJTV'), ('west_westfjords', 'FIHO'), ('west_westfjords', 'FTEY'),
    ('west_westfjords', 'GUSK'), ('west_westfjords', 'HITA'), ('west_westfjords', 'HOLV'),
    ('west_westfjords', 'HRAH'), ('west_westfjords', 'ISAF'), ('west_westfjords', 'ISFS'),
    ('west_westfjords', 'RHOL')
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
