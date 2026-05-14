"""
cogs/hub.py — 統合コマンドチャンネル（v2.1）

SOSチャンネルを「しんちょくんコマンドチャンネル」に統合し、
SOS・ミーティング・クエスト状況・マイルストーン・ピアボーナス・ランキングの
全機能を1チャンネルの常設ボタンから利用可能にする。

【このファイルで学べること】
- discord.ui.Modal を用いたテキスト入力フォームの作成
- utils.messaging.send_dm_safely を用いたDMの安全な送信

【実務的な設計意図（例外処理の共通化とDRY原則）】
Discord Botにおいて「DMの送信」は、ユーザーがDMを拒否設定している場合などに
必ず例外（discord.Forbidden）を発生させるリスクを伴います。
以前はこのファイル内のあらゆる箇所で都度 try-except を書いていましたが、
send_dm_safely ヘルパーに共通化することで、コードの重複を排除し（DRY原則）、
ログ出力やレート制限のハンドリングを一元的に管理できるように改善されています。
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands

import config
from rate_limiter import dm_limiter
from gamification import ExpManager, TitleSystem
from utils.messaging import send_dm_safely

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


# ============================================
# SOS機能（sos.pyから移植）
# ============================================

class SOSModal(discord.ui.Modal, title="🆘 SOSフォーム"):
    content = discord.ui.TextInput(
        label="困っていること",
        placeholder="例: UV展開がうまくいかない、テクスチャの参考がほしい",
        style=discord.TextStyle.paragraph, required=True, max_length=1000,
    )
    urgency_input = discord.ui.TextInput(
        label="緊急度（低 / 中 / 高）",
        placeholder="低・中・高 のいずれかを入力",
        style=discord.TextStyle.short, required=True, max_length=2, default="中",
    )

    def __init__(self, bot: commands.Bot, mentor_id: int):
        super().__init__()
        self.bot = bot
        self.mentor_id = mentor_id

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        content_text = self.content.value
        urgency = self.urgency_input.value

        # メンバー別感謝メッセージ
        thanks_msg = "💙 **報告ありがとう！**🙏"
        if self.bot.sheets:
            custom = self.bot.sheets.get_message(user.id, "sos_thanks")
            if custom:
                thanks_msg = f"💙 **{custom}**"

        await interaction.response.send_message(thanks_msg, ephemeral=True)

        urgency_map = {"低": "low", "中": "medium", "高": "high"}
        urgency_key = urgency_map.get(urgency, "medium")
        urgency_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(urgency_key, "🟡")

        # メンターにDM
        if mentor := await self.bot.fetch_user(self.mentor_id):
            embed = discord.Embed(
                title="📩 SOSメッセージが届きました",
                description=f"**{user.display_name}** さんから相談です。",
                color=config.COLOR_SOS,
            )
            embed.add_field(name="📝 困りごと", value=content_text, inline=False)
            embed.add_field(name=f"{urgency_emoji} 緊急度", value=urgency, inline=True)
            embed.set_thumbnail(url=user.display_avatar.url)
            embed.set_footer(text="しんちょくん 🤖 | SOS通知")
            await send_dm_safely(mentor, embed=embed)

        # リーダーにCc
        leader_info = self.bot.sheets.get_leader() if self.bot.sheets else None
        leader_id = leader_info["discord_id"] if leader_info else config.LEADER_ID
        if leader_id and leader_id != self.mentor_id:
            if leader := await self.bot.fetch_user(leader_id):
                cc_embed = discord.Embed(
                    title="📋 SOS通知（Cc）",
                    description=f"**{user.display_name}** さんがメンターに相談しました。",
                    color=config.COLOR_WARNING,
                )
                cc_embed.add_field(name="📝 困りごと", value=content_text, inline=False)
                cc_embed.add_field(name=f"{urgency_emoji} 緊急度", value=urgency, inline=True)
                cc_embed.set_footer(text="しんちょくん 🤖 | Cc通知")
                await send_dm_safely(leader, embed=cc_embed)

        # EXP +5
        try:
            guild = self.bot.get_guild(config.GUILD_ID) if config.GUILD_ID else None
            exp_manager = ExpManager(self.bot.db, self.bot.sheets)
            await exp_manager.grant_exp(user.id, config.EXP_REWARDS["sos_send"], "SOS送信ボーナス", guild=guild)
        except Exception as e:
            logger.error(f"❌ SOS EXP付与エラー: {e}")

        # DB記録
        try:
            await self.bot.db.save_sos_request(user.id, self.mentor_id, content_text, urgency_key)
        except Exception as e:
            logger.error(f"❌ SOS記録エラー: {e}")


class MentorSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, sender_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        options = []
        for uid in config.get_active_member_ids():
            if uid == sender_id:
                continue
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"ID:{uid}"
            options.append(discord.SelectOption(label=name, value=str(uid)))
        if not options:
            options.append(discord.SelectOption(label="メンバー未設定", value="0"))
        select = discord.ui.Select(placeholder="相談したいメンバーを選択...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        mentor_id = int(interaction.data["values"][0])
        if mentor_id == 0:
            await interaction.response.send_message("⚠️ メンバーが設定されていません。", ephemeral=True)
            return
        modal = SOSModal(self.bot, mentor_id)
        await interaction.response.send_modal(modal)


# ============================================
# ミーティング機能（複数人対応・フォーム化）
# ============================================

class MeetingModal(discord.ui.Modal, title="📅 ミーティング調整"):
    datetime_input = discord.ui.TextInput(
        label="日時", placeholder="例: 5/1（木）14:00",
        style=discord.TextStyle.short, required=True, max_length=100,
    )
    subject_input = discord.ui.TextInput(
        label="内容", placeholder="例: キャラモデルのレビュー",
        style=discord.TextStyle.short, required=True, max_length=200,
    )

    def __init__(self, bot: commands.Bot, target_ids: list[int]):
        super().__init__()
        self.bot = bot
        self.target_ids = target_ids

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        dt = self.datetime_input.value
        subject = self.subject_input.value
        sent = 0

        for uid in self.target_ids:
            meeting_id = await self.bot.db.save_meeting_request(
                interaction.user.id, uid, dt, subject
            )
            if user := await self.bot.fetch_user(uid):
                embed = discord.Embed(
                    title="📅 打ち合わせの打診",
                    description=f"**{interaction.user.display_name}** さんから提案があります。",
                    color=config.COLOR_PRIMARY,
                )
                embed.add_field(name="📆 日時", value=dt, inline=True)
                embed.add_field(name="📝 内容", value=subject, inline=True)
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.set_footer(text="しんちょくん 🤖 | 日程調整")

                from cogs.meeting import MeetingResponseView
                view = MeetingResponseView(self.bot, meeting_id, interaction.user.id)
                success = await send_dm_safely(user, embed=embed, view=view)
                if success:
                    sent += 1

        await interaction.followup.send(f"✅ {sent}人にミーティング打診を送信しました！\n📆 {dt} | 📝 {subject}", ephemeral=True)


class MeetingMemberSelect(discord.ui.View):
    def __init__(self, bot: commands.Bot, sender_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        options = []
        for uid in config.get_active_member_ids():
            if uid == sender_id:
                continue
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"ID:{uid}"
            options.append(discord.SelectOption(label=name, value=str(uid)))
        if not options:
            options.append(discord.SelectOption(label="メンバー未設定", value="0"))
        select = discord.ui.Select(
            placeholder="参加者を選択（複数可）...", options=options,
            min_values=1, max_values=max(1, len(options)),
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        selected = [int(v) for v in interaction.data["values"]]
        if 0 in selected:
            await interaction.response.send_message("⚠️ メンバーが設定されていません。", ephemeral=True)
            return
        modal = MeetingModal(self.bot, selected)
        await interaction.response.send_modal(modal)


# ============================================
# ピアボーナス機能
# ============================================

class PeerBonusModal(discord.ui.Modal, title="🎁 ピアボーナス"):
    message_input = discord.ui.TextInput(
        label="感謝のメッセージ",
        placeholder="例: UV展開手伝ってくれてありがとう！",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, target_id: int):
        super().__init__()
        self.bot = bot
        self.target_id = target_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sender = interaction.user
        msg_text = self.message_input.value

        # 1日上限チェック
        today_count = await self.bot.db.get_today_bonus_count(sender.id)
        if today_count >= config.PEER_BONUS_DAILY_LIMIT:
            await interaction.followup.send(
                f"⚠️ 今日の送信上限（{config.PEER_BONUS_DAILY_LIMIT}回）に達しています。また明日！",
                ephemeral=True,
            )
            return

        # DB記録
        await self.bot.db.save_peer_bonus(sender.id, self.target_id, msg_text)

        # EXP付与（双方）
        guild = self.bot.get_guild(config.GUILD_ID) if config.GUILD_ID else None
        exp_manager = ExpManager(self.bot.db, self.bot.sheets)
        await exp_manager.grant_exp(sender.id, config.EXP_REWARDS["peer_bonus_send"], "ピアボーナス送信", guild=guild)
        await exp_manager.grant_exp(self.target_id, config.EXP_REWARDS["peer_bonus_receive"], "ピアボーナス受信", guild=guild)

        # 相手にDM
        if target_user := await self.bot.fetch_user(self.target_id):
            bonus_msg = "感謝のメッセージが届きました 💌"
            if self.bot.sheets:
                custom = self.bot.sheets.get_message(self.target_id, "peer_bonus")
                if custom:
                    bonus_msg = custom

            nickname = ""
            if self.bot.sheets:
                nickname = self.bot.sheets.get_member_nickname(sender.id)
            sender_name = nickname or sender.display_name

            embed = discord.Embed(
                title=f"🎁 {bonus_msg}",
                description=f"**{sender_name}** さんからの感謝です。",
                color=config.COLOR_PEER,
            )
            embed.add_field(name="💬 メッセージ", value=msg_text, inline=False)
            embed.add_field(name="⭐ ボーナス", value=f"+{config.EXP_REWARDS['peer_bonus_receive']} EXP", inline=True)
            embed.set_thumbnail(url=sender.display_avatar.url)
            embed.set_footer(text="しんちょくん 🤖 | ピアボーナス")
            await send_dm_safely(target_user, embed=embed)

        remaining = config.PEER_BONUS_DAILY_LIMIT - today_count - 1
        await interaction.followup.send(
            f"🎁 感謝を送信しました！ 双方に **+{config.EXP_REWARDS['peer_bonus_send']} EXP** 💌\n"
            f"今日の残り送信回数: {remaining}回",
            ephemeral=True,
        )


class PeerBonusSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, sender_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        options = []
        for uid in config.get_active_member_ids():
            if uid == sender_id:
                continue
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"ID:{uid}"
            options.append(discord.SelectOption(label=name, value=str(uid)))
        if not options:
            options.append(discord.SelectOption(label="メンバー未設定", value="0"))
        select = discord.ui.Select(placeholder="感謝を送る相手を選択...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        target_id = int(interaction.data["values"][0])
        if target_id == 0:
            await interaction.response.send_message("⚠️ メンバーが設定されていません。", ephemeral=True)
            return
        modal = PeerBonusModal(self.bot, target_id)
        await interaction.response.send_modal(modal)


# ============================================
# タスク追加申請フォーム（v2.2）
# ============================================

class TaskRequestModal(discord.ui.Modal, title="📝 タスク追加申請"):
    title_input = discord.ui.TextInput(
        label="タスク名", placeholder="例: ステージBGMの作曲",
        style=discord.TextStyle.short, required=True, max_length=100,
    )
    detail_input = discord.ui.TextInput(
        label="詳細（任意）", placeholder="例: テンポ120前後、戦闘向け",
        style=discord.TextStyle.paragraph, required=False, max_length=500,
    )
    exp_input = discord.ui.TextInput(
        label="希望EXP量（空欄で30）", placeholder="30",
        style=discord.TextStyle.short, required=False, max_length=5, default="30",
    )

    def __init__(self, bot: commands.Bot, week_start: str):
        super().__init__()
        self.bot = bot
        self.week_start = week_start

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        title = self.title_input.value.strip()
        detail = self.detail_input.value.strip()
        try:
            exp = int(self.exp_input.value.strip() or "30")
        except ValueError:
            exp = 30

        req_id = await self.bot.db.create_task_request(
            discord_id=interaction.user.id,
            quest_title=title, quest_detail=detail,
            requested_exp=exp, week_start=self.week_start,
        )

        leader_info = self.bot.sheets.get_leader() if self.bot.sheets else None
        leader_id = leader_info["discord_id"] if leader_info else config.LEADER_ID
        if leader := await self.bot.fetch_user(leader_id):
            embed = discord.Embed(
                title="📝 タスク追加申請",
                description=f"**{interaction.user.display_name}** さんからタスク申請があります。",
                color=config.COLOR_PRIMARY,
            )
            embed.add_field(name="📋 タスク名", value=title, inline=False)
            if detail:
                embed.add_field(name="📝 詳細", value=detail, inline=False)
            embed.add_field(name="⭐ 希望EXP", value=f"{exp} EXP", inline=True)
            embed.add_field(name="📅 対象週", value=self.week_start, inline=True)
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.set_footer(text=f"しんちょくん 🤖 | 申請ID: {req_id}")
            view = TaskApprovalView(self.bot, req_id)
            await send_dm_safely(leader, embed=embed, view=view)

        await interaction.followup.send(
            f"✅ タスク申請を送信しました！\n📋 {title} | ⭐ {exp} EXP | 📅 {self.week_start}",
            ephemeral=True,
        )


class TaskApprovalView(discord.ui.View):
    def __init__(self, bot: commands.Bot, request_id: int):
        super().__init__(timeout=86400 * 3)
        self.bot = bot
        self.request_id = request_id

    async def _disable_buttons(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

    @discord.ui.button(label="✅ 承認", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        req = await self.bot.db.get_task_request(self.request_id)
        if not req or req["status"] != "pending":
            await interaction.followup.send("⚠️ この申請は処理済みです。", ephemeral=True)
            return

        await self.bot.db.update_task_request(self.request_id, "approved", interaction.user.id)

        if self.bot.sheets:
            try:
                await self.bot.sheets.append_quest_row(
                    req["week_start"], req["discord_id"],
                    req["quest_title"], req["quest_detail"], req["requested_exp"],
                )
            except Exception as e:
                logger.error(f"❌ Sheetsタスク追記エラー: {e}")

        if user := await self.bot.fetch_user(req["discord_id"]):
            embed = discord.Embed(title="✅ タスク申請が承認されました！", color=config.COLOR_SUCCESS)
            embed.add_field(name="📋 タスク名", value=req["quest_title"], inline=False)
            embed.add_field(name="⭐ EXP", value=f"{req['requested_exp']} EXP", inline=True)
            embed.set_footer(text="しんちょくん 🤖")
            await send_dm_safely(user, embed=embed)

        await self._disable_buttons(interaction)
        await interaction.followup.send("✅ 承認しました！ Sheetsに追記済みです。", ephemeral=True)

    @discord.ui.button(label="📝 採用（変更あり）", style=discord.ButtonStyle.primary)
    async def adopt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TaskAdoptModal(self.bot, self.request_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🤔 検討", style=discord.ButtonStyle.secondary)
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TaskReviewModal(self.bot, self.request_id)
        await interaction.response.send_modal(modal)


class TaskAdoptModal(discord.ui.Modal, title="📝 採用（変更あり）"):
    comment_input = discord.ui.TextInput(
        label="変更内容・コメント",
        placeholder="例: EXPを50に変更して採用します",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, request_id: int):
        super().__init__()
        self.bot = bot
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        req = await self.bot.db.get_task_request(self.request_id)
        if not req or req["status"] != "pending":
            await interaction.followup.send("⚠️ この申請は処理済みです。", ephemeral=True)
            return

        comment = self.comment_input.value.strip()
        await self.bot.db.update_task_request(self.request_id, "adopted", interaction.user.id, comment)

        if self.bot.sheets:
            try:
                await self.bot.sheets.append_quest_row(
                    req["week_start"], req["discord_id"],
                    req["quest_title"], req["quest_detail"], req["requested_exp"],
                )
            except Exception as e:
                logger.error(f"❌ Sheetsタスク追記エラー: {e}")

        if user := await self.bot.fetch_user(req["discord_id"]):
            embed = discord.Embed(title="📝 タスク申請が採用されました！", color=config.COLOR_PRIMARY)
            embed.add_field(name="📋 タスク名", value=req["quest_title"], inline=False)
            embed.add_field(name="💬 リーダーより", value=comment, inline=False)
            embed.add_field(name="⭐ EXP", value=f"{req['requested_exp']} EXP", inline=True)
            embed.set_footer(text="しんちょくん 🤖 | 内容に変更がある場合があります")
            await send_dm_safely(user, embed=embed)

        await interaction.followup.send("📝 採用しました！ Sheetsに追記済みです。", ephemeral=True)


class TaskReviewModal(discord.ui.Modal, title="🤔 検討中"):
    comment_input = discord.ui.TextInput(
        label="コメント（申請者に伝えたいこと）",
        placeholder="例: 来週に回せるか確認させてください",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, request_id: int):
        super().__init__()
        self.bot = bot
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        req = await self.bot.db.get_task_request(self.request_id)
        if not req or req["status"] != "pending":
            await interaction.followup.send("⚠️ この申請は処理済みです。", ephemeral=True)
            return

        comment = self.comment_input.value.strip()
        await self.bot.db.update_task_request(self.request_id, "reviewing", interaction.user.id, comment)

        if user := await self.bot.fetch_user(req["discord_id"]):
            embed = discord.Embed(title="🤔 タスク申請を検討中です", color=config.COLOR_WARNING)
            embed.add_field(name="📋 タスク名", value=req["quest_title"], inline=False)
            embed.add_field(name="💬 リーダーより", value=comment, inline=False)
            embed.set_footer(text="しんちょくん 🤖 | 後日改めて連絡があります")
            await send_dm_safely(user, embed=embed)

        await interaction.followup.send("🤔 検討中にしました。申請者に通知済みです。", ephemeral=True)


# ============================================
# ミーティングモード選択（v2.2）
# ============================================

class MeetingModeSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, sender_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.sender_id = sender_id

    @discord.ui.button(label="📅 個別打診", style=discord.ButtonStyle.primary)
    async def direct_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MeetingMemberSelect(self.bot, self.sender_id)
        await interaction.response.send_message(
            "📅 **参加者を選んでください（複数選択可）**", view=view, ephemeral=True
        )

    @discord.ui.button(label="📊 日程調整", style=discord.ButtonStyle.success)
    async def poll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.scheduler import ScheduleCreateModal
        modal = ScheduleCreateModal(self.bot)
        await interaction.response.send_modal(modal)


class TaskWeekSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label="📅 今週", style=discord.ButtonStyle.primary)
    async def this_week(self, interaction: discord.Interaction, button: discord.ui.Button):
        from sheets import SheetsClient
        week = SheetsClient.get_current_week_start()
        modal = TaskRequestModal(self.bot, week)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="📅 来週", style=discord.ButtonStyle.secondary)
    async def next_week(self, interaction: discord.Interaction, button: discord.ui.Button):
        today = datetime.now(JST).date()
        monday = today - timedelta(days=today.weekday())
        next_monday = monday + timedelta(days=7)
        week = next_monday.strftime("%Y-%m-%d")
        modal = TaskRequestModal(self.bot, week)
        await interaction.response.send_modal(modal)


# ============================================
# 統合コマンドチャンネル常設View（永続）v2.2
# ============================================

class ResendQuestView(discord.ui.View):
    def __init__(self, bot: commands.Bot, pending_quests: list[dict], week_start: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.pending_quests = pending_quests
        self.week_start = week_start

    @discord.ui.button(label="📩 未完クエストをDMに再通知する", style=discord.ButtonStyle.primary)
    async def resend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        quest_cog = self.bot.get_cog("QuestCog")
        if quest_cog is None:
            await interaction.followup.send("⚠️ クエスト機能が現在利用できません。", ephemeral=True)
            return
            
        try:
            user_quests = self.pending_quests[:5]
            user_id = interaction.user.id
            members = self.bot.sheets.get_members()
            autonomy = "low"
            for m in members:
                if m["discord_id"] == user_id:
                    autonomy = m.get("autonomy_level", "low")
                    break

            embed = quest_cog._build_quest_embed(user_quests, autonomy, self.week_start, user_id)
            from cogs.quest import QuestView
            view = QuestView(self.bot)
            
            success = await send_dm_safely(interaction.user, embed=embed, view=view)
            if success:
                await interaction.followup.send("✅ DMに未完クエストを再通知しました！", ephemeral=True)
                button.disabled = True
                button.label = "📩 再通知済み"
                await interaction.edit_original_response(view=self)
            else:
                await interaction.followup.send("⚠️ DMを送信できませんでした。サーバーのプライバシー設定を確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"❌ 再送信エラー: {e}")
            await interaction.followup.send(f"❌ 送信に失敗しました: {e}", ephemeral=True)


class PersistentHubView(discord.ui.View):
    """全機能を集約した永続ボタンView（v2.2）"""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🆘 SOS", style=discord.ButtonStyle.danger, custom_id="hub_sos_btn", row=0)
    async def sos_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MentorSelectView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            "🧑‍🏫 **相談したいメンバーを選んでください**", view=view, ephemeral=True
        )

    @discord.ui.button(label="📅 ミーティング", style=discord.ButtonStyle.primary, custom_id="hub_meeting_btn", row=0)
    async def meeting_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MeetingModeSelectView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            "📅 **モードを選んでください**\n• **個別打診** — 相手を選んで日時を提案\n• **日程調整** — 全員に候補日時を投票してもらう",
            view=view, ephemeral=True
        )

    @discord.ui.button(label="⚔️ クエスト状況", style=discord.ButtonStyle.secondary, custom_id="hub_quest_btn", row=1)
    async def quest_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        exp_manager = ExpManager(self.bot.db, getattr(self.bot, "sheets", None))
        profile = await exp_manager.ensure_profile(user_id)
        embed = discord.Embed(title=f"⚔️ {interaction.user.display_name} のクエスト状況", color=config.COLOR_QUEST)
        embed.add_field(name="🏅 称号", value=profile["current_title"] or "未設定", inline=True)
        embed.add_field(name="📊 レベル", value=f"Lv.{profile['level']}", inline=True)
        embed.add_field(name="💰 累計EXP", value=f"{profile['total_exp']} EXP", inline=True)
        next_exp = TitleSystem.get_exp_for_next_level(profile["level"])
        current_level_exp = profile["level"] ** 2 * config.EXP_PER_LEVEL_UNIT
        progress = profile["total_exp"] - current_level_exp
        needed = next_exp - current_level_exp
        filled = min(10, int(progress / needed * 10)) if needed > 0 else 10
        bar = "▓" * filled + "░" * (10 - filled)
        remaining = max(0, next_exp - profile["total_exp"])
        embed.add_field(name="📈 次のレベルまで", value=f"[{bar}] 残り **{remaining} EXP**", inline=False)
        pending_quests = []
        if self.bot.sheets:
            try:
                week_start = self.bot.sheets.get_current_week_start()
                quests = await self.bot.sheets.get_weekly_quests(week_start)
                user_quests = [q for q in quests if q["discord_id"] == user_id]
                completions = await self.bot.db.get_quest_completions(user_id, week_start)
                completed_titles = {c["quest_title"] for c in completions}
                if user_quests:
                    lines = []
                    for q in user_quests:
                        if q['quest_title'] in completed_titles:
                            lines.append(f"✅ {q['quest_title']}")
                        else:
                            if q.get("status") not in ["辞退", "延長"]:
                                pending_quests.append(q)
                            lines.append(f"⬜ {q['quest_title']}")
                    embed.add_field(name=f"📋 今週のクエスト ({len(completed_titles)}/{len(user_quests)})", value="\n".join(lines), inline=False)
            except Exception as e:
                logger.warning(f"⚠️ クエスト情報取得エラー: {e}")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="しんちょくん 🤖")
        
        view = None
        if pending_quests:
            view = ResendQuestView(self.bot, pending_quests, week_start)
            
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🎯 今後の目標", style=discord.ButtonStyle.secondary, custom_id="hub_milestone_btn", row=1)
    async def milestone_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        today = datetime.now(JST).date()
        embed = discord.Embed(title="🎯 今後の目標", color=config.COLOR_PRIMARY)
        for ms in config.MILESTONES:
            deadline = datetime.strptime(ms["deadline"], "%Y-%m-%d").date()
            days = (deadline - today).days
            if days < 0:
                status = f"✅ 完了期限超過 ({abs(days)}日超過)"
            elif days == 0:
                status = "🔥 今日が期限！"
            elif days <= 7:
                status = f"⚠️ 残り **{days}日**"
            else:
                status = f"🕐 残り **{days}日**"
            embed.add_field(name=f"{ms['name']}（{ms['deadline']}）", value=f"{status}\n{ms['description']}", inline=False)
        embed.set_footer(text="しんちょくん 🤖")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="💌 ありがとうを送る", style=discord.ButtonStyle.success, custom_id="hub_peer_btn", row=2)
    async def peer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PeerBonusSelectView(self.bot, interaction.user.id)
        await interaction.response.send_message("💌 **感謝を送る相手を選んでください**", view=view, ephemeral=True)

    @discord.ui.button(label="🏆 ランキング", style=discord.ButtonStyle.secondary, custom_id="hub_ranking_btn", row=2)
    async def ranking_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        profiles = await self.bot.db.get_all_user_profiles()
        if not profiles:
            await interaction.followup.send("📊 ランキングデータがまだありません。", ephemeral=True)
            return
        embed = discord.Embed(title="🏆 EXPランキング", color=config.COLOR_GOLD)
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, p in enumerate(profiles):
            user = self.bot.get_user(p["discord_id"])
            name = user.display_name if user else f"ID:{p['discord_id']}"
            medal = medals[i] if i < len(medals) else f"{i+1}."
            embed.add_field(name=f"{medal} {name}", value=f"🏅 {p['current_title']}\n📊 Lv.{p['level']} | 💰 {p['total_exp']} EXP", inline=False)
        embed.set_footer(text="しんちょくん 🤖 | ランキング")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="📝 タスク申請", style=discord.ButtonStyle.primary, custom_id="hub_task_btn", row=3)
    async def task_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = TaskWeekSelectView(self.bot)
        await interaction.response.send_message("📝 **どの週にタスクを申請しますか？**", view=view, ephemeral=True)


# ============================================
# Hub Cog
# ============================================

class HubCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup_hub", description="コマンドチャンネルに常設フォームを設置します")
    @app_commands.default_permissions(administrator=True)
    async def setup_hub(self, interaction: discord.Interaction):
        channel = self.bot.get_channel(config.SOS_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("⚠️ チャンネルが見つかりません。`.env` の `SOS_CHANNEL_ID` を確認してください。", ephemeral=True)
            return

        embed = discord.Embed(
            title="🤖 しんちょくん コマンドセンター",
            description=(
                "以下のボタンから各機能を利用できます。\n"
                "ボタンを押すと**あなただけに見える**メッセージで応答します。\n\n"
                "🆘 **SOS** — 困ったときは誰かに相談！\n"
                "📅 **ミーティング** — 個別打診 or 全体日程調整\n"
                "⚔️ **クエスト状況** — 自分のクエスト・EXP確認\n"
                "🎯 **今後の目標** — プロジェクト締切確認\n"
                "💌 **ありがとうを送る** — メンバーに感謝を送る\n"
                "🏆 **ランキング** — チームのEXPランキング\n"
                "📝 **タスク申請** — きむらにタスク追加を申請"
            ),
            color=config.COLOR_PRIMARY,
        )
        embed.set_footer(text="しんちょくん 🤖 | コマンドセンター")

        view = PersistentHubView(self.bot)
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ コマンドセンターを設置しました！", ephemeral=True)
        logger.info("✅ ハブ常設フォーム設置完了")


async def setup(bot: commands.Bot):
    await bot.add_cog(HubCog(bot))

