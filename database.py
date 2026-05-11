"""
database.py — 非同期SQLiteデータベース操作（v2 拡張版）

aiosqlite を使用して discord.py の非同期処理と統合。
v2 で追加されたテーブル:
  - user_profiles: ユーザーのEXP・レベル・称号（永続）
  - exp_records: EXP獲得履歴
  - quest_completions: クエスト完了記録
  - sos_requests: SOS送信履歴
  - meeting_requests: 日程調整リクエスト

【このファイルで学べること】
- SQLiteの CREATE TABLE IF NOT EXISTS（冪等なテーブル作成）
- UPSERT（INSERT ... ON CONFLICT DO UPDATE）パターン
- 非同期コンテキストマネージャー（async with）
- 日付文字列の扱いと比較
"""

import os
import aiosqlite
from datetime import datetime, timedelta, timezone

# 日本標準時
JST = timezone(timedelta(hours=9))


def now_jst():
    """現在のJST日時を返す"""
    return datetime.now(JST)


def today_jst():
    """現在のJST日付文字列を返す（YYYY-MM-DD）"""
    return now_jst().strftime("%Y-%m-%d")


# ============================================
# データベーススキーマ（v2 拡張版）
# ============================================
# 
# 【CREATE TABLE IF NOT EXISTS とは？】
# テーブルが存在しない場合のみ作成する。
# すでに存在する場合はスキップされるため、
# Bot再起動のたびに呼んでも安全（=冪等, idempotent）。
#
# 【UNIQUE制約】
# 指定した列の組み合わせが重複しないことを保証する。
# 重複データの挿入を防ぎ、ON CONFLICTで更新に切り替えられる。
#
SCHEMA = """
-- ===== v1 既存テーブル（過去データ参照用に残置）=====

CREATE TABLE IF NOT EXISTS progress_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    schedule_date TEXT NOT NULL,
    done TEXT NOT NULL DEFAULT '',
    blockers TEXT DEFAULT '',
    next_tasks TEXT NOT NULL DEFAULT '',
    motivation INTEGER NOT NULL DEFAULT 3,
    is_no_progress INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, schedule_date)
);

CREATE TABLE IF NOT EXISTS schedule_rounds (
    schedule_date TEXT PRIMARY KEY,
    message_id INTEGER,
    channel_id INTEGER,
    created_at TEXT NOT NULL
);

-- ===== v2 新規テーブル =====

-- ユーザープロフィール: EXP・レベル・称号の永続管理
-- Bot再起動してもデータが消えない
CREATE TABLE IF NOT EXISTS user_profiles (
    discord_id INTEGER PRIMARY KEY,
    total_exp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 1,
    job_type TEXT NOT NULL DEFAULT '',
    current_title TEXT NOT NULL DEFAULT '',
    consecutive_weeks INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- EXP獲得履歴: いつ・誰が・なぜ・いくらEXPを得たか
CREATE TABLE IF NOT EXISTS exp_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- クエスト完了記録: どの週の・どのクエストを完了したか
-- UNIQUE制約で同一クエストの二重完了を防止
CREATE TABLE IF NOT EXISTS quest_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    quest_title TEXT NOT NULL,
    exp_earned INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL,
    UNIQUE(discord_id, week_start, quest_title)
);

-- SOS送信履歴: 誰が・誰に・何を相談したか
CREATE TABLE IF NOT EXISTS sos_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL,
    mentor_user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    urgency TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL
);

-- 日程調整リクエスト: 誰が・誰に・いつ・何の打ち合わせを提案したか
CREATE TABLE IF NOT EXISTS meeting_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL,
    to_user_id INTEGER NOT NULL,
    proposed_datetime TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    response_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- ===== v2.1 新規テーブル =====

-- ピアボーナス: メンバー間の感謝記録
CREATE TABLE IF NOT EXISTS peer_bonuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL,
    to_user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ===== v2.2 新規テーブル =====

-- 日程調整投票（「調整さん」方式）
CREATE TABLE IF NOT EXISTS schedule_polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER NOT NULL,
    subject TEXT NOT NULL,
    candidates TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    confirmed_slot TEXT DEFAULT '',
    event_id INTEGER DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 各メンバーの投票記録
CREATE TABLE IF NOT EXISTS schedule_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    selected_slots TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(poll_id, discord_id)
);

-- タスク追加申請
CREATE TABLE IF NOT EXISTS task_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    quest_title TEXT NOT NULL,
    quest_detail TEXT DEFAULT '',
    requested_exp INTEGER DEFAULT 30,
    week_start TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer_id INTEGER DEFAULT 0,
    reject_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class Database:
    """
    非同期SQLiteデータベースクラス（v2 拡張版）

    【設計方針】
    - 全メソッドで async with aiosqlite.connect() を使用。
      接続はメソッドごとに開閉する（コネクションプーリングは不要な規模）。
    - aiosqlite は SQLite のファイルロックを自動管理するため、
      並行書き込みも安全（ただし書き込み性能はRDBMSに劣る）。
    """

    def __init__(self, db_path="data/progress.db"):
        self.db_path = db_path

    async def init(self):
        """データベースとテーブルを初期化"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ============================================
    # スケジュールラウンド操作（v1 互換）
    # ============================================

    async def create_schedule_round(self, schedule_date: str, message_id: int, channel_id: int):
        """新しいスケジュールラウンドを作成"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO schedule_rounds 
                   (schedule_date, message_id, channel_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (schedule_date, message_id, channel_id, now_jst().isoformat()),
            )
            await db.commit()

    async def get_latest_schedule_date(self) -> str | None:
        """最新のスケジュール日付を取得"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT schedule_date FROM schedule_rounds ORDER BY schedule_date DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                return row["schedule_date"] if row else None

    # ============================================
    # 進捗レポート操作（v1 互換 — 過去データ参照用）
    # ============================================

    async def save_report(
        self,
        user_id: int,
        schedule_date: str,
        done: str,
        blockers: str,
        next_tasks: str,
        motivation: int,
        is_no_progress: bool = False,
    ) -> bool:
        """進捗レポートを保存（同じuser_id+schedule_dateなら上書き更新）"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM progress_reports WHERE user_id = ? AND schedule_date = ?",
                (user_id, schedule_date),
            ) as cursor:
                existing = await cursor.fetchone()

            await db.execute(
                """INSERT INTO progress_reports 
                   (user_id, schedule_date, done, blockers, next_tasks, motivation, is_no_progress, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, schedule_date) DO UPDATE SET
                       done=excluded.done,
                       blockers=excluded.blockers,
                       next_tasks=excluded.next_tasks,
                       motivation=excluded.motivation,
                       is_no_progress=excluded.is_no_progress,
                       created_at=excluded.created_at""",
                (
                    user_id, schedule_date, done, blockers, next_tasks,
                    motivation, int(is_no_progress), now_jst().isoformat(),
                ),
            )
            await db.commit()
            return existing is None

    async def get_reports_for_schedule(self, schedule_date: str) -> list[dict]:
        """指定スケジュール日のレポートを全件取得"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM progress_reports WHERE schedule_date = ? ORDER BY created_at",
                (schedule_date,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_reported_user_ids(self, schedule_date: str) -> list[int]:
        """指定スケジュール日に報告済みのユーザーIDリストを取得"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id FROM progress_reports WHERE schedule_date = ?",
                (schedule_date,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    async def get_user_latest_report(self, user_id: int) -> dict | None:
        """ユーザーの最新レポートを取得"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM progress_reports WHERE user_id = ? ORDER BY schedule_date DESC LIMIT 1",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_user_reports(self, user_id: int, weeks: int = 4) -> list[dict]:
        """ユーザーの過去N週間のレポートを取得"""
        cutoff = (now_jst() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM progress_reports WHERE user_id = ? AND schedule_date >= ? ORDER BY schedule_date DESC",
                (user_id, cutoff),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_all_reports_since(self, weeks: int = 4) -> list[dict]:
        """全メンバーの過去N週間のレポートを取得"""
        cutoff = (now_jst() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM progress_reports WHERE schedule_date >= ? ORDER BY schedule_date DESC, user_id",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_weekly_report_count(self, user_id: int, weeks: int = 4) -> dict:
        """ユーザーの過去N週間の報告率を計算"""
        cutoff = (now_jst() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM schedule_rounds WHERE schedule_date >= ?",
                (cutoff,),
            ) as cursor:
                total = (await cursor.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM progress_reports WHERE user_id = ? AND schedule_date >= ?",
                (user_id, cutoff),
            ) as cursor:
                reported = (await cursor.fetchone())[0]

            return {
                "total": total,
                "reported": reported,
                "rate": (reported / total * 100) if total > 0 else 0,
            }

    async def get_motivation_history(self, weeks: int = 8) -> list[dict]:
        """全メンバーのモチベーション履歴を取得（グラフ用）"""
        cutoff = (now_jst() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT user_id, schedule_date, motivation 
                   FROM progress_reports 
                   WHERE schedule_date >= ? AND is_no_progress = 0
                   ORDER BY schedule_date""",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ============================================
    # ユーザープロフィール操作（v2 新規）
    # ============================================
    #
    # 【UPSERT パターン】
    # INSERT ... ON CONFLICT DO UPDATE は「存在すれば更新、なければ挿入」を
    # 1つのSQLで実現するパターン。
    # "UPSERT" = "UPDATE or INSERT" の造語。

    async def get_user_profile(self, discord_id: int) -> dict | None:
        """
        ユーザープロフィールを取得する。
        
        Returns:
            プロフィール辞書 or None（未登録の場合）
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM user_profiles WHERE discord_id = ?",
                (discord_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def upsert_user_profile(
        self,
        discord_id: int,
        total_exp: int,
        level: int,
        job_type: str,
        current_title: str,
        consecutive_weeks: int = 0,
    ):
        """
        ユーザープロフィールを挿入 or 更新する（UPSERT）。
        
        【ON CONFLICT(discord_id) DO UPDATE】
        discord_id が既に存在する場合は UPDATE を実行。
        excluded.* は INSERT で指定した値を参照する。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO user_profiles 
                   (discord_id, total_exp, level, job_type, current_title, consecutive_weeks, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET
                       total_exp=excluded.total_exp,
                       level=excluded.level,
                       job_type=excluded.job_type,
                       current_title=excluded.current_title,
                       consecutive_weeks=excluded.consecutive_weeks,
                       updated_at=excluded.updated_at""",
                (
                    discord_id, total_exp, level, job_type, current_title,
                    consecutive_weeks, now_jst().isoformat(),
                ),
            )
            await db.commit()

    async def get_all_user_profiles(self) -> list[dict]:
        """全ユーザーのプロフィールを取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM user_profiles ORDER BY total_exp DESC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ============================================
    # EXP記録操作（v2 新規）
    # ============================================

    async def add_exp_record(self, discord_id: int, amount: int, reason: str):
        """
        EXP獲得記録を追加する。
        
        これは「履歴」テーブルへの追記。
        累計EXPの更新は user_profiles テーブルで別途行う。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO exp_records (discord_id, amount, reason, created_at)
                   VALUES (?, ?, ?, ?)""",
                (discord_id, amount, reason, now_jst().isoformat()),
            )
            await db.commit()

    async def get_weekly_exp(self, discord_id: int, week_start: str) -> int:
        """
        指定ユーザーの指定週のEXP合計を取得する。
        
        【SQL集約関数 COALESCE + SUM】
        SUM() は対象行がない場合 NULL を返すため、
        COALESCE(SUM(...), 0) で NULL を 0 に変換する。
        """
        week_end_date = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)
        week_end = week_end_date.strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM exp_records 
                   WHERE discord_id = ? AND created_at >= ? AND created_at < ?""",
                (discord_id, week_start, week_end),
            ) as cursor:
                result = await cursor.fetchone()
                return result[0]

    async def get_all_weekly_exp(self, week_start: str) -> list[dict]:
        """
        指定週の全メンバーのEXP合計を取得する（ランキング用）。
        
        【GROUP BY と ORDER BY DESC】
        GROUP BY で discord_id ごとに集計し、
        ORDER BY DESC で多い順に並べる（ランキング）。
        """
        week_end_date = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)
        week_end = week_end_date.strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT discord_id, SUM(amount) as weekly_exp 
                   FROM exp_records 
                   WHERE created_at >= ? AND created_at < ?
                   GROUP BY discord_id 
                   ORDER BY weekly_exp DESC""",
                (week_start, week_end),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_all_exp_logs(self) -> list[dict]:
        """全てのEXP獲得記録を取得する（データ同期用）"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM exp_records ORDER BY created_at ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ============================================
    # クエスト完了記録（v2 新規）
    # ============================================

    async def record_quest_completion(
        self, discord_id: int, week_start: str, quest_title: str, exp_earned: int
    ) -> bool:
        """
        クエスト完了を記録する。
        
        Returns:
            True = 新規記録（初回完了）
            False = 既に完了済み（重複）
        
        【UNIQUE制約による重複防止】
        (discord_id, week_start, quest_title) の組み合わせに UNIQUE制約があるため、
        同じクエストを2回完了しようとするとエラーになる。
        try/except で IntegrityError を捕捉し、False を返す。
        """
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """INSERT INTO quest_completions 
                       (discord_id, week_start, quest_title, exp_earned, completed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (discord_id, week_start, quest_title, exp_earned, now_jst().isoformat()),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                # UNIQUE制約違反 = 既に完了済み
                return False

    async def get_quest_completions(self, discord_id: int, week_start: str) -> list[dict]:
        """指定ユーザーの指定週のクエスト完了一覧を取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM quest_completions 
                   WHERE discord_id = ? AND week_start = ?
                   ORDER BY completed_at""",
                (discord_id, week_start),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_all_quest_completions_for_week(self, week_start: str) -> list[dict]:
        """指定週の全メンバーのクエスト完了を取得する（サマリー用）"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM quest_completions 
                   WHERE week_start = ? 
                   ORDER BY discord_id, completed_at""",
                (week_start,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ============================================
    # SOS記録（v2 新規）
    # ============================================

    async def save_sos_request(
        self, from_user_id: int, mentor_user_id: int, content: str, urgency: str = "medium"
    ):
        """SOS送信を記録する"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO sos_requests 
                   (from_user_id, mentor_user_id, content, urgency, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (from_user_id, mentor_user_id, content, urgency, now_jst().isoformat()),
            )
            await db.commit()

    # ============================================
    # 日程調整リクエスト（v2 新規）
    # ============================================

    async def save_meeting_request(
        self, from_user_id: int, to_user_id: int,
        proposed_datetime: str, subject: str,
    ) -> int:
        """
        日程調整リクエストを保存し、レコードIDを返す。
        
        【lastrowid とは？】
        INSERT 直後に、自動採番された PRIMARY KEY の値を取得するプロパティ。
        このIDを使って後から status を更新する。
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO meeting_requests 
                   (from_user_id, to_user_id, proposed_datetime, subject, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (from_user_id, to_user_id, proposed_datetime, subject, now_jst().isoformat()),
            )
            await db.commit()
            return cursor.lastrowid

    async def update_meeting_status(self, meeting_id: int, status: str, reason: str = ""):
        """日程調整リクエストのステータスを更新する"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE meeting_requests 
                   SET status = ?, response_reason = ? 
                   WHERE id = ?""",
                (status, reason, meeting_id),
            )
            await db.commit()

    async def get_meeting_request(self, meeting_id: int) -> dict | None:
        """日程調整リクエストを取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM meeting_requests WHERE id = ?",
                (meeting_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    # ============================================
    # ピアボーナス操作（v2.1 新規）
    # ============================================

    async def save_peer_bonus(
        self, from_user_id: int, to_user_id: int, message: str
    ):
        """ピアボーナスを記録する"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO peer_bonuses (from_user_id, to_user_id, message, created_at)
                   VALUES (?, ?, ?, ?)""",
                (from_user_id, to_user_id, message, now_jst().isoformat()),
            )
            await db.commit()

    async def get_today_bonus_count(self, from_user_id: int) -> int:
        """
        今日のピアボーナス送信回数を取得する。
        1日あたりのスパム防止用。
        """
        today = today_jst()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT COUNT(*) FROM peer_bonuses
                   WHERE from_user_id = ? AND created_at LIKE ?""",
                (from_user_id, f"{today}%"),
            ) as cursor:
                result = await cursor.fetchone()
                return result[0]

    # ============================================
    # 日程調整投票（v2.2 新規）
    # ============================================

    async def create_schedule_poll(
        self, created_by: int, subject: str, candidates_json: str, expires_at: str
    ) -> int:
        """日程調整投票を作成し、IDを返す"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO schedule_polls
                   (created_by, subject, candidates, status, expires_at, created_at)
                   VALUES (?, ?, ?, 'open', ?, ?)""",
                (created_by, subject, candidates_json, expires_at, now_jst().isoformat()),
            )
            await db.commit()
            return cursor.lastrowid

    async def save_schedule_vote(
        self, poll_id: int, discord_id: int, selected_json: str
    ) -> bool:
        """投票を保存（UPSERT）。新規ならTrue、更新ならFalseを返す"""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                # 既存の投票があるかチェック
                cursor = await db.execute(
                    "SELECT 1 FROM schedule_votes WHERE poll_id = ? AND discord_id = ?",
                    (poll_id, discord_id)
                )
                existing = await cursor.fetchone()
                is_new = existing is None

                await db.execute(
                    """INSERT INTO schedule_votes (poll_id, discord_id, selected_slots, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(poll_id, discord_id) DO UPDATE SET
                           selected_slots=excluded.selected_slots,
                           created_at=excluded.created_at""",
                    (poll_id, discord_id, selected_json, now_jst().isoformat()),
                )
                await db.commit()
                return is_new
            except Exception as e:
                logger.error(f"❌ 投票保存エラー: {e}")
                return True # エラー時はとりあえず新規扱い（または例外スロー）

    async def get_schedule_poll(self, poll_id: int) -> dict | None:
        """投票情報を取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM schedule_polls WHERE id = ?", (poll_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_schedule_votes(self, poll_id: int) -> list[dict]:
        """指定投票の全回答を取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM schedule_votes WHERE poll_id = ? ORDER BY created_at",
                (poll_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_schedule_vote_count(self, poll_id: int) -> int:
        """指定投票の回答数を取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM schedule_votes WHERE poll_id = ?",
                (poll_id,),
            ) as cursor:
                result = await cursor.fetchone()
                return result[0]

    async def confirm_schedule_poll(self, poll_id: int, confirmed_slot: str, event_id: int = 0):
        """投票を確定状態にする"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE schedule_polls SET status = 'confirmed',
                   confirmed_slot = ?, event_id = ? WHERE id = ?""",
                (confirmed_slot, event_id, poll_id),
            )
            await db.commit()

    async def get_open_polls(self) -> list[dict]:
        """期限切れの未確定投票を取得する（自動集計用）"""
        now = now_jst().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM schedule_polls
                   WHERE status = 'open' AND expires_at <= ?""",
                (now,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ============================================
    # タスク申請操作（v2.2 新規）
    # ============================================

    async def create_task_request(
        self, discord_id: int, quest_title: str, quest_detail: str,
        requested_exp: int, week_start: str,
    ) -> int:
        """タスク申請を作成し、IDを返す"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO task_requests
                   (discord_id, quest_title, quest_detail, requested_exp, week_start, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (discord_id, quest_title, quest_detail, requested_exp, week_start, now_jst().isoformat()),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_task_request(self, request_id: int) -> dict | None:
        """タスク申請を取得する"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM task_requests WHERE id = ?", (request_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update_task_request(
        self, request_id: int, status: str, reviewer_id: int = 0, reject_reason: str = ""
    ):
        """タスク申請のステータスを更新する"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE task_requests SET status = ?, reviewer_id = ?, reject_reason = ?
                   WHERE id = ?""",
                (status, reviewer_id, reject_reason, request_id),
            )
            await db.commit()
