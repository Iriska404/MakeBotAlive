"""Логика игры «Киллер». Здесь нет ничего про Telegram — только правила и база."""
import random
import time
from dataclasses import dataclass

from database import Database

# ---- Настройки правил -------------------------------------------------------
MIN_PLAYERS = 3          # меньше трёх играть нельзя: игра идёт до двух выживших
KILL_POINTS = 100.0      # очков за каждое подтверждённое убийство
SURVIVAL_POINTS = 100.0  # максимум очков за время жизни (если прожил всю игру)
NAME_MIN_LEN = 2
NAME_MAX_LEN = 30
# ------------------------------------------------------------------------------


@dataclass
class ScoreRow:
    name: str
    kills: int
    alive: bool
    alive_seconds: float
    score: float


def clean_name(raw):
    """Убирает лишние пробелы; None, если имя не подходит по длине."""
    name = " ".join((raw or "").split())
    if not (NAME_MIN_LEN <= len(name) <= NAME_MAX_LEN):
        return None
    return name


class KillerGame:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ игры
    def create_game(self, chat_id, chat_title, creator_id):
        with self.db.transaction():
            exists = self.db.one(
                "SELECT id FROM games WHERE chat_id = ? AND status IN ('waiting', 'active')",
                (chat_id,),
            )
            if exists:
                return None
            return self.db.execute(
                "INSERT INTO games (chat_id, chat_title, creator_id, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, chat_title, creator_id, time.time()),
            )

    def set_lobby_message(self, game_id, message_id):
        self.db.execute("UPDATE games SET lobby_message_id = ? WHERE id = ?", (message_id, game_id))

    def get_game(self, game_id):
        return self.db.one("SELECT * FROM games WHERE id = ?", (game_id,))

    def get_chat_game(self, chat_id):
        return self.db.one(
            "SELECT * FROM games WHERE chat_id = ? AND status IN ('waiting', 'active')",
            (chat_id,),
        )

    # -------------------------------------------------------------- вступление
    def _set_pending(self, user_id, game_id):
        self.db.execute(
            "INSERT OR REPLACE INTO pending_names (user_id, game_id, created_at) VALUES (?, ?, ?)",
            (user_id, game_id, time.time()),
        )

    def begin_join(self, user_id, game_id):
        """-> ('ask', None) | ('already', имя) | ('closed', None)"""
        g = self.get_game(game_id)
        if not g or g["status"] != "waiting":
            return "closed", None
        me = self.db.one(
            "SELECT name FROM players WHERE game_id = ? AND user_id = ?", (game_id, user_id)
        )
        if me:
            return "already", me["name"]
        self._set_pending(user_id, game_id)
        return "ask", None

    def begin_rename(self, user_id, game_id):
        g = self.get_game(game_id)
        if not g or g["status"] != "waiting":
            return False
        me = self.db.one(
            "SELECT id FROM players WHERE game_id = ? AND user_id = ?", (game_id, user_id)
        )
        if not me:
            return False
        self._set_pending(user_id, game_id)
        return True

    def submit_name(self, user_id, username, raw_name):
        """Игрок прислал имя. -> (код, game_id, имя)
        Коды: no_pending | closed | bad_length | taken | joined | renamed"""
        pending = self.db.one("SELECT game_id FROM pending_names WHERE user_id = ?", (user_id,))
        if not pending:
            return "no_pending", None, None
        game_id = pending["game_id"]
        g = self.get_game(game_id)
        if not g or g["status"] != "waiting":
            self.db.execute("DELETE FROM pending_names WHERE user_id = ?", (user_id,))
            return "closed", game_id, None

        name = clean_name(raw_name)
        if name is None:
            return "bad_length", game_id, None

        with self.db.transaction():
            for p in self.list_players(game_id):
                if p["user_id"] != user_id and p["name"].casefold() == name.casefold():
                    return "taken", game_id, None
            me = self.db.one(
                "SELECT id FROM players WHERE game_id = ? AND user_id = ?", (game_id, user_id)
            )
            if me:
                self.db.execute(
                    "UPDATE players SET name = ?, username = ? WHERE id = ?",
                    (name, username, me["id"]),
                )
                code = "renamed"
            else:
                self.db.execute(
                    "INSERT INTO players (game_id, user_id, username, name, joined_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (game_id, user_id, username, name, time.time()),
                )
                code = "joined"
            self.db.execute("DELETE FROM pending_names WHERE user_id = ?", (user_id,))
        return code, game_id, name

    # ---------------------------------------------------------------- игроки
    def list_players(self, game_id):
        return self.db.all(
            "SELECT * FROM players WHERE game_id = ? ORDER BY joined_at, id", (game_id,)
        )

    def alive_and_dead(self, game_id):
        alive = self.db.all(
            "SELECT name FROM players WHERE game_id = ? AND is_alive = 1 ORDER BY name", (game_id,)
        )
        dead = self.db.all(
            "SELECT name FROM players WHERE game_id = ? AND is_alive = 0 ORDER BY died_at", (game_id,)
        )
        return alive, dead

    def user_active_games(self, user_id):
        """Активные игры, в которых игрок ещё жив."""
        return self.db.all(
            """SELECT g.id AS game_id, g.chat_title
               FROM games g JOIN players p ON p.game_id = g.id
               WHERE p.user_id = ? AND g.status = 'active' AND p.is_alive = 1
               ORDER BY g.created_at DESC""",
            (user_id,),
        )

    def get_target_name(self, game_id, user_id):
        row = self.db.one(
            """SELECT t.name FROM players p JOIN players t ON t.id = p.target_id
               WHERE p.game_id = ? AND p.user_id = ? AND p.is_alive = 1""",
            (game_id, user_id),
        )
        return row["name"] if row else None

    # ------------------------------------------------------------------ старт
    def start_game(self, game_id, user_id):
        """Раздаёт цели по случайному кругу: A -> B -> C -> ... -> A."""
        now = time.time()
        with self.db.transaction():
            g = self.get_game(game_id)
            if not g:
                return {"code": "not_found"}
            if g["creator_id"] != user_id:
                return {"code": "not_creator"}
            if g["status"] != "waiting":
                return {"code": "not_waiting"}
            players = self.list_players(game_id)
            if len(players) < MIN_PLAYERS:
                return {"code": "too_few", "count": len(players)}

            ids = [p["id"] for p in players]
            random.shuffle(ids)
            for i, pid in enumerate(ids):
                self.db.execute(
                    "UPDATE players SET target_id = ? WHERE id = ?", (ids[(i + 1) % len(ids)], pid)
                )
            self.db.execute(
                "UPDATE games SET status = 'active', started_at = ? WHERE id = ?", (now, game_id)
            )
            self.db.execute("DELETE FROM pending_names WHERE game_id = ?", (game_id,))

            by_id = {p["id"]: p for p in players}
            next_of = {pid: ids[(i + 1) % len(ids)] for i, pid in enumerate(ids)}
            assignments = [
                (p["user_id"], p["name"], by_id[next_of[p["id"]]]["name"]) for p in players
            ]
        return {"code": "ok", "assignments": assignments}

    # --------------------------------------------------------------- убийства
    def create_kill_request(self, game_id, user_id):
        now = time.time()
        with self.db.transaction():
            row = self.db.one(
                """SELECT k.id AS killer_pid, k.name AS killer_name, g.status AS game_status,
                          v.id AS victim_pid, v.user_id AS victim_uid, v.name AS victim_name
                   FROM players k
                   JOIN games g ON g.id = k.game_id
                   LEFT JOIN players v ON v.id = k.target_id
                   WHERE k.game_id = ? AND k.user_id = ? AND k.is_alive = 1""",
                (game_id, user_id),
            )
            if not row:
                return {"code": "not_in_game"}
            if row["game_status"] != "active":
                return {"code": "not_active"}
            if row["victim_pid"] is None:
                return {"code": "no_target"}

            # старые неотвеченные запросы этого убийцы закрываем — остаётся только свежий
            self.db.execute(
                "UPDATE kill_requests SET status = 'expired', resolved_at = ? "
                "WHERE killer_id = ? AND status = 'pending'",
                (now, row["killer_pid"]),
            )
            request_id = self.db.execute(
                "INSERT INTO kill_requests (game_id, killer_id, victim_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (game_id, row["killer_pid"], row["victim_pid"], now),
            )
        return {
            "code": "ok",
            "request_id": request_id,
            "victim_user_id": row["victim_uid"],
            "victim_name": row["victim_name"],
            "killer_name": row["killer_name"],
        }

    def expire_request(self, request_id):
        self.db.execute(
            "UPDATE kill_requests SET status = 'expired', resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (time.time(), request_id),
        )

    def resolve_kill(self, request_id, by_user_id, confirm):
        """Жертва подтверждает или отклоняет убийство.
        Коды: not_found | forbidden | stale | invalid | rejected | confirmed"""
        now = time.time()
        with self.db.transaction():
            req = self.db.one(
                """SELECT kr.id, kr.status, kr.game_id,
                          k.id AS killer_pid, k.user_id AS killer_uid, k.name AS killer_name,
                          k.is_alive AS killer_alive, k.target_id AS killer_target,
                          v.id AS victim_pid, v.user_id AS victim_uid, v.name AS victim_name,
                          v.is_alive AS victim_alive, v.target_id AS victim_target,
                          g.status AS game_status, g.chat_id AS chat_id
                   FROM kill_requests kr
                   JOIN players k ON k.id = kr.killer_id
                   JOIN players v ON v.id = kr.victim_id
                   JOIN games g   ON g.id = kr.game_id
                   WHERE kr.id = ?""",
                (request_id,),
            )
            if not req:
                return {"code": "not_found"}
            if req["victim_uid"] != by_user_id:
                return {"code": "forbidden"}
            if req["status"] != "pending":
                return {"code": "stale"}

            info = {
                "game_id": req["game_id"],
                "chat_id": req["chat_id"],
                "killer_user_id": req["killer_uid"],
                "killer_name": req["killer_name"],
                "victim_name": req["victim_name"],
            }

            if not confirm:
                self._close_request(request_id, "rejected", now)
                return {"code": "rejected", **info}

            # проверяем, что ситуация в игре не изменилась с момента запроса
            still_valid = (
                req["game_status"] == "active"
                and req["killer_alive"]
                and req["victim_alive"]
                and req["killer_target"] == req["victim_pid"]
            )
            if not still_valid:
                self._close_request(request_id, "expired", now)
                return {"code": "invalid", **info}

            # жертва выбывает, убийца получает +1 убийство и цель жертвы
            self.db.execute(
                "UPDATE players SET is_alive = 0, died_at = ?, killed_by = ?, target_id = NULL "
                "WHERE id = ?",
                (now, req["killer_pid"], req["victim_pid"]),
            )
            self.db.execute(
                "UPDATE players SET kills_count = kills_count + 1, target_id = ? WHERE id = ?",
                (req["victim_target"], req["killer_pid"]),
            )
            self._close_request(request_id, "confirmed", now)
            # запросы, завязанные на выбывшего игрока, теряют смысл
            self.db.execute(
                "UPDATE kill_requests SET status = 'expired', resolved_at = ? "
                "WHERE status = 'pending' AND (killer_id = ? OR victim_id = ?)",
                (now, req["victim_pid"], req["victim_pid"]),
            )

            alive_count = self.db.one(
                "SELECT COUNT(*) AS c FROM players WHERE game_id = ? AND is_alive = 1",
                (req["game_id"],),
            )["c"]
            finished = alive_count <= 2
            new_target_name = None
            if finished:
                self._finish(req["game_id"], now)
            else:
                new_target_name = self.db.one(
                    "SELECT name FROM players WHERE id = ?", (req["victim_target"],)
                )["name"]

        return {
            "code": "confirmed",
            "alive_count": alive_count,
            "finished": finished,
            "new_target_name": new_target_name,
            **info,
        }

    def _close_request(self, request_id, status, now):
        self.db.execute(
            "UPDATE kill_requests SET status = ?, resolved_at = ? WHERE id = ?",
            (status, now, request_id),
        )

    # ---------------------------------------------------------------- финиш
    def _finish(self, game_id, now):
        self.db.execute(
            "UPDATE games SET status = 'finished', finished_at = ? WHERE id = ?", (now, game_id)
        )
        self.db.execute(
            "UPDATE kill_requests SET status = 'expired', resolved_at = ? "
            "WHERE game_id = ? AND status = 'pending'",
            (now, game_id),
        )

    def finish_game(self, game_id, user_id):
        """Создатель завершает игру досрочно (или отменяет набор).
        Коды: not_found | forbidden | closed | cancelled | finished"""
        now = time.time()
        with self.db.transaction():
            g = self.get_game(game_id)
            if not g:
                return "not_found"
            if g["creator_id"] != user_id:
                return "forbidden"
            if g["status"] == "waiting":
                self.db.execute(
                    "UPDATE games SET status = 'cancelled', finished_at = ? WHERE id = ?",
                    (now, game_id),
                )
                self.db.execute("DELETE FROM pending_names WHERE game_id = ?", (game_id,))
                return "cancelled"
            if g["status"] == "active":
                self._finish(game_id, now)
                return "finished"
            return "closed"

    # ---------------------------------------------------------------- очки
    def scoreboard(self, game_id):
        """Таблица очков, от лучшего к худшему.

        очки = убийства * KILL_POINTS + (доля игры, которую игрок прожил) * SURVIVAL_POINTS
        При равенстве очков: больше убийств, затем дольше прожил.
        """
        g = self.get_game(game_id)
        if not g or g["started_at"] is None:
            return []
        start = g["started_at"]
        end = g["finished_at"] or time.time()
        total = max(end - start, 1.0)

        rows = []
        for p in self.list_players(game_id):
            p_end = p["died_at"] if p["died_at"] else end
            alive_seconds = max(0.0, min(p_end, end) - start)
            score = p["kills_count"] * KILL_POINTS + (alive_seconds / total) * SURVIVAL_POINTS
            rows.append(
                ScoreRow(
                    name=p["name"],
                    kills=p["kills_count"],
                    alive=bool(p["is_alive"]),
                    alive_seconds=alive_seconds,
                    score=round(score, 1),
                )
            )
        rows.sort(key=lambda r: (-r.score, -r.kills, -r.alive_seconds))
        return rows
