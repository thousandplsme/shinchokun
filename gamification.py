"""
gamification.py — EXP/称号ゲーミフィケーションシステム

EXP（経験値）の付与・レベル計算・称号判定・Discordロール自動管理を行う。

【このファイルで学べること】
- math.floor / math.sqrt の数学関数
- Discordロール（Role）のプログラム的操作
- ソート済み辞書のキーから「該当する最大のキー」を逆順検索
- オブジェクト指向：責務の分離（TitleSystem / ExpManager）
"""

import math
import logging
from typing import Optional

import discord

import config
from database import Database

logger = logging.getLogger(__name__)


# ============================================
# 称号システム
# ============================================

class TitleSystem:
    """
    レベルと職種から称号を決定するクラス。
    
    【称号の決定ロジック】
    1. config.TITLE_TEMPLATES の「必要レベル」キーを大きい順に走査
    2. ユーザーのレベルが「必要レベル」以上なら、そのテンプレートを採用
    3. テンプレート内の {job} を職種名に置換して称号文字列を生成
    
    例: Lv.12 のプログラマー → TITLE_TEMPLATES[10] = "{job} C級"
        → "プログラマー C級"
    """

    @staticmethod
    def calculate_level(total_exp: int) -> int:
        """
        累計EXPからレベルを計算する。
        
        式: Level = floor(sqrt(total_exp / EXP_PER_LEVEL_UNIT))
        ただし最低レベルは 1。
        
        【math.floor() とは？】
        小数点以下を切り捨てて整数にする関数。
        例: floor(3.7) = 3, floor(5.0) = 5
        
        【math.sqrt() とは？】
        平方根（ルート）を計算する関数。
        例: sqrt(100) = 10.0, sqrt(250) = 15.81...
        
        【この計算式の意味】
        レベルが上がるほど、次のレベルに必要なEXPが多くなる。
        Lv.1→2: 30 EXP, Lv.10→11: 210 EXP, Lv.20→21: 410 EXP
        → 序盤はサクサク上がるが、後半はじっくり成長する設計。
        """
        level = math.floor(math.sqrt(total_exp / config.EXP_PER_LEVEL_UNIT))
        return max(1, level)

    @staticmethod
    def get_title(level: int, job_name: str) -> str:
        """
        レベルと職種名から称号文字列を生成する。
        
        Args:
            level: 現在のレベル
            job_name: 職種の表示名（例: "プログラマー"）
        
        Returns:
            称号文字列（例: "プログラマー C級"）
        
        【sorted() + reverse=True の理由】
        TITLE_TEMPLATES のキー [1, 5, 10, 20, 30, 50] を
        大きい方から [50, 30, 20, 10, 5, 1] と走査し、
        最初に「level >= キー」となるテンプレートを選ぶ。
        これにより「該当する最大ランク」が決まる。
        """
        # TITLE_TEMPLATES のキー（必要レベル）を降順ソート
        sorted_levels = sorted(config.TITLE_TEMPLATES.keys(), reverse=True)

        for required_level in sorted_levels:
            if level >= required_level:
                template = config.TITLE_TEMPLATES[required_level]
                # {job} を実際の職種名に置換
                return template.format(job=job_name)

        # フォールバック（ここに来ることは通常ない）
        return f"見習い {job_name}"

    @staticmethod
    def get_title_rank(level: int) -> int:
        """
        レベルから称号ランク（TITLE_TEMPLATES のキー値）を返す。
        ロールカラーの決定に使用。
        """
        sorted_levels = sorted(config.TITLE_TEMPLATES.keys(), reverse=True)
        for required_level in sorted_levels:
            if level >= required_level:
                return required_level
        return 1

    @staticmethod
    def get_exp_for_next_level(current_level: int) -> int:
        """
        次のレベルに必要な累計EXPを計算する。

        式: 必要EXP = (current_level + 1)^2 * EXP_PER_LEVEL_UNIT
        
        例: Lv.1→2 = 40 EXP, Lv.5→6 = 360 EXP
        """
        return (current_level + 1) ** 2 * config.EXP_PER_LEVEL_UNIT


# ============================================
# EXPマネージャー
# ============================================

