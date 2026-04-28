"""
cogs/commands.py — 管理者用スラッシュコマンド（v2.1）

全コマンドを管理者権限化。ユーザー向け機能はハブチャンネルに移行済み。
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands

import config

logger = logging.getLogger(__name__)


class CommandsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答を確認します")
    @app_commands.default_permissions(administrator=True)
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! レイテンシ: **{latency}ms**", ephemeral=True)

    @app_commands.command(name="test_quest", description="【テスト用】クエスト配信を手動で実行します")
    @app_commands.default_permissions(administrator=True)
    async def test_quest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        quest_cog = self.bot.get_cog("QuestCog")
        if quest_cog:
            await quest_cog.deliver_weekly_quests()
            await interaction.followup.send("✅ クエスト配信を実行しました！", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ QuestCogが読み込まれていません。", ephemeral=True)

    @app_commands.command(name="test_summary", description="【テスト用】週次サマリーを手動で投稿します")
    @app_commands.default_permissions(administrator=True)
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        summary_cog = self.bot.get_cog("SummaryCog")
        if summary_cog:
            await summary_cog.post_weekly_summary()
            await interaction.followup.send("✅ 週次サマリーを投稿しました！", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ SummaryCogが読み込まれていません。", ephemeral=True)

    @app_commands.command(name="help_bot", description="管理者用コマンド一覧を表示します")
    @app_commands.default_permissions(administrator=True)
    async def help_bot(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🤖 しんちょくん — 管理者コマンド",
            description="メンバー向け機能は **コマンドチャンネル** のボタンから利用できます。",
            color=config.COLOR_PRIMARY,
        )
        cmds = [
            ("`/ping`", "Botの応答確認"),
            ("`/setup_hub`", "コマンドチャンネルに常設フォームを設置"),
            ("`/test_quest`", "クエスト配信を手動実行"),
            ("`/test_summary`", "週次サマリーを手動投稿"),
            ("`/help_bot`", "このヘルプを表示"),
        ]
        for cmd, desc in cmds:
            embed.add_field(name=cmd, value=desc, inline=False)
        embed.set_footer(text="しんちょくん 🤖 | 管理者ヘルプ")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
