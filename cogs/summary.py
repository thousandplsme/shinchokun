"""
cogs/summary.py — 週次サマリー機能（v2 強化版）

日曜夜に1週間の進捗サマリーを自動投稿する。
v2ではクエスト完了状況・EXPランキング・称号情報を追加。
メンバーの追加入力は不要（全データを自動集計）。

【v2 での変更点】
- 進捗報告ベース → クエスト完了ベースに変更
- EXPランキング（今週の獲得EXP順）を追加
- 各メンバーの称号・レベルを表示
- クエスト消化率をメンバー別に表示
"""

import logging
from datetime import timedelta

import discord
from discord.ext import commands
from discord import app_commands

import config
from database import now_jst
from sheets import SheetsClient

logger = logging.getLogger(__name__)


class SummaryCog(commands.Cog):
    """週次サマリーの自動生成・投稿を管理するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def post_weekly_summary(self):
        """
        週次サマリーを投稿する（スケジューラから日曜 21:00 に呼ばれる）。
        
        【集計データのソース】
        - クエスト消化率: Google Sheets の週次タスク + DB の quest_completions
        - EXPランキング: DB の exp_records（今週分を集計）
        - 称号・レベル: DB の user_profiles
        
        → メンバーから追加入力を求めない設計（負担ゼロ）。
        """
        channel = self.bot.get_channel(config.PROGRESS_CHANNEL_ID)
        if not channel:
            logger.error(f"チャンネルが見つかりません: {config.PROGRESS_CHANNEL_ID}")
            return

        # 今週の月曜日を計算
        today = now_jst()
        monday = today - timedelta(days=today.weekday())
        week_start = monday.strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        # Embed作成（複数のEmbedを送信）
        embeds = []

        # ===== Embed 1: ヘッダー + EXPランキング =====
        embed_header = discord.Embed(
            title="📊 週間サマリー",
            description=f"**{week_start}** 〜 **{week_end}**",
            color=config.COLOR_GOLD,
        )

        # EXPランキング取得
        weekly_exp_data = await self.bot.db.get_all_weekly_exp(week_start)

        if weekly_exp_data:
            ranking_lines = []
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

            for i, data in enumerate(weekly_exp_data):
                user = self.bot.get_user(data["discord_id"])
                name = user.display_name if user else f"ID:{data['discord_id']}"
                medal = medals[i] if i < len(medals) else f"{i+1}."

                ranking_lines.append(
                    f"{medal} **{name}** — +{data['weekly_exp']} EXP"
                )

            embed_header.add_field(
                name="🏆 今週のEXPランキング",
                value="\n".join(ranking_lines),
                inline=False,
            )
        else:
            embed_header.add_field(
                name="🏆 今週のEXPランキング",
                value="今週のEXPデータはありません。",
                inline=False,
            )

        embeds.append(embed_header)

        # ===== Embed 2: クエスト消化率 =====
        embed_quests = discord.Embed(
            title="⚔️ クエスト消化率",
            color=config.COLOR_QUEST,
        )

        try:
            sheets: SheetsClient = self.bot.sheets
            all_quests = await sheets.get_weekly_quests(week_start)

            if all_quests:
                # メンバーごとにグルーピング
                member_quest_map: dict[int, dict] = {}
                for q in all_quests:
                    uid = q["discord_id"]
                    if uid not in member_quest_map:
                        member_quest_map[uid] = {"total": 0, "completed": 0}
                    member_quest_map[uid]["total"] += 1
                    if q.get("status", "") == "完了":
                        member_quest_map[uid]["completed"] += 1

                for uid, stats in member_quest_map.items():
                    user = self.bot.get_user(uid)
                    name = user.display_name if user else f"ID:{uid}"
                    rate = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0

                    # プログレスバー
                    filled = int(rate / 10)
                    bar = "▓" * filled + "░" * (10 - filled)

                    embed_quests.add_field(
                        name=f"🔹 {name}",
                        value=(
                            f"[{bar}] **{rate:.0f}%**\n"
                            f"達成: {stats['completed']}/{stats['total']} クエスト"
                        ),
                        inline=False,
                    )
            else:
                embed_quests.add_field(
                    name="📝 クエスト",
                    value="今週のクエストはありません。",
                    inline=False,
                )
        except Exception as e:
            logger.error(f"⚠️ クエスト消化率取得エラー: {e}")
            embed_quests.add_field(
                name="⚠️ エラー",
                value="クエストデータの取得に失敗しました。",
                inline=False,
            )

        embeds.append(embed_quests)

        # ===== Embed 3: メンバー称号・レベル一覧 =====
        embed_titles = discord.Embed(
            title="🏅 メンバーステータス",
            color=config.COLOR_PRIMARY,
        )

        profiles = await self.bot.db.get_all_user_profiles()
        if profiles:
            for profile in profiles:
                user = self.bot.get_user(profile["discord_id"])
                name = user.display_name if user else f"ID:{profile['discord_id']}"

                embed_titles.add_field(
                    name=f"🔹 {name}",
                    value=(
                        f"🏅 {profile['current_title']}\n"
                        f"📊 Lv.{profile['level']} | 💰 {profile['total_exp']} EXP"
                    ),
                    inline=True,
                )
        else:
            embed_titles.add_field(
                name="📝 ステータス",
                value="プロフィールデータがありません。",
                inline=False,
            )

        embed_titles.set_footer(text="しんちょくん 🤖 | 週間サマリー")
        embed_titles.timestamp = discord.utils.utcnow()

        embeds.append(embed_titles)

        # 全Embedを送信
        await channel.send(embeds=embeds)
        logger.info(f"📊 週間サマリーを投稿しました ({week_start} 〜 {week_end})")

    # テスト用コマンド
    @app_commands.command(
        name="test_summary",
        description="【テスト用】週次サマリーを手動で投稿します",
    )
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.post_weekly_summary()
        await interaction.followup.send("✅ 週次サマリーを投稿しました！", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))
