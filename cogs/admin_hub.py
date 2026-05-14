"""
cogs/admin_hub.py — 管理者専用の統合コマンドハブ（v2.2 新規）

管理者向け機能を1つのメッセージに常設し、UIから直感的に操作できるようにします。

【このファイルで学べること】
- discord.ui.View と discord.ui.Modal によるインタラクティブUI
- discord.ui.Select を用いた動的なユーザー選択・ドロップダウン

【実務的な設計意図（MVCパターンとファットUIの課題）】
現在、このファイルには「UIの見た目（View）」と「裏側の処理（Controller/Model）」が
混在しており、ファイルサイズが大きくなっています（ファットUI）。
本来のソフトウェアアーキテクチャ（MVCパターンなど）では、
`views/` ディレクトリ等を作成してUI定義を別ファイルに分離するのが理想です。
（現状はv2.2でのスピード開発を優先して1ファイルにまとめていますが、
今後のリファクタリングではUI要素の別モジュール化を検討すべき部分です）。
"""

import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from discord import app_commands

import config
from gamification import ExpManager, TitleSystem

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

# レア度定義 (色対応)
RARITY_COLORS = {
    "normal": {"name": "ノーマル", "color": 0x95A5A6, "label": "⚪ ノーマル (灰)"},
    "rare": {"name": "レア", "color": 0x3498DB, "label": "🔵 レア (青)"},
    "epic": {"name": "エピック", "color": 0x9B59B6, "label": "🟣 エピック (紫)"},
    "legendary": {"name": "レジェンド", "color": 0xF1C40F, "label": "🟡 レジェンド (金)"},
}

# ============================================
# 隠し称号付与 UIフロー
# ============================================

class HiddenTitleModal(discord.ui.Modal, title="🏅 隠し称号の付与"):
    title_name = discord.ui.TextInput(
        label="称号名",
        placeholder="例: 伝説のバグハンター",
        style=discord.TextStyle.short, required=True, max_length=50,
    )
    reason = discord.ui.TextInput(
        label="付与の理由（活躍内容）",
        placeholder="例: あの進行不能バグの原因を特定してくれました！",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )
    exp_reward = discord.ui.TextInput(
        label="獲得EXP（数値のみ）",
        placeholder="例: 500",
        style=discord.TextStyle.short, required=True, max_length=10, default="100",
    )

    def __init__(self, bot: commands.Bot, target_user: discord.Member, rarity_key: str):
        super().__init__()
        self.bot = bot
        self.target_user = target_user
        self.rarity_key = rarity_key

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            exp_amount = int(self.exp_reward.value.strip())
        except ValueError:
            await interaction.followup.send("⚠️ 獲得EXPには数値を入力してください。", ephemeral=True)
            return

        rarity_info = RARITY_COLORS.get(self.rarity_key, RARITY_COLORS["normal"])
        role_name = f"称号: {self.title_name.value.strip()}"
        role_color = discord.Color(rarity_info["color"])

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("⚠️ サーバー内でのみ実行できます。", ephemeral=True)
            return

        # 1. ロールの作成または取得
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(
                    name=role_name, color=role_color, hoist=False, mentionable=False,
                    reason=f"しんちょくん: 隠し称号 '{self.title_name.value}'"
                )
                logger.info(f"✨ 隠し称号ロール作成: {role_name}")
            except Exception as e:
                logger.error(f"❌ ロール作成エラー: {e}")
                await interaction.followup.send("❌ ロールの作成に失敗しました。Manage Roles 権限を確認してください。", ephemeral=True)
                return

        # 2. ロールの付与
        try:
            if role not in self.target_user.roles:
                await self.target_user.add_roles(role, reason="しんちょくん: 隠し称号付与")
        except Exception as e:
            logger.error(f"❌ ロール付与エラー: {e}")
            await interaction.followup.send("❌ ロールの付与に失敗しました。", ephemeral=True)
            return

        # 3. EXPの付与とログ記録
        try:
            if self.bot.sheets:
                await self.bot.sheets.append_exp_log(
                    discord_id=self.target_user.id,
                    amount=exp_amount,
                    reason=f"隠し称号獲得: {self.title_name.value}"
                )
            
            exp_mgr = ExpManager(self.bot.db, self.bot.sheets)
            await exp_mgr.ensure_profile(self.target_user.id)
            profile = await self.bot.db.get_user_profile(self.target_user.id)
            if profile:
                new_exp = profile["total_exp"] + exp_amount
                # 隠し称号はメインの称号(Job/Level)は上書きせず、純粋にEXPだけ加算する
                await self.bot.db.update_exp(self.target_user.id, new_exp, profile["current_title"], profile["level"])
        except Exception as e:
            logger.error(f"❌ EXP付与エラー: {e}")
            # 継続してアナウンスは送る

        # 4. 全体アナウンス
        channel = guild.get_channel(config.PROGRESS_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🎉 隠し称号 実績解除！",
                description=f"{self.target_user.mention} さんが新たな実績を解除し、称号を獲得しました！",
                color=role_color,
            )
            embed.add_field(name="🏅 獲得称号", value=f"**『{self.title_name.value}』**\n(レア度: {rarity_info['name']})", inline=False)
            embed.add_field(name="📝 活躍内容", value=self.reason.value, inline=False)
            embed.add_field(name="🎁 ボーナス", value=f"**+{exp_amount} EXP**", inline=False)
            embed.set_thumbnail(url=self.target_user.display_avatar.url)
            embed.set_footer(text="しんちょくん 🤖 | 隠し称号システム")

            await channel.send(content=f"{self.target_user.mention} おめでとうございます！", embed=embed)

        await interaction.followup.send(f"✅ {self.target_user.display_name} さんに隠し称号を付与し、アナウンスを送信しました。", ephemeral=True)


class HiddenTitleSetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot
        self.selected_user: discord.Member | None = None
        self.selected_rarity: str | None = None

        # ユーザー選択
        self.user_select = discord.ui.UserSelect(
            placeholder="称号を付与するメンバーを選択",
            custom_id="hidden_title_user_select"
        )
        self.user_select.callback = self.user_callback
        self.add_item(self.user_select)

        # レア度選択
        options = [
            discord.SelectOption(label=r["label"], value=k)
            for k, r in RARITY_COLORS.items()
        ]
        self.rarity_select = discord.ui.Select(
            placeholder="称号のレア度を選択",
            options=options,
            custom_id="hidden_title_rarity_select"
        )
        self.rarity_select.callback = self.rarity_callback
        self.add_item(self.rarity_select)

        # 決定ボタン
        self.confirm_btn = discord.ui.Button(
            label="詳細を入力して付与",
            style=discord.ButtonStyle.primary,
            disabled=True,
            custom_id="hidden_title_confirm_btn"
        )
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def user_callback(self, interaction: discord.Interaction):
        self.selected_user = self.user_select.values[0]
        await self._update_view(interaction)

    async def rarity_callback(self, interaction: discord.Interaction):
        self.selected_rarity = self.rarity_select.values[0]
        await self._update_view(interaction)

    async def _update_view(self, interaction: discord.Interaction):
        if self.selected_user and self.selected_rarity:
            self.confirm_btn.disabled = False
        await interaction.response.edit_message(view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        if not self.selected_user or not self.selected_rarity:
            return
        modal = HiddenTitleModal(self.bot, self.selected_user, self.selected_rarity)
        await interaction.response.send_modal(modal)


# ============================================
# メンバー選択用 View (個別クエスト配信用)
# ============================================

class SendQuestSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot
        
        self.user_select = discord.ui.UserSelect(
            placeholder="クエストを追加配信するメンバーを選択",
            custom_id="send_quest_user_select"
        )
        self.user_select.callback = self.user_callback
        self.add_item(self.user_select)

    async def user_callback(self, interaction: discord.Interaction):
        target_id = int(self.user_select.values[0])
        target = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
        
        await interaction.response.defer(ephemeral=True)
        quest_cog = self.bot.get_cog("QuestCog")
        if not quest_cog:
            await interaction.followup.send("⚠️ QuestCogが読み込まれていません。", ephemeral=True)
            return
        
        sheets = self.bot.sheets
        if not sheets:
            await interaction.followup.send("⚠️ Google Sheets が未接続です。", ephemeral=True)
            return

        try:
            week_start = sheets.get_current_week_start()
            quests = await sheets.get_weekly_quests(week_start)
        except Exception as e:
            logger.error(f"❌ send_quest: データ取得エラー: {e}")
            await interaction.followup.send(f"❌ データ取得に失敗しました:\n```{e}```", ephemeral=True)
            return

        user_quests = [q for q in quests if q["discord_id"] == target_id and not q.get("sent", False)]
        
        if not user_quests:
            await interaction.followup.send(f"✅ {target.display_name} さんの未送信クエストはありません。", ephemeral=True)
            return

        user_quests = user_quests[:5]
        
        member_info = sheets.get_member(target_id)
        autonomy = member_info.get("autonomy_level", "low") if member_info else "low"

        try:
            embed = quest_cog._build_quest_embed(user_quests, autonomy, week_start, target_id)
            from cogs.quest import QuestView
            view = QuestView(self.bot)
            
            await target.send(embed=embed, view=view)
            
            for q in user_quests:
                await sheets.update_quest_sent_status(week_start, target_id, q["quest_title"], True)
                
            await interaction.followup.send(f"✅ {target.display_name} さんに {len(user_quests)}件 のクエストを追加配信しました！", ephemeral=True)
            logger.info(f"📩 追加クエスト配信: {target.name} ({len(user_quests)}件)")
            
        except discord.Forbidden:
            await interaction.followup.send(f"⚠️ {target.display_name} さんにDMを送信できませんでした。", ephemeral=True)
        except Exception as e:
            logger.error(f"❌ send_quest 送信エラー: {e}")
            await interaction.followup.send(f"❌ 送信中にエラーが発生しました:\n```{e}```", ephemeral=True)

# ============================================
# Admin Hub View
# ============================================

class AdminHubView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        # 常設なのでタイムアウトなし
        super().__init__(timeout=None)
        self.bot = bot

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⚠️ この操作は管理者専用です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🏅 隠し称号の付与", style=discord.ButtonStyle.success, custom_id="admin_hub_title", row=0)
    async def hidden_title_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        view = HiddenTitleSetupView(self.bot)
        await interaction.response.send_message("👤 **隠し称号の付与設定**\n付与するメンバーとレア度を選択してください。", view=view, ephemeral=True)

    @discord.ui.button(label="⚔️ クエスト一斉配信", style=discord.ButtonStyle.primary, custom_id="admin_hub_test_quest", row=0)
    async def test_quest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        quest_cog = self.bot.get_cog("QuestCog")
        if quest_cog:
            await interaction.response.send_message("⏳ クエスト配信を開始します...", ephemeral=True)
            await quest_cog.deliver_weekly_quests()
            await interaction.edit_original_response(content="✅ クエスト配信を実行しました。")
        else:
            await interaction.response.send_message("⚠️ QuestCogが読み込まれていません。", ephemeral=True)

    @discord.ui.button(label="📩 個別クエスト配信", style=discord.ButtonStyle.secondary, custom_id="admin_hub_send_quest", row=0)
    async def send_quest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        view = SendQuestSelectView(self.bot)
        await interaction.response.send_message("👤 **追加配信**\nクエストを追加配信するメンバーを選んでください。", view=view, ephemeral=True)

    @discord.ui.button(label="📅 サマリー投稿", style=discord.ButtonStyle.primary, custom_id="admin_hub_test_summary", row=1)
    async def test_summary_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        summary_cog = self.bot.get_cog("SummaryCog")
        if summary_cog:
            await interaction.response.send_message("⏳ サマリーを投稿します...", ephemeral=True)
            await summary_cog.post_weekly_summary()
            await interaction.edit_original_response(content="✅ サマリーを投稿しました。")
        else:
            await interaction.response.send_message("⚠️ SummaryCogが読み込まれていません。", ephemeral=True)

    @discord.ui.button(label="🔄 データ同期", style=discord.ButtonStyle.danger, custom_id="admin_hub_sync", row=1)
    async def sync_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        commands_cog = self.bot.get_cog("CommandsCog")
        if commands_cog:
            await interaction.response.send_message("⏳ データ同期（EXP・ロール）を実行中...", ephemeral=True)
            # sync_exp の呼び出し（Interactionを直接渡すと重複応答エラーになるため、内部で処理を分けるかラップするべきですが、簡単のため直接コールバック呼び出し）
            # sync_exp / sync_roles は内部で followup/response.send を呼ぶため、一括ボタンにする場合は注意が必要。
            # ここでは別々に選べる View を返す方が安全です。
            
            view = AdminSyncSelectView(self.bot)
            await interaction.edit_original_response(content="🔄 同期する項目を選択してください", view=view)
        else:
            await interaction.response.send_message("⚠️ CommandsCogが読み込まれていません。", ephemeral=True)


class AdminSyncSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="EXP同期", style=discord.ButtonStyle.primary)
    async def sync_exp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            logs = await self.bot.db.get_all_exp_logs()
        except Exception as e:
            logger.error(f"❌ EXPログ取得エラー: {e}")
            await interaction.followup.send("⚠️ ログの取得に失敗しました。", ephemeral=True)
            return

        if not logs:
            reset_count = 0
            profiles = await self.bot.db.get_all_user_profiles()
            for profile in profiles:
                uid = profile["discord_id"]
                if profile["total_exp"] > 0:
                    job_type = profile.get("job_type", "")
                    if not job_type and getattr(self.bot, "sheets", None):
                        member = self.bot.sheets.get_member(uid)
                        if member:
                            job_type = member.get("job_type", "")
                    job_name = self.bot.sheets.get_job_name(job_type) if getattr(self.bot, "sheets", None) else job_type
                    new_title = TitleSystem.get_title(1, job_name)
                    await self.bot.db.upsert_user_profile(
                        discord_id=uid, total_exp=0, level=1,
                        job_type=job_type, current_title=new_title,
                        consecutive_weeks=0,
                    )
                    reset_count += 1
            embed = discord.Embed(
                title="🔄 EXPリセット完了",
                description=f"EXPログが空のため、**{reset_count}人** のEXPを0にリセットしました。",
                color=config.COLOR_WARNING,
            )
            embed.set_footer(text="しんちょくん 🤖 | EXP同期")
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"🔄 EXPログ空 → {reset_count}人リセット")
            return

        totals: dict[int, int] = {}
        for log in logs:
            uid = log["discord_id"]
            totals[uid] = totals.get(uid, 0) + log["amount"]

        results = []
        for uid, new_exp in totals.items():
            old_profile = await self.bot.db.get_user_profile(uid)
            old_exp = old_profile["total_exp"] if old_profile else 0

            new_level = TitleSystem.calculate_level(new_exp)

            job_type = old_profile["job_type"] if old_profile else ""
            if not job_type and getattr(self.bot, "sheets", None):
                member = self.bot.sheets.get_member(uid)
                if member:
                    job_type = member.get("job_type", "")
            job_name = self.bot.sheets.get_job_name(job_type) if getattr(self.bot, "sheets", None) else job_type
            new_title = TitleSystem.get_title(new_level, job_name)

            await self.bot.db.upsert_user_profile(
                discord_id=uid, total_exp=new_exp, level=new_level,
                job_type=job_type, current_title=new_title,
                consecutive_weeks=old_profile["consecutive_weeks"] if old_profile else 0,
            )

            diff = new_exp - old_exp
            diff_str = f"+{diff}" if diff >= 0 else str(diff)
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"ID:{uid}"
            results.append(f"**{name}**: {old_exp} → {new_exp} ({diff_str})")

        embed = discord.Embed(
            title="🔄 EXP同期完了",
            description=f"EXPログ {len(logs)} 件から {len(totals)} 人分を同期しました。",
            color=config.COLOR_SUCCESS,
        )
        embed.add_field(name="📊 同期結果", value="\n".join(results) or "なし", inline=False)
        embed.set_footer(text="しんちょくん 🤖 | EXP同期")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"🔄 EXP同期完了: {len(totals)}人")

    @discord.ui.button(label="ロール同期", style=discord.ButtonStyle.primary)
    async def sync_roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("⚠️ サーバー内で実行してください。", ephemeral=True)
            return

        exp_manager = ExpManager(self.bot.db, getattr(self.bot, "sheets", None))
        member_ids = config.get_active_member_ids()
        success = 0
        errors = []

        for uid in member_ids:
            try:
                profile = await exp_manager.ensure_profile(uid, guild=guild)
                level = profile["level"]

                job_type = profile.get("job_type", "")
                if not job_type and getattr(self.bot, "sheets", None):
                    member_info = self.bot.sheets.get_member(uid)
                    if member_info:
                        job_type = member_info.get("job_type", "")
                job_name = self.bot.sheets.get_job_name(job_type) if getattr(self.bot, "sheets", None) else job_type
                title = TitleSystem.get_title(level, job_name)

                if title != profile["current_title"]:
                    logger.info(f"🔧 称号修復: ID={uid}, '{profile['current_title']}' → '{title}'")
                    await self.bot.db.upsert_user_profile(
                        discord_id=uid, total_exp=profile["total_exp"], level=level,
                        job_type=job_type, current_title=title,
                        consecutive_weeks=profile.get("consecutive_weeks", 0),
                    )

                if not title or not title.strip():
                    continue

                role = discord.utils.get(guild.roles, name=title)
                if role is None:
                    rank = TitleSystem.get_title_rank(level)
                    color = discord.Color(config.ROLE_COLORS.get(rank, 0x95A5A6))
                    role = await guild.create_role(
                        name=title, color=color,
                        reason="しんちょくん: 称号ロール一括作成",
                    )
                    logger.info(f"✨ ロール作成: {title}")

                member = guild.get_member(uid)
                if member is None:
                    member = await guild.fetch_member(uid)
                if role not in member.roles:
                    await member.add_roles(role, reason="称号ロール一括同期")

                success += 1
                user = self.bot.get_user(uid)
                logger.info(f"🏅 ロール同期: {user.display_name if user else uid} → {title}")

            except discord.NotFound:
                errors.append(f"ID:{uid} — メンバーが見つかりません")
            except discord.Forbidden:
                errors.append(f"ID:{uid} — 権限不足")
            except Exception as e:
                errors.append(f"ID:{uid} — {e}")

        embed = discord.Embed(
            title="🏅 ロール同期完了",
            description=f"**{success}/{len(member_ids)}** 人にロールを付与しました。",
            color=config.COLOR_SUCCESS if not errors else config.COLOR_WARNING,
        )
        if errors:
            embed.add_field(name="⚠️ エラー", value="\n".join(errors[:5]), inline=False)
        embed.set_footer(text="しんちょくん 🤖 | ロール同期")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"🏅 ロール同期完了: {success}/{len(member_ids)}人")


