"""Слой хранения данных: SQLite, схема и пара удобных помощников."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    chat_title       TEXT,
    creator_id       INTEGER NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'waiting',  -- waiting | active | finished | cancelled
    lobby_message_id INTEGER,
    created_at       REAL    NOT NULL,
    started_at       REAL,
    finished_at      REAL
);

CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL REFERENCES games (id),
    user_id     INTEGER NOT NULL,
    username    TEXT,
    name        TEXT    NOT NULL,                  -- имя, которое игрок вписал сам
    target_id   INTEGER REFERENCES players (id),
    is_alive    INTEGER NOT NULL DEFAULT 1,
    kills_count INTEGER NOT NULL DEFAULT 0,
    joined_at   REAL    NOT NULL,
    died_at     REAL,
    killed_by   INTEGER REFERENCES players (id),
    UNIQUE (game_id, user_id)
);

CREATE TABLE IF NOT EXISTS kill_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL REFERENCES games (id),
    killer_id   INTEGER NOT NULL REFERENCES players (id),
    victim_id   INTEGER NOT NULL REFERENCES players (id),
    status      TEXT    NOT NULL DEFAULT 'pending',   -- pending | confirmed | rejected | expired
    created_at  REAL    NOT NULL,
    resolved_at REAL
);

-- Кто сейчас должен прислать боту своё имя (пока идёт вступление в игру)
CREATE TABLE IF NOT EXISTS pending_names (
    user_id    INTEGER PRIMARY KEY,
    game_id    INTEGER NOT NULL REFERENCES games (id),
    created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_players_game  ON players (game_id);
CREATE INDEX IF NOT EXISTS idx_players_user  ON players (user_id);
CREATE INDEX IF NOT EXISTS idx_games_chat    ON games (chat_id, status);
CREATE INDEX IF NOT EXISTS idx_requests_game ON kill_requests (game_id, status);
"""


class Database:
    def __init__(self, path="killer.db"):
        self.path = path
        # isolation_level=None -> автокоммит; транзакциями управляем вручную
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def execute(self, query, params=()):
        """Выполнить запрос, вернуть lastrowid."""
        return self.conn.execute(query, params).lastrowid

    def one(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def all(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    @contextmanager
    def transaction(self):
        """Всё внутри блока применяется целиком или не применяется вовсе."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
