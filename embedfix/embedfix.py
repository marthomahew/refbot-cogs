"""Repost social media links through "embed fixer" proxies.

Discord's own previews for Twitter/X, Instagram, TikTok and Reddit are often
missing or broken. Proxy sites (fxtwitter, hhinstagram, ...) serve the same post
with preview tags Discord understands. When someone posts one of these links,
we reply with the proxy version and hide the original message's embed so the
channel doesn't show two previews.

The domain -> proxy map is stored per server and editable with `!embedfix map`,
because these proxy services come and go.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.refbot.embedfix")

# Starting proxies. Each was checked against a real post on 2026-09-24.
# (rxddit.com was returning errors and instagramez.com redirects to ads, so
# neither is used.)
DEFAULT_MAP = {
    "twitter.com": "fxtwitter.com",
    "x.com": "fixupx.com",
    "instagram.com": "hhinstagram.com",
    "tiktok.com": "tnktok.com",
    "reddit.com": "vxreddit.com",
}

# Don't answer a message that's just a wall of links with a wall of links.
MAX_LINKS = 5

# Subdomains that are just "the same site" and should be dropped, so
# www.instagram.com and m.twitter.com work like instagram.com and twitter.com.
# Other subdomains are kept, e.g. vm.tiktok.com -> vm.tnktok.com.
DROP_PREFIXES = ("www.", "mobile.", "m.", "old.", "new.")

URL_RE = re.compile(r"https?://[^\s<>|]+", re.IGNORECASE)
# Parts of a message whose links we must NOT touch:
ANGLE_RE = re.compile(r"<https?://[^\s>]+>", re.IGNORECASE)  # <link> = "no preview please"
SPOILER_RE = re.compile(r"\|\|.*?\|\|", re.DOTALL)  # ||spoilers||
CODE_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)  # `code`
DOMAIN_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
# Punctuation/markdown that often sits right after a link: "look (https://x.com/a/status/1)!"
TRAILING = ".,!?;:)]}'\"*_~"


def normalize_domain(text: str) -> Optional[str]:
    """Turn "https://www.Example.com/foo" into "example.com". None if it's not a domain."""
    text = re.sub(r"^https?://", "", text.strip().lower()).split("/")[0]
    if text.startswith("www."):
        text = text[4:]
    return text if DOMAIN_RE.fullmatch(text) else None


