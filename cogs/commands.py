"""
cogs/commands.py — 管理者用スラッシュコマンド（v2.2）

全コマンドを管理者権限化。ユーザー向け機能はハブチャンネルに移行済み。
v2.2: /sync_exp, /sync_roles を追加。
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands

import config
from gamification import ExpManager, TitleSystem

logger = logging.getLogger(__name__)


class CommandsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答を確認します")
    @app_commands.default_permissions(administrator=True)
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! レイテンシ: **{latency}ms**", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
