"""
cogs/commands.py — 管理者用スラッシュコマンド

【v2.2 での大きな変更】
v2.1以前は多数のスラッシュコマンドが存在しましたが、v2.2にて
それらは「ハブ（固定UI）」のボタンに置き換えられました。
（例: /report → 進捗ハブのボタン）

【このファイルで学べること】
- スラッシュコマンド（app_commands）の作り方
- デフォルトパーミッション（管理者限定）の設定方法
- 運用保守のためのヘルスチェック（ping）の実装

【実務的な設計意図（UIベースへの移行）】
「スラッシュコマンドは覚えるのが大変」「スマホで打ちにくい」という
ユーザーの課題を解決するため、よく使う機能は全てボタンUI（Views）に移行しました。
現在このファイルに残っているのは、開発者や管理者がシステムの生存確認（ヘルスチェック）や
デバッグを行うための「裏コマンド」のみです。
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)


class CommandsCog(commands.Cog):
    """
    管理者用コマンドを提供するCog。
    
    【@app_commands.default_permissions とは？】
    このコマンドを実行できる権限を定義します。
    administrator=True を指定することで、Discordのサーバー設定で
    「管理者」権限を持つユーザーしかこのコマンドを見たり実行したりできなくなります。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答を確認します")
    @app_commands.default_permissions(administrator=True)
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! レイテンシ: **{latency}ms**", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
