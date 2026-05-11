"""
bot.py — しんちょくん エントリーポイント（v2.2）

v2.2 変更点:
- cogs/scheduler.py 追加（日程調整「調整さん」方式）
- 期限切れ投票の自動集計ジョブ追加
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from database import Database
from sheets import SheetsClient

load_dotenv()

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot")
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)


class ShinchokuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.db = Database()
        self.sheets: SheetsClient | None = None
        self.scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
        self._ready_count = 0  # 同時起動検知用

    async def setup_hook(self):
        logger.info("=== setup_hook 開始 ===")
        self.tree.on_error = self.on_app_command_error

        # 1. DB初期化
        await self.db.init()
        logger.info("✅ データベース初期化完了")

        # 2. Google Sheets接続
        if config.SPREADSHEET_ID:
            try:
                self.sheets = SheetsClient(
                    config.GOOGLE_CREDENTIALS_PATH, config.SPREADSHEET_ID,
                )
                await self.sheets.init()
                logger.info("✅ Google Sheets 接続完了")
            except Exception as e:
                logger.error(f"⚠️ Google Sheets 接続失敗: {e}")
                self.sheets = None
        else:
            logger.warning("⚠️ SPREADSHEET_ID 未設定。Google Sheets 機能は無効です。")

        # 3. Cog読み込み（v2.1）
        cog_list = [
            "cogs.quest",
            "cogs.hub",       # SOS統合 + 全機能フォーム
            "cogs.admin_hub", # 管理者専用コマンドハブ＆隠し称号
            "cogs.meeting",   # 個別打診応答View
            "cogs.scheduler", # 日程調整（調整さん方式）
            "cogs.summary",
            "cogs.commands",  # 管理者コマンド
        ]
        for cog in cog_list:
            try:
                await self.load_extension(cog)
                logger.info(f"✅ Cog読み込み: {cog}")
            except Exception as e:
                logger.error(f"❌ Cog読み込み失敗: {cog} - {e}")

        # 4. 永続View登録（v2.1: Hub + Quest）
        from cogs.quest import QuestView
        from cogs.hub import PersistentHubView
        from cogs.admin_hub import AdminHubView
        self.add_view(QuestView(self))
        self.add_view(PersistentHubView(self))
        self.add_view(AdminHubView(self))
        logger.info("✅ 永続View登録完了")

        # 5. スラッシュコマンド同期
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(f"✅ コマンド同期（ギルド）: {len(synced)}個")
        else:
            synced = await self.tree.sync()
            logger.info(f"✅ コマンド同期（グローバル）: {len(synced)}個")

        # 6. スケジューラ
        self._setup_schedules()
        self.scheduler.start()
        logger.info("✅ スケジューラ起動完了")
        logger.info("=== setup_hook 完了 ===")

    def _setup_schedules(self):
        quest_cog = self.get_cog("QuestCog")
        if quest_cog:
            self.scheduler.add_job(
                quest_cog.deliver_weekly_quests,
                CronTrigger(day_of_week=config.QUEST_DAY, hour=config.QUEST_HOUR,
                           minute=config.QUEST_MINUTE, timezone=config.TIMEZONE),
                id="weekly_quest", replace_existing=True,
            )
            logger.info(f"  📜 クエスト配信ジョブ登録: {config.QUEST_DAY} {config.QUEST_HOUR}:{config.QUEST_MINUTE:02d}")

        summary_cog = self.get_cog("SummaryCog")
        if summary_cog:
            self.scheduler.add_job(
                summary_cog.post_weekly_summary,
                CronTrigger(day_of_week=config.SUMMARY_DAY, hour=config.SUMMARY_HOUR,
                           minute=0, timezone=config.TIMEZONE),
                id="weekly_summary", replace_existing=True,
            )
            logger.info(f"  📊 週次サマリージョブ登録: {config.SUMMARY_DAY} {config.SUMMARY_HOUR}:00")

        # 日程調整の期限切れチェック（毎時）
        scheduler_cog = self.get_cog("SchedulerCog")
        if scheduler_cog:
            self.scheduler.add_job(
                scheduler_cog.check_expired_polls,
                CronTrigger(minute=0, timezone=config.TIMEZONE),
                id="poll_expire_check", replace_existing=True,
            )
            logger.info("  📊 投票期限チェックジョブ登録: 毎時")

    # === Azure安定化: イベント監視 ===

    async def on_ready(self):
        self._ready_count += 1
        logger.info(f"🤖 {self.user} としてログインしました！（on_ready #{self._ready_count}）")
        logger.info(f"📡 サーバー数: {len(self.guilds)}")
        logger.info(f"👥 登録メンバー数: {len(config.get_active_member_ids())}")
        logger.info(f"📊 Google Sheets: {'接続済み' if self.sheets else '未接続'}")

        if self._ready_count > 1:
            logger.warning(
                "⚠️ on_ready が複数回呼ばれています。"
                "WebSocket再接続が発生した可能性があります。"
                "同一トークンで別のBotが同時起動していないか確認してください。"
            )

        activity = discord.Activity(type=discord.ActivityType.watching, name="みんなのクエスト 🗡️")
        await self.change_presence(activity=activity)
        await self._check_missed_schedules()

    async def on_disconnect(self):
        logger.warning("🔌 Discord接続が切断されました。自動再接続を待機中...")

    async def on_resumed(self):
        logger.info("🔄 Discord接続が復旧しました（WebSocket再接続成功）")

    async def _check_missed_schedules(self):
        JST = timezone(timedelta(hours=9))
        now = datetime.now(JST)
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today_day = day_names[now.weekday()]

        if today_day == config.QUEST_DAY and now.hour >= config.QUEST_HOUR:
            logger.info("⚡ クエスト配信漏れチェック → /test_quest で手動配信可能")

        if today_day == config.SUMMARY_DAY and now.hour >= config.SUMMARY_HOUR:
            summary_cog = self.get_cog("SummaryCog")
            if summary_cog:
                logger.info("⚡ 週次サマリー漏れ検知！ 自動投稿します")
                await summary_cog.post_weekly_summary()

    async def notify_leader_error(self, context: str, error: Exception, tb: str = None):
        """
        エラー発生時にリーダーのDMに詳細を通知する。

        Args:
            context: エラー発生箇所の説明（例: "クエスト配信"）
            error: 発生した例外
            tb: トレースバック情報
        """
        try:
            if self.sheets:
                leader = self.sheets.get_leader()
                if leader:
                    leader_user = await self.fetch_user(leader["discord_id"])
                    if leader_user:
                        embed = discord.Embed(
                            title="⚠️ しんちょくん 予期せぬエラー通知",
                            description=f"**{context}** にてエラーが発生しました。",
                            color=config.COLOR_DANGER,
                        )
                        error_text = str(error)[:1000]
                        embed.add_field(name="📋 エラー概要", value=f"```{error_text}```", inline=False)
                        
                        if tb:
                            tb_text = tb[-1000:] # 末尾のスタックトレースのみ抽出
                            embed.add_field(name="🔍 トレースバック (末尾)", value=f"```{tb_text}```", inline=False)
                            
                        embed.add_field(
                            name="💡 対処方法",
                            value="bot.log を確認してください。\n自動復旧できない場合はBot再起動をお試しください。",
                            inline=False,
                        )
                        embed.set_footer(text="しんちょくん 🤖 | 自動エラー検知")
                        await leader_user.send(embed=embed)
                        logger.info(f"📨 リーダーにエラー通知を送信: {context}")
        except Exception as notify_err:
            logger.warning(f"⚠️ リーダーへのエラー通知に失敗: {notify_err}")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        cmd_name = interaction.command.name if interaction.command else "UI操作等"
        logger.error(f"❌ インタラクションエラー ({cmd_name}): {error}")
        
        import traceback
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        user_info = f"{interaction.user.display_name} (ID: {interaction.user.id})"
        
        await self.notify_leader_error(f"操作: {cmd_name} | ユーザー: {user_info}", error, tb)
        
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("❌ 予期せぬエラーが発生しました。リーダーに自動通知されました。", ephemeral=True)
            except:
                pass
        else:
            try:
                await interaction.followup.send("❌ 予期せぬエラーが発生しました。リーダーに自動通知されました。", ephemeral=True)
            except:
                pass

    async def on_command_error(self, ctx, error):
        logger.error(f"❌ コマンドエラー: {error}")
        import traceback
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        await self.notify_leader_error(f"テキストコマンド: {ctx.command}", error, tb)


def main():
    token = config.DISCORD_TOKEN
    if not token or token == "ここにトークンを貼り付け":
        logger.error("❌ DISCORD_TOKEN が設定されていません！")
        sys.exit(1)

    if config.GUILD_ID == 0:
        logger.warning("⚠️ GUILD_ID 未設定。コマンド同期がグローバルになります。")
    if config.PROGRESS_CHANNEL_ID == 0:
        logger.warning("⚠️ PROGRESS_CHANNEL_ID 未設定。")
    if config.SOS_CHANNEL_ID == 0:
        logger.warning("⚠️ SOS_CHANNEL_ID 未設定。コマンドチャンネルが動作しません。")

    bot = ShinchokuBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
