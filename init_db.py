"""
init_db.py — Inicializa a base de dados em produção

Cria todas as tabelas e insere os dados base (grupos, fases, equipas, jogos).
Seguro para correr várias vezes — usa IF NOT EXISTS em todas as tabelas.

Uso:
    python init_db.py
"""

import sqlite3
import os

DB_PATH = os.environ.get("DATABASE_PATH", "database.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"A inicializar base de dados em: {DB_PATH}")

    # ======================
    # CRIAR TABELAS
    # ======================
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stages (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            flag_code TEXT,
            group_id INTEGER NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(id)
        );

        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_game_id TEXT,
            team_home_id INTEGER,
            team_away_id INTEGER,
            stage_id INTEGER NOT NULL,
            game_date TEXT,
            game_time TEXT,
            game_datetime TEXT,
            score_home INTEGER,
            score_away INTEGER,
            FOREIGN KEY (team_home_id) REFERENCES teams(id),
            FOREIGN KEY (team_away_id) REFERENCES teams(id),
            FOREIGN KEY (stage_id) REFERENCES stages(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            created_at TEXT,
            country_code TEXT,
            eureka_unit TEXT,
            is_admin INTEGER DEFAULT 0,
            privacy_consent_at TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL,
            predicted_home_score INTEGER NOT NULL,
            predicted_away_score INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (game_id) REFERENCES games(id),
            UNIQUE (user_id, game_id)
        );

        CREATE TABLE IF NOT EXISTS bettalks_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bettalks_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES bettalks_posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bettalks_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES bettalks_posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE (post_id, user_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bettalks_likes_unique
            ON bettalks_likes(post_id, user_id);
    """)

    print("✓ Tabelas criadas")

    # ======================
    # GRUPOS
    # ======================
    existing_groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    if existing_groups == 0:
        conn.executescript("""
            INSERT INTO groups VALUES
            (1,'Grupo A'),(2,'Grupo B'),(3,'Grupo C'),(4,'Grupo D'),
            (5,'Grupo E'),(6,'Grupo F'),(7,'Grupo G'),(8,'Grupo H'),
            (9,'Grupo I'),(10,'Grupo J'),(11,'Grupo K'),(12,'Grupo L');
        """)
        print("✓ Grupos inseridos")
    else:
        print("  Grupos já existem — ignorado")

    # ======================
    # FASES
    # ======================
    existing_stages = conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0]
    if existing_stages == 0:
        conn.executescript("""
            INSERT INTO stages VALUES
            (1,'Primeira Fase'),
            (2,'32 Avos'),
            (3,'Oitavas de Final'),
            (4,'Quartas de Final'),
            (5,'Semifinal'),
            (6,'Terceiro Lugar'),
            (7,'Final');
        """)
        print("✓ Fases inseridas")
    else:
        print("  Fases já existem — ignorado")

    # ======================
    # EQUIPAS
    # ======================
    existing_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    if existing_teams == 0:
        conn.executescript("""
            INSERT INTO teams VALUES
            (1,'México','mx',1),(2,'África do Sul','za',1),(3,'Coreia do Sul','kr',1),(4,'República Tcheca','cz',1),
            (5,'Canadá','ca',2),(6,'Bósnia e Herzegovina','ba',2),(7,'Suíça','ch',2),(8,'Catar','qa',2),
            (9,'Brasil','br',3),(10,'Marrocos','ma',3),(11,'Escócia','gb-sct',3),(12,'Haiti','ht',3),
            (13,'Estados Unidos','us',4),(14,'Paraguai','py',4),(15,'Austrália','au',4),(16,'Turquia','tr',4),
            (17,'Alemanha','de',5),(18,'Curaçao','cw',5),(19,'Equador','ec',5),(20,'Costa do Marfim','ci',5),
            (21,'Países Baixos','nl',6),(22,'Japão','jp',6),(23,'Tunísia','tn',6),(24,'Suécia','se',6),
            (25,'Bélgica','be',7),(26,'Egito','eg',7),(27,'Irã','ir',7),(28,'Nova Zelândia','nz',7),
            (29,'Arábia Saudita','sa',8),(30,'Uruguai','uy',8),(31,'Espanha','es',8),(32,'Cabo Verde','cv',8),
            (33,'França','fr',9),(34,'Senegal','sn',9),(35,'Noruega','no',9),(36,'Iraque','iq',9),
            (37,'Argentina','ar',10),(38,'Argélia','dz',10),(39,'Áustria','at',10),(40,'Jordânia','jo',10),
            (41,'Croácia','hr',11),(42,'Inglaterra','gb-eng',11),(43,'Panamá','pa',11),(44,'Gana','gh',11),
            (45,'Portugal','pt',12),(46,'República Democrática do Congo','cd',12),(47,'Uzbequistão','uz',12),(48,'Colômbia','co',12);
        """)
        print("✓ Equipas inseridas")
    else:
        print("  Equipas já existem — ignorado")

    # ======================
    # JOGOS
    # ======================
    existing_games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    if existing_games == 0:
        conn.executescript("""
            INSERT INTO games (team_home_id, team_away_id, stage_id, game_date, game_time, game_datetime) VALUES
            -- Grupo A
            (1,2,1,'2026-06-11','20:00','2026-06-11 20:00:00'),
            (3,4,1,'2026-06-12','03:00','2026-06-12 03:00:00'),
            (2,4,1,'2026-06-18','17:00','2026-06-18 17:00:00'),
            (1,3,1,'2026-06-19','02:00','2026-06-19 02:00:00'),
            (2,3,1,'2026-06-25','02:00','2026-06-25 02:00:00'),
            (4,1,1,'2026-06-25','02:00','2026-06-25 02:00:00'),
            -- Grupo B
            (5,6,1,'2026-06-12','20:00','2026-06-12 20:00:00'),
            (7,8,1,'2026-06-13','20:00','2026-06-13 20:00:00'),
            (7,6,1,'2026-06-18','20:00','2026-06-18 20:00:00'),
            (5,8,1,'2026-06-18','23:00','2026-06-18 23:00:00'),
            (5,7,1,'2026-06-24','20:00','2026-06-24 20:00:00'),
            (6,8,1,'2026-06-24','20:00','2026-06-24 20:00:00'),
            -- Grupo C
            (9,10,1,'2026-06-13','23:00','2026-06-13 23:00:00'),
            (11,12,1,'2026-06-13','20:00','2026-06-13 20:00:00'),
            (9,12,1,'2026-06-20','02:00','2026-06-20 02:00:00'),
            (11,10,1,'2026-06-19','23:00','2026-06-19 23:00:00'),
            (10,12,1,'2026-06-24','23:00','2026-06-24 23:00:00'),
            (9,11,1,'2026-06-24','23:00','2026-06-24 23:00:00'),
            -- Grupo D
            (13,14,1,'2026-06-13','02:00','2026-06-13 02:00:00'),
            (15,16,1,'2026-06-14','05:00','2026-06-14 05:00:00'),
            (13,15,1,'2026-06-19','20:00','2026-06-19 20:00:00'),
            (14,16,1,'2026-06-20','05:00','2026-06-20 05:00:00'),
            (13,16,1,'2026-06-26','03:00','2026-06-26 03:00:00'),
            (15,14,1,'2026-06-26','03:00','2026-06-26 03:00:00'),
            -- Grupo E
            (17,18,1,'2026-06-14','18:00','2026-06-14 18:00:00'),
            (19,20,1,'2026-06-15','00:00','2026-06-15 00:00:00'),
            (17,20,1,'2026-06-20','21:00','2026-06-20 21:00:00'),
            (18,19,1,'2026-06-21','01:00','2026-06-21 01:00:00'),
            (17,19,1,'2026-06-25','21:00','2026-06-25 21:00:00'),
            (18,20,1,'2026-06-25','21:00','2026-06-25 21:00:00'),
            -- Grupo F
            (21,22,1,'2026-06-14','21:00','2026-06-14 21:00:00'),
            (23,24,1,'2026-06-15','03:00','2026-06-15 03:00:00'),
            (22,23,1,'2026-06-21','05:00','2026-06-21 05:00:00'),
            (21,24,1,'2026-06-20','18:00','2026-06-20 18:00:00'),
            (21,23,1,'2026-06-26','00:00','2026-06-26 00:00:00'),
            (22,24,1,'2026-06-26','00:00','2026-06-26 00:00:00'),
            -- Grupo G
            (25,26,1,'2026-06-15','06:00','2026-06-15 06:00:00'),
            (27,28,1,'2026-06-16','02:00','2026-06-16 02:00:00'),
            (25,27,1,'2026-06-21','20:00','2026-06-21 20:00:00'),
            (26,28,1,'2026-06-22','02:00','2026-06-22 02:00:00'),
            (25,28,1,'2026-06-27','04:00','2026-06-27 04:00:00'),
            (26,27,1,'2026-06-27','04:00','2026-06-27 04:00:00'),
            -- Grupo H
            (29,30,1,'2026-06-15','23:00','2026-06-15 23:00:00'),
            (31,32,1,'2026-06-15','17:00','2026-06-15 17:00:00'),
            (29,31,1,'2026-06-21','17:00','2026-06-21 17:00:00'),
            (32,30,1,'2026-06-21','23:00','2026-06-21 23:00:00'),
            (29,32,1,'2026-06-27','01:00','2026-06-27 01:00:00'),
            (30,31,1,'2026-06-27','01:00','2026-06-27 01:00:00'),
            -- Grupo I
            (33,34,1,'2026-06-16','20:00','2026-06-16 20:00:00'),
            (35,36,1,'2026-06-16','23:00','2026-06-16 23:00:00'),
            (33,36,1,'2026-06-22','22:00','2026-06-22 22:00:00'),
            (35,34,1,'2026-06-23','01:00','2026-06-23 01:00:00'),
            (33,35,1,'2026-06-26','20:00','2026-06-26 20:00:00'),
            (34,36,1,'2026-06-26','20:00','2026-06-26 20:00:00'),
            -- Grupo J
            (39,40,1,'2026-06-17','05:00','2026-06-17 05:00:00'),
            (37,38,1,'2026-06-17','02:00','2026-06-17 02:00:00'),
            (37,39,1,'2026-06-22','18:00','2026-06-22 18:00:00'),
            (38,40,1,'2026-06-23','04:00','2026-06-23 04:00:00'),
            (38,39,1,'2026-06-28','03:00','2026-06-28 03:00:00'),
            (37,40,1,'2026-06-28','03:00','2026-06-28 03:00:00'),
            -- Grupo K
            (45,46,1,'2026-06-17','18:00','2026-06-17 18:00:00'),
            (47,48,1,'2026-06-18','03:00','2026-06-18 03:00:00'),
            (45,47,1,'2026-06-23','18:00','2026-06-23 18:00:00'),
            (46,48,1,'2026-06-24','03:00','2026-06-24 03:00:00'),
            (45,48,1,'2026-06-28','00:30','2026-06-28 00:30:00'),
            (46,47,1,'2026-06-28','00:30','2026-06-28 00:30:00'),
            -- Grupo L
            (41,42,1,'2026-06-17','21:00','2026-06-17 21:00:00'),
            (43,44,1,'2026-06-18','03:00','2026-06-18 03:00:00'),
            (42,44,1,'2026-06-23','21:00','2026-06-23 21:00:00'),
            (43,41,1,'2026-06-24','00:00','2026-06-24 00:00:00'),
            (41,44,1,'2026-06-27','22:00','2026-06-27 22:00:00'),
            (43,42,1,'2026-06-27','22:00','2026-06-27 22:00:00');
        """)
        print("✓ Jogos inseridos")
    else:
        print("  Jogos já existem — ignorado")

    conn.commit()
    conn.close()
    print("\n✓ Base de dados inicializada com sucesso!")

if __name__ == "__main__":
    init_db()
