"""
cogs/meeting.py — 日程調整の応答View（v2.1 改修版）

ミーティング打診のスラッシュコマンドは hub.py に移行。
このファイルには応答View（参加/別日提案/辞退）のみ残す。
"""

import logging
import discord
from discord.ext import commands

import config
from rate_limiter import dm_limiter

logger = logging.getLogger(__name__)


class RescheduleModal(discord.ui.Modal, title="📅 別日を提案する"):
    proposed_date = discord.ui.TextInput(
        label="希望日時", placeholder="例: 4/28（水）14:00",
        style=discord.TextStyle.short, required=True, max_length=100,
    )
    reason = discord.ui.TextInput(
        label="コメント（任意）", placeholder="例: 夕方は予定があるから夜で",
        style=discord.TextStyle.paragraph, required=False, max_length=500,
    )

    def __init__(self, bot: commands.Bot, meeting_id: int, from_user_id: int):
        super().__init__()
        self.bot = bot
        self.meeting_id = meeting_id
        self.from_user_id = from_user_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.bot.db.update_meeting_status(self.meeting_id, "rescheduled", self.proposed_date.value)
        try:
            from_user = await self.bot.fetch_user(self.from_user_id)
            if from_user:
                embed = discord.Embed(title="📅 別日が提案されました", color=config.COLOR_WARNING,
                    description=f"**{interaction.user.display_name}** さんから別日の提案がありました。")
                embed.add_field(name="📆 提案日時", value=self.proposed_date.value, inline=False)
                if self.reason.value:
                    embed.add_field(name="💬 コメント", value=self.reason.value, inline=False)
                embed.set_footer(text="しんちょくん 🤖 | 日程調整")
                async with dm_limiter:
                    await from_user.send(embed=embed)
        except Exception as e:
            logger.error(f"❌ 別日提案通知エラー: {e}")
        await interaction.response.send_message("✅ 別日を提案しました！", ephemeral=True)


class DeclineReasonModal(discord.ui.Modal, title="❌ 辞退理由の入力"):
    reason_text = discord.ui.TextInput(
        label="辞退理由", placeholder="例: バイトのシフトと被っていて",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, meeting_id: int, from_user_id: int):
        super().__init__()
        self.bot = bot
        self.meeting_id = meeting_id
        self.from_user_id = from_user_id

    async def on_submit(self, interaction: discord.Interaction):
        await _send_decline(self.bot, interaction, self.meeting_id, self.from_user_id, self.reason_text.value)


async def _send_decline(bot, interaction, meeting_id, from_user_id, reason):
    await bot.db.update_meeting_status(meeting_id, "declined", reason)
    try:
        from_user = await bot.fetch_user(from_user_id)
        if from_user:
            embed = discord.Embed(title="❌ 日程が辞退されました", color=config.COLOR_DANGER,
                description=f"**{interaction.user.display_name}** さんが辞退しました。")
            embed.add_field(name="💬 理由", value=reason, inline=False)
            embed.set_footer(text="しんちょくん 🤖 | 日程調整")
            async with dm_limiter:
                await from_user.send(embed=embed)
    except Exception as e:
        logger.error(f"❌ 辞退通知エラー: {e}")
    await interaction.response.send_message("✅ 辞退が通知されました。", ephemeral=True)


class MeetingResponseView(discord.ui.View):
    def __init__(self, bot: commands.Bot, meeting_id: int, from_user_id: int):
        super().__init__(timeout=86400)
        self.bot = bot
        self.meeting_id = meeting_id
        self.from_user_id = from_user_id

    @discord.ui.button(label="✅ 参加する", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.db.update_meeting_status(self.meeting_id, "accepted")
        try:
            from_user = await self.bot.fetch_user(self.from_user_id)
            if from_user:
                meeting = await self.bot.db.get_meeting_request(self.meeting_id)
                embed = discord.Embed(title="✅ 参加が確定しました！", color=config.COLOR_SUCCESS,
                    description=f"**{interaction.user.display_name}** さんが参加を承諾しました。")
                if meeting:
                    embed.add_field(name="📅 日時", value=meeting["proposed_datetime"], inline=True)
                    embed.add_field(name="📝 内容", value=meeting["subject"], inline=True)
                embed.set_footer(text="しんちょくん 🤖 | 日程調整")
                async with dm_limiter:
                    await from_user.send(embed=embed)
        except Exception as e:
            logger.error(f"❌ 参加通知エラー: {e}")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="✅ **参加を承諾しました！**", view=self)

    @discord.ui.button(label="📅 別日を提案", style=discord.ButtonStyle.primary)
    async def reschedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RescheduleModal(self.bot, self.meeting_id, self.from_user_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="❌ 辞退する", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = DeclineReasonSelectView(self.bot, self.meeting_id, self.from_user_id)
        await interaction.response.send_message("辞退理由を選択してください:", view=view, ephemeral=True)


class DeclineReasonSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, meeting_id: int, from_user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.meeting_id = meeting_id
        self.from_user_id = from_user_id

    @discord.ui.button(label="予定あり", style=discord.ButtonStyle.secondary)
    async def reason_busy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _send_decline(self.bot, interaction, self.meeting_id, self.from_user_id, "予定あり")

    @discord.ui.button(label="体調不良", style=discord.ButtonStyle.secondary)
    async def reason_health(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _send_decline(self.bot, interaction, self.meeting_id, self.from_user_id, "体調不良")

    @discord.ui.button(label="準備が必要", style=discord.ButtonStyle.secondary)
    async def reason_prep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _send_decline(self.bot, interaction, self.meeting_id, self.from_user_id, "準備が必要")

    @discord.ui.button(label="その他（入力）", style=discord.ButtonStyle.primary)
    async def reason_other(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DeclineReasonModal(self.bot, self.meeting_id, self.from_user_id)
        await interaction.response.send_modal(modal)


async def setup(bot: commands.Bot):
    pass  # Cog不要（Viewのみ提供）