def rewrite_link(url: str, domain_map: dict[str, str]) -> Optional[str]:
    """Return the proxy version of `url`, or None if it isn't a link we fix."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for prefix in DROP_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break

    for domain, proxy in domain_map.items():
        if host == domain:
            new_host = proxy
        elif host.endswith("." + domain):
            new_host = host[: -len(domain)] + proxy  # keep the subdomain: vm.tiktok.com -> vm.tnktok.com
        else:
            continue
        if not parts.path.strip("/"):
            return None  # a bare homepage link has nothing to embed
        # The query string is dropped on purpose: on these sites it's only
        # tracking junk (?s=20, ?igsh=..., ?utm_source=...).
        return urlunsplit(("https", new_host, parts.path, "", ""))
    return None


def find_links(content: str, domain_map: dict[str, str]) -> list[str]:
    """All fixable links in a message, already rewritten, without duplicates."""
    for pattern in (CODE_RE, SPOILER_RE, ANGLE_RE):
        content = pattern.sub(" ", content)
    fixed: list[str] = []
    for match in URL_RE.finditer(content):
        url = match.group(0).rstrip(TRAILING)
        new = rewrite_link(url, domain_map)
        if new and new not in fixed:
            fixed.append(new)
    return fixed[:MAX_LINKS]


def rewrite_content(content: str, domain_map: dict[str, str]) -> str:
    """The whole message with fixable links swapped in place (used by repost mode).

    Links in <...>, ||spoilers|| and `code` are left exactly as they were.
    """
    protected = [m.span() for pattern in (CODE_RE, SPOILER_RE, ANGLE_RE) for m in pattern.finditer(content)]

    def fix(match: re.Match) -> str:
        raw = match.group(0)
        if any(start <= match.start() < end for start, end in protected):
            return raw
        url = raw.rstrip(TRAILING)
        new = rewrite_link(url, domain_map)
        return new + raw[len(url):] if new else raw  # keep any trailing punctuation

    return URL_RE.sub(fix, content)


class EmbedFix(commands.Cog):
    """Fix Twitter/X, Instagram, TikTok and Reddit embeds."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D2, force_registration=True)
        self.config.register_guild(
            enabled=True,
            # Stored as a list of [domain, proxy] pairs rather than a dict on purpose:
            # Red merges dict defaults back into saved dicts, so `unmap` of a
            # default domain wouldn't stick. Lists are never merged.
            proxies=[[domain, proxy] for domain, proxy in DEFAULT_MAP.items()],
            ignored_channels=[],
            # "repost": delete the message and repost it as the author with fixed links.
            # "reply": leave the message, hide its preview, reply with the fixed links.
            mode="repost",
        )
        self._warned: set[str] = set()  # one-time log warnings already sent
        self._webhooks: dict[int, discord.Webhook] = {}  # channel id -> our webhook there
        self._webhook_lock = asyncio.Lock()  # so two quick messages don't create two webhooks

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # This cog stores no data about users, so there's nothing to delete.
        return

    async def _get_map(self, guild: discord.Guild) -> dict[str, str]:
        return {domain: proxy for domain, proxy in await self.config.guild(guild).proxies()}

    async def _set_map(self, guild: discord.Guild, domain_map: dict[str, str]) -> None:
        await self.config.guild(guild).proxies.set([[d, p] for d, p in domain_map.items()])

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    # ------------------------------------------------------------ repost mode

    async def _get_webhook(self, channel: discord.abc.GuildChannel) -> Optional[discord.Webhook]:
        """Find (or make) this bot's webhook in a channel. Webhooks are what let a
        message show someone else's name and avatar."""
        async with self._webhook_lock:
            if channel.id in self._webhooks:
                return self._webhooks[channel.id]
            try:
                hooks = await channel.webhooks()
                hook = next((h for h in hooks if h.user and h.user.id == self.bot.user.id and h.token), None)
                if hook is None:
                    hook = await channel.create_webhook(name="Refbot embedfix", reason="embedfix: repost fixed links")
            except discord.HTTPException as e:
                log.warning("Couldn't get a webhook in #%s: %r", channel, e)
                return None
            self._webhooks[channel.id] = hook
            return hook

    async def _try_repost(self, message: discord.Message, domain_map: dict[str, str]) -> bool:
        """Delete the message and repost it as its author with fixed links.

        Returns False (without touching anything) when a repost would lose
        something or isn't possible; the caller then uses reply mode instead.
        """
        channel = message.channel
        me = message.guild.me

        # A webhook repost can't carry these over, so reply instead.
        if message.attachments or message.stickers or message.reference or getattr(message, "poll", None):
            return False

        # Webhooks live on the channel; in a thread we use the parent's webhook.
        if isinstance(channel, discord.Thread):
            if message.id == channel.id:
                return False  # opening post of a thread/forum post: deleting it deletes the thread
            parent = channel.parent
        else:
            parent = channel
        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            return False  # e.g. voice channel chat

        if not (channel.permissions_for(me).manage_messages and parent.permissions_for(me).manage_webhooks):
            self._warn_once(
                f"repost-perms-{message.guild.id}",
                "Repost mode needs Manage Messages and Manage Webhooks (guild %s); replying instead",
                message.guild.id,
            )
            return False

        content = rewrite_content(message.content, domain_map)
        if content == message.content or len(content) > 2000:
            return False

        webhook = await self._get_webhook(parent)
        if webhook is None:
            return False

        send_kwargs = {
            "content": content,
            "username": message.author.display_name[:80],  # server nickname if they have one
            "avatar_url": message.author.display_avatar.url,
            # The original message already pinged anyone it mentioned; don't ping twice.
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if isinstance(channel, discord.Thread):
            send_kwargs["thread"] = channel

        # Post the new copy first, and only delete the original once that worked,
        # so a failure never makes someone's message disappear.
        try:
            await webhook.send(**send_kwargs)
        except discord.NotFound:
            self._webhooks.pop(parent.id, None)  # someone deleted our webhook; make a new one next time
            return False
        except discord.HTTPException as e:
            log.warning("Webhook repost failed in #%s: %r", channel, e)
            return False

        try:
            await message.delete()
        except discord.HTTPException as e:
            log.warning("Reposted but couldn't delete the original %s: %r", message.jump_url, e)
        return True

    # ------------------------------------------------------------ listener

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        # Cheap checks first; this runs for every message on the server.
        if message.guild is None or message.author.bot or "http" not in message.content:
            return
        # Respect Red's own settings: cog disabled here, ignored channels, blocklists.
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        if not await self.bot.ignored_channel_or_guild(message):
            return
        if not await self.bot.allowed_by_whitelist_blacklist(message.author):
            return

        conf = await self.config.guild(message.guild).all()
        if not conf["enabled"]:
            return
        channel = message.channel
        # Ignoring a channel also covers threads inside it.
        if channel.id in conf["ignored_channels"] or getattr(channel, "parent_id", None) in conf["ignored_channels"]:
            return

        domain_map = {domain: proxy for domain, proxy in conf["proxies"]}
        links = find_links(message.content, domain_map)
        if not links:
            return

        if conf["mode"] == "repost" and await self._try_repost(message, domain_map):
            return

        # Reply mode (also the fallback when a repost isn't possible).
        perms = channel.permissions_for(message.guild.me)
        can_send = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
        # Replying to a message also needs Read Message History.
        if not (can_send and perms.embed_links and perms.read_message_history):
            self._warn_once(
                f"send-{channel.id}",
                "Missing Send Messages/Embed Links/Read Message History in #%s (%s); skipping",
                channel,
                channel.id,
            )
            return

        try:
            await message.reply(
                "\n".join(links),
                mention_author=False,  # don't ping the person who posted
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't reply with fixed links in #%s: %r", channel, e)
            return

        # Hide the original's (broken or duplicate) embed. Needs Manage Messages;
        # without it we still posted the fixed link, so just carry on.
        if perms.manage_messages:
            try:
                await message.edit(suppress=True)
            except discord.HTTPException as e:
                log.debug("Couldn't suppress embed on %s: %r", message.jump_url, e)
        else:
            self._warn_once(
                f"manage-{message.guild.id}",
                "No Manage Messages permission in guild %s; original embeds won't be hidden",
                message.guild.id,
            )

    # ------------------------------------------------------------ commands

    @commands.hybrid_group(name="embedfix")
    @commands.guild_only()
    async def embedfix(self, ctx: commands.Context):
        """Fix social media embeds."""

    @embedfix.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_toggle(self, ctx: commands.Context):
        """Turn link fixing on or off for this server."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        await conf.enabled.set(enabled)
        await ctx.send(f"Embed fixing is now **{'on' if enabled else 'off'}**.")

    @embedfix.command(name="mode")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_mode(self, ctx: commands.Context, mode: Literal["repost", "reply"]):
        """Choose how fixed links are posted: `repost` or `reply`.

        repost: delete the message and repost it under the author's name with fixed links.
        reply: keep the message, hide its preview, and reply with the fixed links.
        Messages with attachments, replies, or stickers always use reply.
        """
        await self.config.guild(ctx.guild).mode.set(mode)
        if mode == "repost":
            text = "Messages with broken links will be reposted under the author's name with fixed links."
        else:
            text = "The bot will reply with fixed links and hide the original preview."
        await ctx.send(f"Mode set to **{mode}**. {text}")

    @embedfix.command(name="map")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_map(self, ctx: commands.Context, domain: str, proxy: str):
        """Send links for a site through a proxy, e.g. `map instagram.com hhinstagram.com`.

        Also used to swap a proxy that stopped working.
        """
        domain, proxy = normalize_domain(domain), normalize_domain(proxy)
        if not domain or not proxy:
            await ctx.send("Both need to be plain domains, like `instagram.com` and `hhinstagram.com`.")
            return
        if domain == proxy:
            await ctx.send("The site and the proxy can't be the same domain.")
            return
        domain_map = await self._get_map(ctx.guild)
        old = domain_map.get(domain)
        domain_map[domain] = proxy
        await self._set_map(ctx.guild, domain_map)
        was = f" (was `{old}`)" if old and old != proxy else ""
        await ctx.send(f"Links to `{domain}` will now go through `{proxy}`{was}.")

    @embedfix.command(name="unmap")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_unmap(self, ctx: commands.Context, domain: str):
        """Stop fixing links for a site."""
        domain = normalize_domain(domain) or domain
        domain_map = await self._get_map(ctx.guild)
        if domain not in domain_map:
            await ctx.send(f"`{domain}` isn't in the list. See `{ctx.clean_prefix}embedfix list`.")
            return
        del domain_map[domain]
        await self._set_map(ctx.guild, domain_map)
        await ctx.send(f"Links to `{domain}` will no longer be fixed.")

    @embedfix.command(name="list")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_list(self, ctx: commands.Context):
        """Show the proxy list, ignored channels and status."""
        conf = await self.config.guild(ctx.guild).all()
        domain_map = await self._get_map(ctx.guild)

        lines = [
            f"**Status:** {'on' if conf['enabled'] else 'off'}",
            f"**Mode:** {conf['mode']}",
            "",
            "**Proxies:**",
        ]
        if domain_map:
            lines += [f"`{d}` → `{p}`" for d, p in sorted(domain_map.items())]
        else:
            lines.append("none")

        ignored = [ctx.guild.get_channel(cid) for cid in conf["ignored_channels"]]
        ignored_text = ", ".join(c.mention for c in ignored if c) or "none"
        lines += ["", f"**Ignored channels:** {ignored_text}"]

        perms = ctx.guild.me.guild_permissions
        if not perms.manage_messages:
            lines += ["", "⚠️ I don't have **Manage Messages**, so I can't hide or repost messages."]
        if conf["mode"] == "repost" and not perms.manage_webhooks:
            lines += ["", "⚠️ Repost mode needs **Manage Webhooks**; until then I'll reply instead."]
        await ctx.send("\n".join(lines))

    @embedfix.command(name="ignore")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_ignore(self, ctx: commands.Context, channel: discord.TextChannel):
        """Don't fix links in a channel (or its threads)."""
        async with self.config.guild(ctx.guild).ignored_channels() as ignored:
            if channel.id in ignored:
                await ctx.send(f"{channel.mention} is already ignored.")
                return
            ignored.append(channel.id)
        await ctx.send(f"Links in {channel.mention} will be left alone.")

    @embedfix.command(name="unignore")
    @commands.admin_or_permissions(manage_guild=True)
    async def ef_unignore(self, ctx: commands.Context, channel: discord.TextChannel):
        """Start fixing links in a channel again."""
        async with self.config.guild(ctx.guild).ignored_channels() as ignored:
            if channel.id not in ignored:
                await ctx.send(f"{channel.mention} isn't ignored.")
                return
            ignored.remove(channel.id)
        await ctx.send(f"Links in {channel.mention} will be fixed again.")
