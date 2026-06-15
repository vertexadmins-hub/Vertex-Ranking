import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=ENV_PATH)

import discord
from discord.ext import commands


TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(__file__).with_name("mmr.sqlite3")
RANKING_JSON_PATH = Path(__file__).with_name("ranking.json")

INITIAL_MMR = 100
MIN_MATCH_MMR_CHANGE = 10
MAX_MATCH_MMR_CHANGE = 40
ELO_RATING_SCALE = 400
ELO_DIFFERENCE_LIMIT = 800

DYNAMIC_K_POINTS = (
    (100, 40),
    (300, 36),
    (600, 32),
    (900, 27),
    (1200, 22),
    (1500, 18),
)

# Replace these values with the real Discord channel IDs.
RANKING_CHANNEL_ID = 1515528127533682758
HISTORIAL_CHANNEL_ID = 1515528097661980704
STAFF_COMMANDS_CHANNEL_ID = 1515531123344805888

# Replace these values with the real Discord role IDs.
ROLE_0_300_ID = 1515854286049906829
ROLE_300_ID = 1515854413300764722
ROLE_650_ID = 1515854665986605216
ROLE_1050_ID = 1515854824460128367
ROLE_1500_ID = 1515854929196224615
ROLE_2000_ID = 1515854984850313246
ROLE_3000_ID = 1515855041263829223

MMR_ROLE_THRESHOLDS = (
    (3000, ROLE_3000_ID, "+3000"),
    (2000, ROLE_2000_ID, "+2000"),
    (1500, ROLE_1500_ID, "+1500"),
    (1050, ROLE_1050_ID, "+1050"),
    (650, ROLE_650_ID, "+650"),
    (300, ROLE_300_ID, "+300"),
    (0, ROLE_0_300_ID, "0-300"),
)
MMR_ROLE_IDS = frozenset(
    role_id
    for _, role_id, _ in MMR_ROLE_THRESHOLDS
    if role_id
)

RANKING_CHANNEL_NAME = "ranking"
HISTORIAL_CHANNEL_NAME = "historial"
STAFF_COMMANDS_CHANNEL_NAME = "staff-commands"

RANKING_ALLOWED_COMMANDS = {"register", "mmr", "ranking"}
MATCH_TYPES = {"ranked", "event", "admin"}
MANUAL_SOURCE_TYPES = {"event", "admin"}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
logger = logging.getLogger("mmr_bot")