# ============================================
# Admin Hub Cog
# ============================================

class AdminHubCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup_admin_hub", description="管理者専用チャンネルに管理用ハブを設置します")
    @app_commands.default_permissions(administrator=True)
    async def setup_admin_hub(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="👑 しんちょくん 管理者センター",
            description=(
                "管理者向けの機能一覧です。ボタンを押して操作してください。\n"
                "*(このパネルは管理者のみ操作可能です)*\n\n"
                "🏅 **隠し称号の付与** — 特別な称号をメンバーに与えます\n"
                "⚔️ **クエスト一斉配信** — 今週のクエストを全員に配信します\n"
                "📩 **個別クエスト配信** — 指定メンバーにプール分を配信します\n"
                "📅 **サマリー投稿** — 週次サマリーを手動で投稿します\n"
                "🔄 **データ同期** — スプレッドシートとDBのEXP/称号を同期します"
            ),
            color=config.COLOR_DANGER,
        )
        embed.set_footer(text="しんちょくん 🤖 | 管理者センター")

        view = AdminHubView(self.bot)
        
        try:
            await interaction.channel.send(embed=embed, view=view)
            await interaction.response.send_message("✅ 管理者ハブを設置しました！", ephemeral=True)
            logger.info("✅ 管理者ハブ設置完了")
        except discord.Forbidden:
            logger.error("❌ 管理者ハブ設置失敗: チャンネルへの送信権限がありません")
            await interaction.response.send_message(
                "⚠️ **エラー**: このチャンネルにメッセージを送信する権限がありません。\n"
                "チャンネル設定 > 権限 から、しんちょくん（Bot）に「**チャンネルを見る**」および「**メッセージを送信**」の権限を付与してください。",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"❌ 管理者ハブ設置時に予期せぬエラー: {e}")
            await interaction.response.send_message(f"⚠️ 予期せぬエラーが発生しました: `{e}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminHubCog(bot))
