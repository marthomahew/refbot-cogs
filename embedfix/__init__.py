from .embedfix import EmbedFix

__red_end_user_data_statement__ = (
    "This cog stores the last 25 links each member shared through the embed fixer "
    "(per server) so `links` can list them. Red's data deletion removes them."
)


async def setup(bot):
    await bot.add_cog(EmbedFix(bot))