# ─────────────────────────────────────────
#  EXPORTAR RANKING A JSON (para la web)
# ─────────────────────────────────────────
def exportar_ranking():
    try:
        with get_connection() as connection:
            players = connection.execute(
                """
                SELECT username, current_mmr, wins, losses
                FROM users
                ORDER BY current_mmr DESC, wins DESC, username COLLATE NOCASE
                """
            ).fetchall()

        data = [
            {
                "nombre": p["username"],
                "mmr": p["current_mmr"],
                "wins": p["wins"],
                "losses": p["losses"],
            }
            for p in players
        ]

        with open(RANKING_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.warning("Could not export ranking.json: %s", e)


class ChannelRestrictionError(commands.CheckFailure):
    pass


class DuplicateRecordError(RuntimeError):
    pass


def is_configured_channel(channel, channel_id):
    return channel.id == channel_id


def only_in_channel(channel_id, channel_name):
    async def predicate(ctx):
        if not is_configured_channel(ctx.channel, channel_id):
            raise ChannelRestrictionError(
                f"This command can only be used in #{channel_name}."
            )
        return True

    return commands.check(predicate)


@bot.check
async def block_disallowed_ranking_commands(ctx):
    in_ranking = is_configured_channel(
        ctx.channel,
        RANKING_CHANNEL_ID,
    )
    if in_ranking and ctx.command.name not in RANKING_ALLOWED_COMMANDS:
        raise ChannelRestrictionError(
            f"This command cannot be used in #{RANKING_CHANNEL_NAME}."
        )
    return True


@contextmanager
def get_connection():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def table_exists(connection, table_name):
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def initialize_database():
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                current_mmr INTEGER NOT NULL DEFAULT 100
                    CHECK (current_mmr >= 0),
                peak_mmr INTEGER NOT NULL DEFAULT 100
                    CHECK (peak_mmr >= 0),
                games_played INTEGER NOT NULL DEFAULT 0
                    CHECK (games_played >= 0),
                wins INTEGER NOT NULL DEFAULT 0
                    CHECK (wins >= 0),
                losses INTEGER NOT NULL DEFAULT 0
                    CHECK (losses >= 0),
                winrate REAL NOT NULL DEFAULT 0
                    CHECK (winrate >= 0 AND winrate <= 100),
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS match_history (
                match_id TEXT PRIMARY KEY,
                player_1_id INTEGER NOT NULL,
                player_2_id INTEGER NOT NULL,
                winner_id INTEGER NOT NULL,
                loser_id INTEGER NOT NULL,
                winner_mmr_gain INTEGER NOT NULL
                    CHECK (winner_mmr_gain >= 0),
                loser_mmr_loss INTEGER NOT NULL
                    CHECK (loser_mmr_loss >= 0),
                played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                match_type TEXT NOT NULL
                    CHECK (match_type IN ('ranked', 'event', 'admin')),
                idempotency_key TEXT UNIQUE,
                FOREIGN KEY (player_1_id) REFERENCES users(user_id),
                FOREIGN KEY (player_2_id) REFERENCES users(user_id),
                FOREIGN KEY (winner_id) REFERENCES users(user_id),
                FOREIGN KEY (loser_id) REFERENCES users(user_id),
                CHECK (winner_id <> loser_id)
            );

            CREATE TABLE IF NOT EXISTS match_participants (
                match_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL CHECK (team_number IN (1, 2)),
                result TEXT NOT NULL CHECK (result IN ('win', 'loss')),
                mmr_before INTEGER NOT NULL CHECK (mmr_before >= 0),
                mmr_change INTEGER NOT NULL,
                mmr_after INTEGER NOT NULL CHECK (mmr_after >= 0),
                PRIMARY KEY (match_id, user_id),
                FOREIGN KEY (match_id)
                    REFERENCES match_history(match_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS admin_actions (
                action_id TEXT PRIMARY KEY,
                actor_id INTEGER,
                target_id INTEGER,
                action_type TEXT NOT NULL,
                source_type TEXT NOT NULL
                    CHECK (source_type IN ('event', 'admin')),
                previous_mmr INTEGER,
                new_mmr INTEGER,
                amount_changed INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                idempotency_key TEXT UNIQUE,
                FOREIGN KEY (target_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS mmr_changes (
                change_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                match_id TEXT,
                admin_action_id TEXT,
                change_type TEXT NOT NULL
                    CHECK (change_type IN ('ranked', 'event', 'admin')),
                previous_mmr INTEGER NOT NULL CHECK (previous_mmr >= 0),
                amount_changed INTEGER NOT NULL,
                new_mmr INTEGER NOT NULL CHECK (new_mmr >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (match_id) REFERENCES match_history(match_id),
                FOREIGN KEY (admin_action_id)
                    REFERENCES admin_actions(action_id),
                CHECK (
                    (match_id IS NOT NULL AND admin_action_id IS NULL)
                    OR
                    (match_id IS NULL AND admin_action_id IS NOT NULL)
                )
            );

            CREATE INDEX IF NOT EXISTS idx_users_ranking
                ON users(current_mmr DESC, wins DESC);

            CREATE INDEX IF NOT EXISTS idx_match_history_played_at
                ON match_history(played_at DESC);

            CREATE INDEX IF NOT EXISTS idx_match_history_players
                ON match_history(player_1_id, player_2_id);

            CREATE INDEX IF NOT EXISTS idx_mmr_changes_user
                ON mmr_changes(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_admin_actions_target
                ON admin_actions(target_id, created_at DESC);
            """
        )

        migrate_legacy_data(connection)
        connection.execute(
            """
            INSERT INTO bot_settings (setting_key, setting_value)
            VALUES ('competitive_schema_version', '1')
            ON CONFLICT(setting_key)
            DO UPDATE SET setting_value = excluded.setting_value
            """
        )


def migrate_legacy_data(connection):
    """Copy old players/matches once without deleting or recalculating them."""
    if table_exists(connection, "players"):
        connection.execute(
            """
            INSERT OR IGNORE INTO users (
                user_id,
                username,
                current_mmr,
                peak_mmr,
                games_played,
                wins,
                losses,
                winrate
            )
            SELECT
                user_id,
                display_name,
                MAX(0, mmr),
                MAX(100, mmr),
                wins + losses,
                wins,
                losses,
                CASE
                    WHEN wins + losses = 0 THEN 0
                    ELSE (100.0 * wins) / (wins + losses)
                END
            FROM players
            """
        )

    if table_exists(connection, "matches"):
        connection.execute(
            """
            INSERT OR IGNORE INTO match_history (
                match_id,
                player_1_id,
                player_2_id,
                winner_id,
                loser_id,
                winner_mmr_gain,
                loser_mmr_loss,
                played_at,
                match_type,
                idempotency_key
            )
            SELECT
                'legacy-' || matches.id,
                matches.winner_id,
                matches.loser_id,
                matches.winner_id,
                matches.loser_id,
                MAX(0, matches.winner_change),
                ABS(matches.loser_change),
                matches.played_at,
                'ranked',
                'legacy:' || matches.id
            FROM matches
            INNER JOIN users AS winner
                ON winner.user_id = matches.winner_id
            INNER JOIN users AS loser
                ON loser.user_id = matches.loser_id
            """
        )


def get_user(user_id, connection=None):
    query = """
        SELECT
            user_id,
            username,
            current_mmr,
            peak_mmr,
            games_played,
            wins,
            losses,
            winrate,
            registered_at,
            updated_at
        FROM users
        WHERE user_id = ?
    """

    if connection is not None:
        return connection.execute(query, (user_id,)).fetchone()

    with get_connection() as own_connection:
        return own_connection.execute(query, (user_id,)).fetchone()


def get_mmr_role_config(mmr):
    for minimum_mmr, role_id, role_name in MMR_ROLE_THRESHOLDS:
        if mmr >= minimum_mmr:
            return role_id, role_name

    return ROLE_0_300_ID, "0-300"


async def assign_mmr_role(member):
    if member is None or member.bot or member.guild is None:
        return False

    user = get_user(member.id)
    if user is None:
        return False

    target_role_id, target_role_name = get_mmr_role_config(
        user["current_mmr"]
    )

    if not target_role_id:
        return False

    target_role = member.guild.get_role(target_role_id)

    if target_role is None:
        return False

    obsolete_roles = [
        role
        for role in member.roles
        if role.id in MMR_ROLE_IDS and role.id != target_role.id
    ]

    try:
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Automatic MMR role update")
        if obsolete_roles:
            await member.remove_roles(*obsolete_roles, reason="Automatic MMR role update")
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.warning("Could not update MMR role for user %s: %s", member.id, error)
        return False

    return True


async def get_guild_member(guild, user_id):
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def sync_all_mmr_roles(guild):
    with get_connection() as connection:
        user_ids = [
            row["user_id"]
            for row in connection.execute("SELECT user_id FROM users").fetchall()
        ]
    updated = 0
    for user_id in user_ids:
        member = await get_guild_member(guild, user_id)
        if member is not None and await assign_mmr_role(member):
            updated += 1
    return updated


def create_user(user):
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, current_mmr, peak_mmr)
            VALUES (?, ?, ?, ?)
            """,
            (user.id, user.display_name, INITIAL_MMR, INITIAL_MMR),
        )
        if cursor.rowcount == 0:
            connection.execute(
                "UPDATE users SET username = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (user.display_name, user.id),
            )
            return False
        return True


def set_user_mmr(connection, user_id, username, new_mmr):
    safe_mmr = max(0, int(new_mmr))
    connection.execute(
        """
        UPDATE users
        SET username = ?, current_mmr = ?, peak_mmr = MAX(peak_mmr, ?), updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (username, safe_mmr, safe_mmr, user_id),
    )
    return safe_mmr


def update_user_stats(connection, user, new_mmr, won):
    row = get_user(user.id, connection)
    if row is None:
        raise ValueError("The user is not registered.")
    games_played = row["games_played"] + 1
    wins = row["wins"] + int(won)
    losses = row["losses"] + int(not won)
    winrate = (100.0 * wins) / games_played
    set_user_mmr(connection, user.id, user.display_name, new_mmr)
    connection.execute(
        """
        UPDATE users
        SET games_played = ?, wins = ?, losses = ?, winrate = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (games_played, wins, losses, winrate, user.id),
    )


def expected_score(player_mmr, opponent_mmr):
    difference = max(-ELO_DIFFERENCE_LIMIT, min(ELO_DIFFERENCE_LIMIT, opponent_mmr - player_mmr))
    return 1 / (1 + 10 ** (difference / ELO_RATING_SCALE))


def get_dynamic_k(mmr):
    if mmr <= DYNAMIC_K_POINTS[0][0]:
        return float(DYNAMIC_K_POINTS[0][1])
    for index in range(1, len(DYNAMIC_K_POINTS)):
        lower_mmr, lower_k = DYNAMIC_K_POINTS[index - 1]
        upper_mmr, upper_k = DYNAMIC_K_POINTS[index]
        if mmr <= upper_mmr:
            progress = (mmr - lower_mmr) / (upper_mmr - lower_mmr)
            return lower_k + progress * (upper_k - lower_k)
    return float(DYNAMIC_K_POINTS[-1][1])


def calculate_mmr_change(winner_mmr, loser_mmr):
    winner_expected_score = expected_score(winner_mmr, loser_mmr)
    dynamic_k = get_dynamic_k(winner_mmr)
    raw_change = dynamic_k * (1 - winner_expected_score)
    return max(MIN_MATCH_MMR_CHANGE, min(MAX_MATCH_MMR_CHANGE, round(raw_change)))


def calculate_team_average_mmr(player_mmrs):
    if not player_mmrs:
        raise ValueError("A team must contain at least one player.")
    return sum(player_mmrs) / len(player_mmrs)


def calculate_team_mmr_change(winner_team_mmrs, loser_team_mmrs):
    winner_average = calculate_team_average_mmr(winner_team_mmrs)
    loser_average = calculate_team_average_mmr(loser_team_mmrs)
    return calculate_mmr_change(winner_average, loser_average)


def save_match_history(connection, *, match_id, player_1_id, player_2_id,
                       winner_id, loser_id, winner_mmr_gain, loser_mmr_loss,
                       match_type, idempotency_key):
    connection.execute(
        """
        INSERT INTO match_history (match_id, player_1_id, player_2_id, winner_id,
            loser_id, winner_mmr_gain, loser_mmr_loss, match_type, idempotency_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, player_1_id, player_2_id, winner_id, loser_id,
         winner_mmr_gain, loser_mmr_loss, match_type, idempotency_key),
    )


def save_match_participant(connection, *, match_id, user_id, team_number,
                           result, mmr_before, mmr_change, mmr_after):
    connection.execute(
        """
        INSERT INTO match_participants (match_id, user_id, team_number, result,
            mmr_before, mmr_change, mmr_after)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, user_id, team_number, result, mmr_before, mmr_change, mmr_after),
    )


def save_mmr_change(connection, *, user_id, change_type, previous_mmr,
                    amount_changed, new_mmr, match_id=None, admin_action_id=None):
    connection.execute(
        """
        INSERT INTO mmr_changes (change_id, user_id, match_id, admin_action_id,
            change_type, previous_mmr, amount_changed, new_mmr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), user_id, match_id, admin_action_id,
         change_type, previous_mmr, amount_changed, new_mmr),
    )


def update_mmr(winner, loser, match_type="ranked", idempotency_key=None):
    if match_type not in MATCH_TYPES:
        raise ValueError("Invalid match type.")
    if winner.id == loser.id:
        raise ValueError("Winner and loser must be different users.")

    match_id = str(uuid.uuid4())

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            if idempotency_key:
                duplicate = connection.execute(
                    "SELECT match_id FROM match_history WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateRecordError("This match has already been recorded.")

            winner_row = get_user(winner.id, connection)
            loser_row = get_user(loser.id, connection)
            if winner_row is None or loser_row is None:
                return None

            winner_old_mmr = winner_row["current_mmr"]
            loser_old_mmr = loser_row["current_mmr"]
            calculated_change = calculate_team_mmr_change([winner_old_mmr], [loser_old_mmr])

            match_change = min(loser_old_mmr, calculated_change)
            winner_new_mmr = winner_old_mmr + match_change
            loser_new_mmr = loser_old_mmr - match_change

            update_user_stats(connection, winner, winner_new_mmr, won=True)
            update_user_stats(connection, loser, loser_new_mmr, won=False)

            save_match_history(connection, match_id=match_id, player_1_id=winner.id,
                player_2_id=loser.id, winner_id=winner.id, loser_id=loser.id,
                winner_mmr_gain=match_change, loser_mmr_loss=match_change,
                match_type=match_type, idempotency_key=idempotency_key)
            save_match_participant(connection, match_id=match_id, user_id=winner.id,
                team_number=1, result="win", mmr_before=winner_old_mmr,
                mmr_change=match_change, mmr_after=winner_new_mmr)
            save_match_participant(connection, match_id=match_id, user_id=loser.id,
                team_number=2, result="loss", mmr_before=loser_old_mmr,
                mmr_change=-match_change, mmr_after=loser_new_mmr)
            save_mmr_change(connection, user_id=winner.id, match_id=match_id,
                change_type=match_type, previous_mmr=winner_old_mmr,
                amount_changed=match_change, new_mmr=winner_new_mmr)
            save_mmr_change(connection, user_id=loser.id, match_id=match_id,
                change_type=match_type, previous_mmr=loser_old_mmr,
                amount_changed=-match_change, new_mmr=loser_new_mmr)

            return {
                "match_id": match_id,
                "winner_mmr": winner_new_mmr,
                "winner_change": match_change,
                "loser_mmr": loser_new_mmr,
                "loser_change": -match_change,
                "match_type": match_type,
            }
    except sqlite3.IntegrityError as error:
        raise DuplicateRecordError("This match has already been recorded.") from error


def apply_manual_mmr_change(actor, target, operation, amount, *,
                             source_type="admin", idempotency_key=None, reason=None):
    if source_type not in MANUAL_SOURCE_TYPES:
        raise ValueError("Invalid manual change source.")
    if operation not in {"add", "remove", "set"}:
        raise ValueError("Invalid MMR operation.")
    if not isinstance(amount, int):
        raise ValueError("MMR amount must be a whole number.")
    if operation in {"add", "remove"} and amount <= 0:
        raise ValueError("MMR amount must be positive.")
    if operation == "set" and amount < 0:
        raise ValueError("MMR cannot be negative.")

    action_id = str(uuid.uuid4())

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            if idempotency_key:
                duplicate = connection.execute(
                    "SELECT action_id FROM admin_actions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateRecordError("This administrative action was already applied.")

            user = get_user(target.id, connection)
            if user is None:
                return None

            previous_mmr = user["current_mmr"]
            if operation == "add":
                new_mmr = previous_mmr + amount
            elif operation == "remove":
                new_mmr = max(0, previous_mmr - amount)
            else:
                new_mmr = amount

            new_mmr = set_user_mmr(connection, target.id, target.display_name, new_mmr)
            amount_changed = new_mmr - previous_mmr

            connection.execute(
                """
                INSERT INTO admin_actions (action_id, actor_id, target_id, action_type,
                    source_type, previous_mmr, new_mmr, amount_changed, reason, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (action_id, actor.id, target.id, operation, source_type,
                 previous_mmr, new_mmr, amount_changed, reason, idempotency_key),
            )
            save_mmr_change(connection, user_id=target.id, admin_action_id=action_id,
                change_type=source_type, previous_mmr=previous_mmr,
                amount_changed=amount_changed, new_mmr=new_mmr)

            return {
                "action_id": action_id,
                "previous_mmr": previous_mmr,
                "new_mmr": new_mmr,
                "amount_changed": amount_changed,
                "operation": operation,
                "source_type": source_type,
            }
    except sqlite3.IntegrityError as error:
        raise DuplicateRecordError("This administrative action was already applied.") from error


def reset_season(actor, idempotency_key=None):
    action_id = str(uuid.uuid4())

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        if idempotency_key:
            duplicate = connection.execute(
                "SELECT action_id FROM admin_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate is not None:
                raise DuplicateRecordError("This season reset was already applied.")

        users = connection.execute("SELECT user_id, username, current_mmr FROM users").fetchall()

        connection.execute(
            """
            INSERT INTO admin_actions (action_id, actor_id, action_type, source_type,
                amount_changed, reason, idempotency_key)
            VALUES (?, ?, 'resetseason', 'admin', 0, ?, ?)
            """,
            (action_id, actor.id, "Season reset; match history preserved.", idempotency_key),
        )

        for user in users:
            amount_changed = INITIAL_MMR - user["current_mmr"]
            set_user_mmr(connection, user["user_id"], user["username"], INITIAL_MMR)
            connection.execute(
                """
                UPDATE users
                SET games_played = 0, wins = 0, losses = 0, winrate = 0, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (user["user_id"],),
            )
            save_mmr_change(connection, user_id=user["user_id"], admin_action_id=action_id,
                change_type="admin", previous_mmr=user["current_mmr"],
                amount_changed=amount_changed, new_mmr=INITIAL_MMR)

        return len(users)


async def send_mmr_update_confirmation(ctx, member, result):
    await ctx.send(
        "**MMR updated**\n"
        f"User: {member.mention}\n"
        f"Previous MMR: **{result['previous_mmr']}**\n"
        f"New MMR: **{result['new_mmr']}**\n"
        f"Amount changed: **{result['amount_changed']:+d}**"
    )


async def send_manual_mmr_history(member, operation, result):
    history_channel = bot.get_channel(HISTORIAL_CHANNEL_ID)
    if history_channel is None:
        history_channel = await bot.fetch_channel(HISTORIAL_CHANNEL_ID)

    if operation == "add":
        message = f"📈 {member.mention} gained +{result['amount_changed']} MMR.\nCurrent MMR: {result['new_mmr']}."
    elif operation == "remove":
        removed = abs(result["amount_changed"])
        message = f"📉 {member.mention} lost -{removed} MMR.\nCurrent MMR: {result['new_mmr']}."
    else:
        message = f"⚙️ {member.mention}'s MMR was set to {result['new_mmr']} by an administrator."

    await history_channel.send(message)


def discord_idempotency_key(prefix, ctx):
    guild_id = ctx.guild.id if ctx.guild else 0
    return f"{prefix}:{guild_id}:{ctx.channel.id}:{ctx.message.id}"


@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    exportar_ranking()


@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


@bot.command()
@only_in_channel(RANKING_CHANNEL_ID, RANKING_CHANNEL_NAME)
async def register(ctx):
    if ctx.author.bot:
        await ctx.send("Bots cannot register.")
        return
    created = create_user(ctx.author)
    if created:
        await ctx.send(f"{ctx.author.mention}, you registered with **{INITIAL_MMR} MMR**.")
    else:
        await ctx.send(f"{ctx.author.mention}, you are already registered.")
    await assign_mmr_role(ctx.author)
    exportar_ranking()


@bot.command()
@only_in_channel(RANKING_CHANNEL_ID, RANKING_CHANNEL_NAME)
async def mmr(ctx, member: discord.Member = None):
    member = member or ctx.author
    user = get_user(member.id)
    if user is None:
        await ctx.send(f"{member.mention} is not registered. Use `!register` first.")
        return
    dynamic_k = round(get_dynamic_k(user["current_mmr"]))
    await ctx.send(
        f"**{member.display_name}**\n"
        f"MMR: **{user['current_mmr']}**\n"
        f"Peak MMR: **{user['peak_mmr']}**\n"
        f"Wins: **{user['wins']}** | Losses: **{user['losses']}** | Games: **{user['games_played']}**\n"
        f"Win rate: **{user['winrate']:.1f}%**\n"
        f"Dynamic K-factor: **{dynamic_k}**"
    )


@bot.command()
@only_in_channel(RANKING_CHANNEL_ID, RANKING_CHANNEL_NAME)
async def ranking(ctx):
    with get_connection() as connection:
        users = connection.execute(
            """
            SELECT username, current_mmr, wins, losses
            FROM users
            ORDER BY current_mmr DESC, wins DESC, username COLLATE NOCASE
            LIMIT 10
            """
        ).fetchall()

    if not users:
        await ctx.send("There are no registered players yet.")
        return

    lines = ["**MMR Ranking - Top 10**"]
    position = 0
    previous_mmr = None
    for index, user in enumerate(users, start=1):
        if user["current_mmr"] != previous_mmr:
            position = index
            previous_mmr = user["current_mmr"]
        lines.append(
            f"**#{position} {user['username']}** - {user['current_mmr']} MMR "
            f"({user['wins']}W/{user['losses']}L)"
        )
    await ctx.send("\n".join(lines))


@bot.command()
@only_in_channel(HISTORIAL_CHANNEL_ID, HISTORIAL_CHANNEL_NAME)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def victoria(ctx, winner: discord.Member, loser: discord.Member):
    if winner.id == loser.id:
        await ctx.send("The winner and loser must be different users.")
        return
    if winner.bot or loser.bot:
        await ctx.send("Matches involving bots cannot be recorded.")
        return

    missing = []
    if get_user(winner.id) is None:
        missing.append(winner.mention)
    if get_user(loser.id) is None:
        missing.append(loser.mention)
    if missing:
        await ctx.send(
            "These players are not registered: " + ", ".join(missing) + ". Each player must use `!register`."
        )
        return

    result = update_mmr(winner, loser, match_type="ranked",
                        idempotency_key=discord_idempotency_key("match", ctx))
    if result is None:
        await ctx.send("The match could not be recorded.")
        return

    await assign_mmr_role(winner)
    await assign_mmr_role(loser)
    exportar_ranking()

    await ctx.send(
        "**Result recorded**\n"
        f"Match ID: `{result['match_id']}`\n"
        f"Winner: {winner.mention} - **{result['winner_mmr']} MMR** ({result['winner_change']:+d})\n"
        f"Loser: {loser.mention} - **{result['loser_mmr']} MMR** ({result['loser_change']:+d})"
    )


@bot.command()
@only_in_channel(STAFF_COMMANDS_CHANNEL_ID, STAFF_COMMANDS_CHANNEL_NAME)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def resetseason(ctx):
    player_count = reset_season(ctx.author, idempotency_key=discord_idempotency_key("resetseason", ctx))
    await ctx.send(
        f"Season reset complete. **{player_count} players** were reset to "
        f"**{INITIAL_MMR} MMR** with 0 wins and 0 losses. Match history was preserved."
    )
    await sync_all_mmr_roles(ctx.guild)
    exportar_ranking()


async def run_manual_mmr_command(ctx, member, operation, amount):
    result = apply_manual_mmr_change(ctx.author, member, operation, amount,
                                     source_type="admin",
                                     idempotency_key=discord_idempotency_key(operation, ctx))
    if result is None:
        await ctx.send(f"{member.mention} is not registered. Use `!register` first.")
        return
    await assign_mmr_role(member)
    exportar_ranking()
    await send_mmr_update_confirmation(ctx, member, result)
    await send_manual_mmr_history(member, operation, result)


@bot.command()
@only_in_channel(STAFF_COMMANDS_CHANNEL_ID, STAFF_COMMANDS_CHANNEL_NAME)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def addmmr(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send("The amount must be a positive number.")
        return
    await run_manual_mmr_command(ctx, member, "add", amount)


@bot.command()
@only_in_channel(STAFF_COMMANDS_CHANNEL_ID, STAFF_COMMANDS_CHANNEL_NAME)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def removemmr(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send("The amount must be a positive number.")
        return
    await run_manual_mmr_command(ctx, member, "remove", amount)


@bot.command()
@only_in_channel(STAFF_COMMANDS_CHANNEL_ID, STAFF_COMMANDS_CHANNEL_NAME)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def setmmr(ctx, member: discord.Member, amount: int):
    if amount < 0:
        await ctx.send("MMR cannot be negative.")
        return
    await run_manual_mmr_command(ctx, member, "set", amount)


@bot.event
async def on_command_error(ctx, error):
    original_error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, ChannelRestrictionError):
        await ctx.send(str(error))
        return
    if isinstance(original_error, DuplicateRecordError):
        await ctx.send(str(original_error))
        return
    if isinstance(error, commands.MissingRequiredArgument):
        usages = {
            "victoria": "!victoria @winner @loser",
            "addmmr": "!addmmr @user amount",
            "removemmr": "!removemmr @user amount",
            "setmmr": "!setmmr @user amount",
        }
        usage = usages.get(ctx.command.name)
        if usage:
            await ctx.send(f"Correct usage: `{usage}`")
        else:
            await ctx.send("A required argument is missing.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("I could not find one of the mentioned users.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("The amount must be a whole number.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the **Manage Server** permission to use this command.")
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command can only be used inside a server.")
    elif isinstance(original_error, discord.Forbidden):
        await ctx.send("I do not have permission to send messages in the configured channel.")
    elif isinstance(original_error, discord.NotFound):
        await ctx.send("The configured channel could not be found.")
    else:
        raise error


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Missing token. Create a .env file with: DISCORD_TOKEN=your_token_here"
        )
    initialize_database()
    bot.run(TOKEN)
