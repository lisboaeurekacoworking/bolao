PRAGMA foreign_keys = ON;

-- =====================
-- GRUPOS
-- =====================
CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- =====================
-- FASES
-- =====================
CREATE TABLE stages (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- =====================
-- TIMES
-- =====================
CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    flag_code TEXT,
    group_id INTEGER NOT NULL,
    FOREIGN KEY (group_id) REFERENCES groups(id)
);

-- =====================
-- JOGOS
-- =====================
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    team_home_id INTEGER,
    team_away_id INTEGER,

    stage_id INTEGER NOT NULL,

    game_date TEXT NOT NULL,   -- YYYY-MM-DD
    game_time TEXT NOT NULL,   -- HH:MM

    score_home INTEGER,
    score_away INTEGER,

    FOREIGN KEY (team_home_id) REFERENCES teams(id),
    FOREIGN KEY (team_away_id) REFERENCES teams(id),
    FOREIGN KEY (stage_id) REFERENCES stages(id)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE
);

CREATE TABLE predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,

    predicted_home_score INTEGER NOT NULL,
    predicted_away_score INTEGER NOT NULL,

    created_at TEXT NOT NULL,

    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (game_id) REFERENCES games(id),

    UNIQUE (user_id, game_id) );
