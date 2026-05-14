"""
cogs/scheduler.py — 日程調整「調整さん」方式（v2.2 新規）

全体向けの日程調整投票機能。
1. リーダーが議題＋候補日時を入力
2. 全メンバーにDMで投票フォームが届く（チェックボックス式）
3. 48時間 or 全員回答で自動集計
4. リーダーDMに集計結果 + 確定ボタン
5. 確定 → Discordサーバーイベント自動作成 + 全員にDM通知

【このファイルで学べること】
- discord.ui.Select を用いた複数選択のUI作成（min_values, max_values）
- discord.Guild.create_scheduled_event によるサーバーイベント作成
- utils.messaging.send_dm_safely を用いた一括DM送信のエラーハンドリング

【実務的な設計意図（イベント駆動とスケジュール実行）】
日程調整の集計は「全員が回答した瞬間（イベント駆動）」と「期限が切れた時（スケジュール実行）」
の2つのトリガーで動作します。定期実行（bot.pyのAPScheduler経由）と
ユーザー操作の両方から同じ `_tally_and_notify` ロジックを呼ぶことで、
漏れなく集計を行いつつ、迅速な結果反映を実現しています。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
from rate_limiter import dm_limiter
from utils.messaging import send_dm_safely

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

POLL_EXPIRE_HOURS = 36


# ============================================
# 候補日時入力フォーム
# ============================================

class ScheduleCreateModal(discord.ui.Modal, title="📊 日程調整を作成"):
    subject_input = discord.ui.TextInput(
        label="議題（1行に1議題、最大5つ）", placeholder="例: Blenderの相談",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )
    candidates_input = discord.ui.TextInput(
        label="候補日時（1行に1候補、最大5つ）",
        placeholder="例:\n5/10（土）10:00\n5/10（土）14:00\n5/11（日）10:00",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        subject = self.subject_input.value.strip()
        raw = self.candidates_input.value.strip()
        candidates = [line.strip() for line in raw.split("\n") if line.strip()][:5]

        if len(candidates) < 2:
            await interaction.followup.send("⚠️ 候補日時は2つ以上入力してください。", ephemeral=True)
            return

        # DB保存
        expires_at = (datetime.now(JST) + timedelta(hours=POLL_EXPIRE_HOURS)).isoformat()
        poll_id = await self.bot.db.create_schedule_poll(
            created_by=interaction.user.id,
            subject=subject,
            candidates_json=json.dumps(candidates, ensure_ascii=False),
            expires_at=expires_at,
        )

        sent = await self._broadcast_poll(interaction, poll_id, subject, candidates)

        await interaction.followup.send(
            f"✅ 日程調整を作成しました！ **{sent}人** にDMを送信しました。\n"
            f"📝 議題: {subject}\n"
            f"⏰ {POLL_EXPIRE_HOURS}時間後に自動集計されます。",
            ephemeral=True,
        )
        logger.info(f"📊 日程調整作成: poll_id={poll_id}, {sent}人に送信")

    async def _broadcast_poll(self, interaction: discord.Interaction, poll_id: int, subject: str, candidates: list[str]) -> int:
        """全メンバーに投票DMを一括送信する（関数分割）"""
        member_ids = config.get_active_member_ids()
        sent = 0
        for uid in member_ids:
            if uid == interaction.user.id:
                continue
            if user := await self.bot.fetch_user(uid):
                embed = discord.Embed(
                    title="📊 日程調整のお知らせ",
                    description=(
                        f"**{interaction.user.display_name}** さんが日程調整を作成しました。\n"
                        f"出席可能な日時をすべて選択してください。"
                    ),
                    color=config.COLOR_PRIMARY,
                )
                embed.add_field(name="📝 議題", value=subject, inline=False)
                embed.add_field(
                    name="📅 候補日時",
                    value="\n".join(f"• {c}" for c in candidates),
                    inline=False,
                )
                embed.add_field(
                    name="⏰ 回答期限",
                    value=f"{POLL_EXPIRE_HOURS}時間以内",
                    inline=True,
                )
                embed.set_footer(text=f"しんちょくん 🤖 | 投票ID: {poll_id}")

                view = ScheduleVoteView(self.bot, poll_id, candidates)
                success = await send_dm_safely(user, embed=embed, view=view)
                if success:
                    sent += 1
        return sent


# ============================================
# 投票View（チェックボックス式）
# ============================================

class ScheduleVoteView(discord.ui.View):
    def __init__(self, bot: commands.Bot, poll_id: int, candidates: list[str]):
        super().__init__(timeout=POLL_EXPIRE_HOURS * 3600)
        self.bot = bot
        self.poll_id = poll_id
        self.candidates = candidates

        options = [
            discord.SelectOption(label=c, value=c)
            for c in candidates
        ]
        select = discord.ui.Select(
            placeholder="出席可能な日時をすべて選択...",
            options=options,
            min_values=1,
            max_values=len(options),
            custom_id=f"schedule_vote_{poll_id}",
        )
        select.callback = self.vote_callback
        self.add_item(select)

    async def vote_callback(self, interaction: discord.Interaction):
        selected = interaction.data["values"]
        is_new = await self.bot.db.save_schedule_vote(
            self.poll_id, interaction.user.id,
            json.dumps(selected, ensure_ascii=False),
        )

        msg = "✅ 回答を送信しました！" if is_new else "🔄 回答を更新しました！"
        await interaction.response.send_message(
            f"{msg}\n選択: {', '.join(selected)}",
            ephemeral=True,
        )
        logger.info(f"📊 投票受信: poll_id={self.poll_id}, user={interaction.user.id}")

        # 全員回答チェック
        await self._check_all_voted(interaction)

    async def _check_all_voted(self, interaction: discord.Interaction):
        """全員回答したら自動集計"""
        poll = await self.bot.db.get_schedule_poll(self.poll_id)
        if not poll or poll["status"] != "open":
            return

        vote_count = await self.bot.db.get_schedule_vote_count(self.poll_id)
        total_members = len(config.get_active_member_ids()) - 1  # 作成者除く

        if vote_count >= total_members:
            await _tally_and_notify(self.bot, self.poll_id)


# ============================================
# 集計結果 + 確定View
# ============================================

class ScheduleConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, poll_id: int, ranked: list[tuple[str, int, int]]):
        super().__init__(timeout=86400)  # 24時間
        self.bot = bot
        self.poll_id = poll_id

        # 最多得票の候補だけ確定ボタンを付ける（最大3つ）
        for slot, count, total in ranked[:3]:
            btn = discord.ui.Button(
                label=f"✅ {slot} ({count}/{total})",
                style=discord.ButtonStyle.success,
                custom_id=f"confirm_{poll_id}_{slot[:50]}",
            )
            btn.callback = self._make_confirm_callback(slot)
            self.add_item(btn)

    def _make_confirm_callback(self, slot: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self._confirm_schedule(interaction, slot)
        return callback

    async def _confirm_schedule(self, interaction: discord.Interaction, slot: str):
        poll = await self.bot.db.get_schedule_poll(self.poll_id)
        if not poll or poll["status"] != "open":
            await interaction.followup.send("⚠️ この投票は既に確定済みです。", ephemeral=True)
            return

        # サーバーイベント作成を試行
        event_id = 0
        guild = self.bot.get_guild(config.GUILD_ID) if config.GUILD_ID else None
        if guild:
            try:
                start_time = datetime.now(JST) + timedelta(days=1)
                event = await guild.create_scheduled_event(
                    name=f"📅 {poll['subject']}",
                    description=f"日程調整で確定: {slot}\n議題: {poll['subject']}",
                    start_time=start_time,
                    end_time=start_time + timedelta(hours=1),
                    entity_type=discord.EntityType.external,
                    location=slot,
                    privacy_level=discord.PrivacyLevel.guild_only,
                )
                event_id = event.id
                logger.info(f"📅 サーバーイベント作成: {event.name}")
            except discord.Forbidden:
                logger.warning("⚠️ サーバーイベント作成権限がありません。Manage Events権限を確認してください。")
            except Exception as e:
                logger.error(f"❌ サーバーイベント作成エラー: {e}")

        # DB更新
        await self.bot.db.confirm_schedule_poll(self.poll_id, slot, event_id)

        # 全メンバーに確定通知
        member_ids = config.get_active_member_ids()
        for uid in member_ids:
            if user := await self.bot.fetch_user(uid):
                embed = discord.Embed(
                    title="📅 日程が確定しました！",
                    description=f"**{poll['subject']}**",
                    color=config.COLOR_SUCCESS,
                )
                embed.add_field(name="📆 確定日時", value=slot, inline=False)
                if event_id:
                    embed.add_field(name="🔔 サーバーイベント", value="自動作成済み", inline=True)
                embed.set_footer(text="しんちょくん 🤖 | 日程確定")
                await send_dm_safely(user, embed=embed)

        event_msg = "\n🔔 サーバーイベントを作成しました！" if event_id else ""
        await interaction.followup.send(
            f"✅ **{slot}** で確定しました！ 全メンバーに通知済みです。{event_msg}",
            ephemeral=True,
        )


# ============================================
# 集計ロジック
# ============================================

async def _tally_and_notify(bot: commands.Bot, poll_id: int):
    """投票を集計し、作成者に結果を通知する"""
    poll = await bot.db.get_schedule_poll(poll_id)
    if not poll or poll["status"] != "open":
        return

    candidates = json.loads(poll["candidates"])
    votes = await bot.db.get_schedule_votes(poll_id)

    # 各候補の得票数を集計
    tally: dict[str, int] = {c: 0 for c in candidates}
    for vote in votes:
        selected = json.loads(vote["selected_slots"])
        for s in selected:
            if s in tally:
                tally[s] += 1

    total = len(votes)
    ranked = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    ranked_with_total = [(slot, count, total) for slot, count in ranked]

    # 作成者にDMで結果を送信
    if creator := await bot.fetch_user(poll["created_by"]):
        embed = discord.Embed(
            title="📊 日程調整の集計結果",
            description=f"**{poll['subject']}**\n回答者: {total}人",
            color=config.COLOR_PRIMARY,
        )
        result_lines = []
        for slot, count, t in ranked_with_total:
            bar = "█" * count + "░" * (t - count)
            star = " ⭐" if count == ranked_with_total[0][1] and count > 0 else ""
            result_lines.append(f"**{slot}** [{bar}] {count}/{t}{star}")
        embed.add_field(
            name="📅 結果", value="\n".join(result_lines) or "回答なし", inline=False,
        )
        embed.add_field(
            name="💡 操作", value="下のボタンで日時を確定してください。", inline=False,
        )
        embed.set_footer(text=f"しんちょくん 🤖 | 投票ID: {poll_id}")

        view = ScheduleConfirmView(bot, poll_id, ranked_with_total)
        await send_dm_safely(creator, embed=embed, view=view)

        logger.info(f"📊 集計完了: poll_id={poll_id}")


# ============================================
# Scheduler Cog（自動集計チェック）
# ============================================

class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def check_expired_polls(self):
        """期限切れの投票を自動集計する（APSchedulerから呼ばれる）"""
        expired = await self.bot.db.get_open_polls()
        for poll in expired:
            logger.info(f"⏰ 期限切れ投票を集計: poll_id={poll['id']}")
            await _tally_and_notify(self.bot, poll["id"])


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