class ExpManager:
    """
    EXPの付与・レベルアップ判定・称号更新・ロール管理を行うクラス。
    
    【責務の分離】
    - TitleSystem: 純粋な計算ロジック（DBやDiscordに依存しない）
    - ExpManager: DB操作・Discord操作を含む、実際のビジネスロジック
    """

    def __init__(self, db: Database, sheets_client=None):
        """
        Args:
            db: データベースインスタンス
            sheets_client: Google Sheetsクライアント（EXPログ記録用、Noneでもok）
        """
        self.db = db
        self.sheets = sheets_client
        self.title_system = TitleSystem()

    async def ensure_profile(self, discord_id: int, job_type: str = "", guild: discord.Guild = None) -> dict:
        """
        ユーザープロフィールが存在することを保証する。
        存在しなければ初期値で作成する。
        
        【v2.2 変更】guild 引数を追加。初回作成時にもロール付与を実行。
        
        【このパターンの目的】
        初回のEXP獲得時にプロフィールが存在しないとエラーになる。
        「なければ作る、あればそのまま返す」ことで、呼び出し側が
        プロフィール作成タイミングを気にしなくて済む。
        """
        profile = await self.db.get_user_profile(discord_id)
        if profile is None:
            job_name = ""
            if self.sheets:
                member = self.sheets.get_member(discord_id)
                if member:
                    job_type = member.get("job_type", job_type)
                    job_name = self.sheets.get_job_name(job_type)

            title = TitleSystem.get_title(1, job_name or job_type)
            await self.db.upsert_user_profile(
                discord_id=discord_id,
                total_exp=0,
                level=1,
                job_type=job_type,
                current_title=title,
                consecutive_weeks=0,
            )
            profile = await self.db.get_user_profile(discord_id)

            # 初回作成時にもロールを付与する（v2.2）
            if guild and title:
                await self._update_role(guild, discord_id, "", title, 1)

        return profile

    async def grant_exp(
        self,
        discord_id: int,
        amount: int,
        reason: str,
        guild: discord.Guild = None,
    ) -> dict:
        """
        EXPを付与し、レベルアップを判定する。
        
        Args:
            discord_id: 対象ユーザーのID
            amount: 付与EXP量
            reason: 付与理由（ログ用）
            guild: Discordサーバー（ロール更新時に使用、Noneならロール処理をスキップ）
        
        Returns:
            結果辞書:
            {
                "leveled_up": bool,       # レベルアップしたか
                "old_level": int,         # 旧レベル
                "new_level": int,         # 新レベル
                "total_exp": int,         # 累計EXP
                "old_title": str,         # 旧称号
                "new_title": str,         # 新称号
                "title_changed": bool,    # 称号が変わったか
            }
        """
        # 1. プロフィールを取得（なければ作成）
        profile = await self.ensure_profile(discord_id)

        old_level = profile["level"]
        old_title = profile["current_title"]
        old_exp = profile["total_exp"]

        # 2. EXP加算
        new_exp = old_exp + amount
        new_level = TitleSystem.calculate_level(new_exp)

        # 3. 称号を再計算
        job_type = profile["job_type"]
        job_name = job_type
        if self.sheets:
            job_name = self.sheets.get_job_name(job_type) or job_type

        new_title = TitleSystem.get_title(new_level, job_name)
        title_changed = old_title != new_title

        # 4. プロフィール更新（DB永続化）
        await self.db.upsert_user_profile(
            discord_id=discord_id,
            total_exp=new_exp,
            level=new_level,
            job_type=job_type,
            current_title=new_title,
            consecutive_weeks=profile["consecutive_weeks"],
        )

        # 5. EXP履歴を記録（DB）
        await self.db.add_exp_record(discord_id, amount, reason)

        # 6. Google Sheets にもEXPログを追記（設定されている場合）
        if self.sheets:
            try:
                await self.sheets.append_exp_log(discord_id, amount, reason)
            except Exception as e:
                logger.warning(f"⚠️ Sheets EXPログ追記失敗（続行）: {e}")

        # 7. Discordロール更新（称号が変わった場合のみ）
        if title_changed and guild:
            await self._update_role(guild, discord_id, old_title, new_title, new_level)

        leveled_up = new_level > old_level

        result = {
            "leveled_up": leveled_up,
            "old_level": old_level,
            "new_level": new_level,
            "total_exp": new_exp,
            "old_title": old_title,
            "new_title": new_title,
            "title_changed": title_changed,
        }

        if leveled_up:
            logger.info(
                f"🎉 レベルアップ！ ID={discord_id}: Lv.{old_level} → Lv.{new_level} ({new_title})"
            )

        return result

    async def _update_role(
        self,
        guild: discord.Guild,
        discord_id: int,
        old_title: str,
        new_title: str,
        new_level: int,
    ):
        """
        Discordのロール（役職）を更新する。
        
        【ロール管理の流れ】
        1. 旧称号のロールがあれば剥奪
        2. 新称号のロールがなければ作成（色付き）
        3. 新ロールをメンバーに付与
        
        【discord.utils.get() とは？】
        条件に一致する最初の要素を返すユーティリティ関数。
        ここでは「ロール名が一致するロール」を検索するために使用。
        """
        try:
            member = guild.get_member(discord_id)
            if member is None:
                member = await guild.fetch_member(discord_id)

            # 旧ロールを検索して剥奪
            if old_title:
                old_role = discord.utils.get(guild.roles, name=old_title)
                if old_role and old_role in member.roles:
                    await member.remove_roles(old_role, reason="称号変更")
                    logger.info(f"🔄 旧ロール剥奪: {old_title}")

            # 新ロールを検索（なければ作成）
            new_role = discord.utils.get(guild.roles, name=new_title)
            if new_role is None:
                # ロールを新規作成
                rank = TitleSystem.get_title_rank(new_level)
                color = discord.Color(config.ROLE_COLORS.get(rank, 0x95A5A6))
                new_role = await guild.create_role(
                    name=new_title,
                    color=color,
                    reason=f"しんちょくん: 称号ロール自動作成",
                )
                logger.info(f"✨ ロール作成: {new_title} (色: {color})")

            # 新ロールを付与
            await member.add_roles(new_role, reason="称号更新")
            logger.info(f"🏅 ロール付与: {new_title} → {member.display_name}")

        except discord.Forbidden:
            logger.error(f"❌ ロール操作権限がありません。Botのロール順位を確認してください。")
        except Exception as e:
            logger.error(f"❌ ロール更新エラー: {e}")

    @staticmethod
    def build_levelup_message(result: dict) -> str:
        """
        レベルアップ時の演出メッセージを生成する。
        
        Args:
            result: grant_exp() が返す結果辞書
        
        Returns:
            お祝いメッセージ文字列
        """
        if not result["leveled_up"]:
            return ""

        msg = f"🎉 **レベルアップ！** Lv.{result['old_level']} → **Lv.{result['new_level']}**\n"

        if result["title_changed"]:
            msg += f"🏅 称号変更: ~~{result['old_title']}~~ → **{result['new_title']}**\n"
            msg += "新しいロールが付与されました！ ✨\n"

        # 大きなレベルアップには追加演出
        level_diff = result["new_level"] - result["old_level"]
        if level_diff >= 5:
            msg += "⚡ **驚異的な成長！** みんなに差をつけろ！ 🔥\n"
        elif level_diff >= 2:
            msg += "💪 一気にレベルアップ！ その調子！\n"

        return msg

    @staticmethod
    def build_exp_message(amount: int, total_exp: int, level: int) -> str:
        """
        EXP獲得時の通知メッセージを生成する。
        
        次のレベルまでの残りEXPも表示。
        
        【プログレスバーの生成】
        1. 現在レベルの必要EXP と 次のレベルの必要EXP を計算
        2. その間で「現在何%」かを求める
        3. ▓ と ░ の個数で視覚的に表現
        """
        next_level_exp = TitleSystem.get_exp_for_next_level(level)
        current_level_exp = level ** 2 * config.EXP_PER_LEVEL_UNIT
        progress = total_exp - current_level_exp
        needed = next_level_exp - current_level_exp

        # プログレスバー（10マス）
        if needed > 0:
            filled = min(10, int(progress / needed * 10))
        else:
            filled = 10
        bar = "▓" * filled + "░" * (10 - filled)

        remaining = max(0, next_level_exp - total_exp)

        return (
            f"⭐ **+{amount} EXP** 獲得！\n"
            f"📊 Lv.{level} [{bar}] 次のレベルまで: {remaining} EXP\n"
            f"💰 累計: {total_exp} EXP"
        )
