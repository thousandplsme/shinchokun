"""
sheets.py — Google Sheets API 連携クラス

Google スプレッドシートの読み書きを一元管理するモジュール。
サービスアカウント認証を使用し、メンバー情報・週次タスク・EXPログを操作する。

【このファイルで学べること】
- Google Sheets API（gspread ライブラリ）の使い方
- サービスアカウント認証の仕組み
- キャッシュパターン（頻繁なAPI呼び出しを避ける設計）
- asyncio.to_thread による同期→非同期変換
- データクラス的な辞書の活用
"""

import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from rate_limiter import with_retry

logger = logging.getLogger(__name__)

# 日本標準時
JST = timezone(timedelta(hours=9))


# ============================================
# Google Sheets API のスコープ定義
# ============================================
# スコープ = 「このアプリがどのGoogle APIにアクセスできるか」の許可範囲。
# spreadsheets: スプレッドシートの読み書き
# drive: ファイル一覧の取得（gspread が内部的に使用）
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _normalize_records(records: list[dict]) -> list[dict]:
    """
    get_all_records() の結果のキー名を正規化する。
    
    Google Sheets でプルダウンやデータ検証を使うと、
    ヘッダー行に余分なスペースが入ることがある。
    前後のスペースを除去して安全にアクセスできるようにする。
    """
    normalized = []
    for record in records:
        normalized.append({
            str(k).strip(): v for k, v in record.items()
        })
    return normalized


