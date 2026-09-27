from .remindme import RemindMe

__red_end_user_data_statement__ = (
    "This cog stores reminders you set (your user ID, the channel, the message it "
    "replies to, the time and your note) until they fire. Red's data deletion removes them."
)


async def setup(bot):
    await bot.add_cog(RemindMe(bot))