class SheetsClient:
    """
    Google Sheets API の操作を一元管理するクラス。
    
    【設計方針】
    - gspread は同期ライブラリなので、asyncio.to_thread() で非同期化する。
    - メンバー情報・職種マスタは起動時にキャッシュし、API呼び出しを最小化する。
    - 全てのAPI呼び出しに @with_retry を適用し、一時的なエラーを自動リトライする。
    
    【実務的な設計意図（キャッシュパターン）】
    Google Sheets API には「1ユーザーあたり1分間に60リクエスト」などの
    厳しいクオータ制限（利用枠）があります。
    メンバーがコマンドを叩くたびにシートを読みに行くと、すぐに制限に引っかかります。
    そのため、変動の少ないデータ（メンバー情報、職種マスタなど）は
    起動時や特定コマンド実行時のみ取得し、メモリ上の「キャッシュ」から返す設計にしています。

    【asyncio.to_thread() とは？】
    同期関数（通常の def で定義された関数）を別スレッドで実行し、
    メインの非同期ループをブロックしないようにする仕組み。
    gspread は async に対応していないため、これが必要。
    """

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        """
        Args:
            credentials_path: サービスアカウントのJSONファイルパス
            spreadsheet_id: 操作するスプレッドシートのID
                            （URLの /d/ と /edit の間の文字列）
        """
        self._credentials_path = credentials_path
        self._spreadsheet_id = spreadsheet_id
        self._client: Optional[gspread.Client] = None
        self._spreadsheet: Optional[gspread.Spreadsheet] = None

        # キャッシュ: メンバー情報と職種マスタを保持
        self._members_cache: list[dict] = []
        self._jobs_cache: dict[str, str] = {}  # {job_key: job_name}
        self._messages_cache: dict[str, dict] = {}  # {tone: {key: message}}

    # ============================================
    # 初期化・認証
    # ============================================

    async def init(self):
        """
        Google Sheets API に接続し、キャッシュを構築する。
        Bot起動時に1回だけ呼ばれる。

        【認証の流れ】
        1. JSONファイル内の「秘密鍵」を読み込む
        2. その鍵でGoogleに「自分はこのサービスアカウントです」と証明
        3. アクセストークンを取得してAPI呼び出しに使用
        """
        def _connect():
            # Credentials.from_service_account_file():
            #   JSONファイルからサービスアカウントの認証情報を読み込む。
            #   scopes= で「どのAPIにアクセスするか」を指定。
            creds = Credentials.from_service_account_file(
                self._credentials_path, scopes=SCOPES
            )
            # gspread.authorize(): 認証情報を使ってgspreadクライアントを作成
            client = gspread.authorize(creds)
            # open_by_key(): スプレッドシートIDを指定して開く
            spreadsheet = client.open_by_key(self._spreadsheet_id)
            return client, spreadsheet

        self._client, self._spreadsheet = await asyncio.to_thread(_connect)
        logger.info("✅ Google Sheets API 接続完了")

        # キャッシュを構築
        await self.refresh_cache()

    async def refresh_cache(self):
        """
        メンバー情報・職種マスタをスプレッドシートから再取得してキャッシュ更新。
        通常は起動時に1回呼ぶが、/reload コマンドなどで手動更新も可能。
        """
        self._members_cache = await self._fetch_members()
        self._jobs_cache = await self._fetch_job_types()
        self._messages_cache = await self._fetch_message_templates()
        logger.info(
            f"✅ キャッシュ更新: メンバー{len(self._members_cache)}人, "
            f"職種{len(self._jobs_cache)}種, "
            f"メッセージテンプレート{len(self._messages_cache)}種"
        )

    # ============================================
    # メンバー情報
    # ============================================

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def _fetch_members(self) -> list[dict]:
        """
        「メンバー」シートから全メンバー情報を取得する。

        【gspread の get_all_records()】
        シートの1行目をヘッダー（キー）として、
        各行を辞書のリストとして返す。
        例: [{"discord_id": "123", "name": "yamasaki", ...}, ...]
        """
        def _read():
            worksheet = self._spreadsheet.worksheet("メンバー")
            return worksheet.get_all_records()

        records = await asyncio.to_thread(_read)
        records = _normalize_records(records)

        # discord_id を int に変換（スプレッドシートでは文字列になる場合がある）
        for r in records:
            try:
                r["discord_id"] = int(r.get("discord_id", 0))
            except (ValueError, TypeError):
                logger.warning(f"⚠️ discord_id の変換失敗: {r}")
                r["discord_id"] = 0
        return records

    def get_members(self) -> list[dict]:
        """キャッシュからメンバー一覧を返す（API呼び出しなし）"""
        return self._members_cache

    def get_member(self, discord_id: int) -> Optional[dict]:
        """キャッシュから特定メンバーの情報を返す"""
        for m in self._members_cache:
            if m["discord_id"] == discord_id:
                return m
        return None

    def get_leader(self) -> Optional[dict]:
        """リーダーの情報を返す"""
        for m in self._members_cache:
            if str(m.get("is_leader", "")).upper() == "TRUE":
                return m
        return None

    # ============================================
    # 職種マスタ
    # ============================================

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def _fetch_job_types(self) -> dict[str, str]:
        """
        「職種マスタ」シートから職種一覧を取得する。
        
        Returns:
            {job_key: job_name} の辞書
            例: {"programmer": "プログラマー", "modeler": "モデラー"}
        """
        def _read():
            worksheet = self._spreadsheet.worksheet("職種マスタ")
            return worksheet.get_all_records()

        records = await asyncio.to_thread(_read)
        return {r["job_key"]: r["job_name"] for r in records}

    def get_job_name(self, job_key: str) -> str:
        """職種キーから表示名を返す（キャッシュ使用）"""
        return self._jobs_cache.get(job_key, job_key)

    def get_all_job_types(self) -> dict[str, str]:
        """全職種マスタを返す（キャッシュ使用）"""
        return self._jobs_cache

    # ============================================
    # 週次タスク（クエスト）
    # ============================================

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def get_weekly_quests(self, week_start: str) -> list[dict]:
        """
        指定週のクエスト（タスク）一覧を取得する。
        
        Args:
            week_start: 週の開始日（月曜日の日付, "YYYY-MM-DD"形式）
        
        Returns:
            クエストのリスト。各クエストは以下のキーを持つ辞書:
            - discord_id (int): 担当メンバーID
            - quest_title (str): クエスト名
            - quest_detail (str): 詳細/サブタスク
            - exp_reward (int): 完了時獲得EXP
            - status (str): "未着手" / "進行中" / "完了"
        """
        def _read():
            worksheet = self._spreadsheet.worksheet("週次タスク")
            return worksheet.get_all_records()

        records = await asyncio.to_thread(_read)
        records = _normalize_records(records)

        if records:
            logger.debug(f"📊 週次タスクシートのキー: {list(records[0].keys())}")

        # 指定週のタスクのみフィルタリング
        quests = []
        for r in records:
            if str(r.get("week_start", "")).strip() == week_start:
                try:
                    r["discord_id"] = int(r.get("discord_id", 0))
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ discord_id の変換失敗 (スキップ): {r}")
                    continue
                try:
                    r["exp_reward"] = int(r.get("exp_reward", 10))
                except (ValueError, TypeError):
                    r["exp_reward"] = 10
                    
                # sent列（TRUE/FALSE）の読み取り
                sent_val = str(r.get("sent", "FALSE")).strip().upper()
                r["sent"] = (sent_val == "TRUE")
                
                quests.append(r)
        return quests

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def update_quest_status(self, week_start: str, discord_id: int, quest_title: str, new_status: str):
        """クエストのステータスを更新する"""
        return await self._update_quest_field(week_start, discord_id, quest_title, "status", new_status)

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def update_quest_sent_status(self, week_start: str, discord_id: int, quest_title: str, is_sent: bool = True):
        """クエストのsent列を更新する"""
        val = "TRUE" if is_sent else "FALSE"
        return await self._update_quest_field(week_start, discord_id, quest_title, "sent", val)

    def _sync_get_sheet_data(self, sheet_name: str) -> tuple[gspread.Worksheet, list[dict], list[str]]:
        """
        同期ヘルパー: 指定シートのワークシートオブジェクト、全レコード、ヘッダー行を取得する。
        （to_thread 内の同期コンテキストから呼ばれることを想定）
        """
        worksheet = self._spreadsheet.worksheet(sheet_name)
        records = worksheet.get_all_records()
        headers = worksheet.row_values(1)
        return worksheet, records, headers

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def update_quest_sent_status_bulk(self, week_start: str, discord_id: int, quest_titles: list[str], is_sent: bool = True):
        """複数のクエストをまとめて送信済み状態に更新する（高速化用）"""
        def _bulk_update():
            worksheet, records, headers = self._sync_get_sheet_data("週次タスク")
            
            if "sent" not in headers:
                logger.warning("⚠️ シートに 'sent' 列が存在しません")
                return False

            sent_col = headers.index("sent") + 1
            val = "TRUE" if is_sent else "FALSE"
            
            # 該当する行をすべて特定
            found_count = 0
            for i, r in enumerate(records):
                row_num = i + 2
                if (
                    str(r.get("week_start", "")).strip() == week_start
                    and int(r.get("discord_id", 0)) == discord_id
                    and r.get("quest_title", "") in quest_titles
                ):
                    worksheet.update_cell(row_num, sent_col, val)
                    found_count += 1
            return found_count > 0

        result = await asyncio.to_thread(_bulk_update)
        if result:
            logger.info(f"✅ クエスト一括更新({len(quest_titles)}件): {discord_id}")
        return result

    async def _update_quest_field(self, week_start: str, discord_id: int, quest_title: str, field_name: str, new_value: str):
        """内部用：特定のクエストの特定フィールドを更新する"""
        def _update():
            worksheet, records, headers = self._sync_get_sheet_data("週次タスク")

            if field_name not in headers:
                logger.warning(f"⚠️ シートに '{field_name}' 列が存在しません")
                return False

            col_idx = headers.index(field_name) + 1

            for i, r in enumerate(records):
                row_num = i + 2
                if (
                    str(r.get("week_start", "")).strip() == week_start
                    and int(r.get("discord_id", 0)) == discord_id
                    and r.get("quest_title", "") == quest_title
                ):
                    worksheet.update_cell(row_num, col_idx, new_value)
                    return True
            return False

        result = await asyncio.to_thread(_update)
        if result:
            logger.info(f"✅ クエストフィールド更新: {quest_title} ({field_name}) → {new_value}")
        return result

    # ============================================
    # EXPログ
    # ============================================

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def append_exp_log(self, discord_id: int, amount: int, reason: str):
        """
        EXPログシートに1行追記する。
        
        【gspread の append_row()】
        シートの最終行の次に新しい行を追加する。
        値はリストで渡す（列の順番に対応）。
        
        Args:
            discord_id: ユーザーID
            amount: 獲得EXP量
            reason: 獲得理由（例: "クエスト完了: モデリング"）
        """
        def _append():
            worksheet = self._spreadsheet.worksheet("EXPログ")
            now = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S")
            worksheet.append_row(
                [now, str(discord_id), amount, reason],
                value_input_option="USER_ENTERED",
            )

        await asyncio.to_thread(_append)
        logger.info(f"📝 EXPログ追記: ID={discord_id}, +{amount} ({reason})")

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def get_all_exp_logs(self) -> list[dict]:
        """
        EXPログシートの全行を取得する（/sync_exp 用）。

        Returns:
            [{discord_id: int, amount: int, reason: str, timestamp: str}, ...]
        """
        def _read():
            try:
                worksheet = self._spreadsheet.worksheet("EXPログ")
                return worksheet.get_all_records()
            except gspread.exceptions.WorksheetNotFound:
                return []

        records = await asyncio.to_thread(_read)
        records = _normalize_records(records)
        result = []
        for r in records:
            try:
                result.append({
                    "discord_id": int(r.get("discord_id", 0)),
                    "amount": int(r.get("amount", 0)),
                    "reason": str(r.get("reason", "")),
                    "timestamp": str(r.get("timestamp", "")),
                })
            except (ValueError, TypeError):
                continue
        return result

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def append_quest_row(
        self, week_start: str, discord_id: int, quest_title: str,
        quest_detail: str, exp_reward: int, status: str = "未着手",
    ):
        """
        「週次タスク」シートに1行追加する。
        """
        def _append():
            worksheet = self._spreadsheet.worksheet("週次タスク")
            # sent列（7列目）は空欄（FALSE相当）になるように追加
            worksheet.append_row(
                [week_start, str(discord_id), quest_title, quest_detail, exp_reward, status, "FALSE"],
                value_input_option="USER_ENTERED",
            )

        await asyncio.to_thread(_append)
        logger.info(f"📝 週次タスク追記: {quest_title} ({week_start})")

    @with_retry(max_retries=3, base_delay=2.0, exceptions=(gspread.exceptions.APIError,))
    async def duplicate_quest_to_next_week(self, week_start: str, discord_id: int, quest_title: str, new_week_start: str) -> bool:
        """
        指定したクエストの行をコピーし、week_startを更新して新しい行として追加する。
        """
        def _duplicate():
            worksheet = self._spreadsheet.worksheet("週次タスク")
            records = worksheet.get_all_records()
            
            target_row = None
            for r in records:
                if (
                    str(r.get("week_start", "")) == week_start
                    and int(r.get("discord_id", 0)) == discord_id
                    and r.get("quest_title", "") == quest_title
                ):
                    target_row = r
                    break
            
            if not target_row:
                return False
            
            # detail と exp_reward をコピー
            detail = str(target_row.get("quest_detail", ""))
            exp = int(target_row.get("exp_reward", 10))
            
            # 新しい行を追加
            worksheet.append_row(
                [new_week_start, str(discord_id), quest_title, detail, exp, "未着手", "FALSE"],
                value_input_option="USER_ENTERED",
            )
            return True

        result = await asyncio.to_thread(_duplicate)
        if result:
            logger.info(f"📝 クエスト延長複製: {quest_title} ({week_start} -> {new_week_start})")
        return result

    # ============================================
    # ユーティリティ
    # ============================================

    @staticmethod
    def get_current_week_start() -> str:
        """今週の月曜日の日付を返す"""
        today = datetime.now(JST).date()
        monday = today - timedelta(days=today.weekday())
        return monday.strftime("%Y-%m-%d")

    @staticmethod
    def get_last_week_start() -> str:
        """先週の月曜日の日付を返す"""
        today = datetime.now(JST).date()
        last_monday = today - timedelta(days=today.weekday() + 7)
        return last_monday.strftime("%Y-%m-%d")

    @staticmethod
    def get_next_week_start(current_week_start: str) -> str:
        """指定された月曜日の『次の月曜日』の日付を返す"""
        current_date = datetime.strptime(current_week_start, "%Y-%m-%d").date()
        next_monday = current_date + timedelta(days=7)
        return next_monday.strftime("%Y-%m-%d")

    # ============================================
    # メッセージテンプレート（v2.1 新規）
    # ============================================

    async def _fetch_message_templates(self) -> dict[str, dict]:
        """
        「メッセージテンプレート」シートからトーン別メッセージを取得する。
        シートが存在しない場合は空辞書を返す（config.py のデフォルト値が使われる）。

        Returns:
            {tone: {key: message}} の辞書
            例: {"casual": {"quest_greeting": "おつかれ！...", ...}}
        """
        def _read():
            try:
                worksheet = self._spreadsheet.worksheet("メッセージテンプレート")
                return worksheet.get_all_records()
            except gspread.exceptions.WorksheetNotFound:
                logger.info("ℹ️ 「メッセージテンプレート」シート未作成。デフォルトメッセージを使用します。")
                return []

        records = await asyncio.to_thread(_read)
        if not records:
            return {}

        templates = {}
        for r in records:
            tone = r.get("tone", "")
            if tone:
                templates[tone] = {
                    k: v for k, v in r.items() if k != "tone" and v
                }
        return templates

    def get_message(self, discord_id: int, key: str) -> str:
        """
        メンバーのトーンに応じたメッセージを返す。
        
        検索順序:
        1. スプレッドシートのメッセージテンプレート（メンバーの dm_tone に一致）
        2. config.py の DEFAULT_MESSAGES（フォールバック）
        
        Args:
            discord_id: 対象メンバーのDiscord ID
            key: メッセージキー（"quest_greeting", "sos_thanks" 等）
        
        Returns:
            メッセージ文字列
        """
        import config

        # メンバーのトーンを取得
        member = self.get_member(discord_id)
        tone = member.get("dm_tone", "") if member else ""
        if not tone:
            tone = config.DEFAULT_TONE

        # 1. シートのテンプレートから検索
        if tone in self._messages_cache:
            msg = self._messages_cache[tone].get(key)
            if msg:
                return msg

        # 2. config.py のデフォルトから検索
        default_tone = config.DEFAULT_MESSAGES.get(tone, {})
        msg = default_tone.get(key)
        if msg:
            return msg

        # 3. formalトーンのデフォルト（最終フォールバック）
        return config.DEFAULT_MESSAGES.get("formal", {}).get(key, "")

    def get_member_nickname(self, discord_id: int) -> str:
        """
        メンバーのニックネームを返す。
        未設定の場合は空文字列を返す（呼び出し側で display_name を使う）。
        """
        member = self.get_member(discord_id)
        if member:
            return member.get("nickname", "") or ""
        return ""
