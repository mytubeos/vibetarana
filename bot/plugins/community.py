"""Group listening policy and unique-member vote skip."""
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from bot.core import calls, player, preferences
from bot.core.decorators import admin_filter
from bot.core.queue import queues
from bot.core.session import operation

async def member(client, chat_id, user_id):
    try:
        m = await client.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER) or (m.status == ChatMemberStatus.RESTRICTED and m.is_member)
    except Exception:
        return False

@Client.on_message(filters.command('voteskip') & filters.group)
async def voteskip(client, message):
    if not message.from_user or not await member(client, message.chat.id, message.from_user.id):
        return
    async with operation(message.chat.id):
        state = queues.get(message.chat.id)
        pref = await preferences.get(message.chat.id)
        target = pref.get('vote_skip', 3)
        if not target:
            response = 'Vote skip is disabled.'
        elif not state.current or state.needs_resume:
            response = 'Nothing is playing.'
        else:
            # Revalidate voters so users who left cannot count toward the threshold.
            valid = set()
            for user_id in state.skip_voters:
                if await member(client, message.chat.id, user_id):
                    valid.add(user_id)
            state.skip_voters = valid | {message.from_user.id}
            votes = len(state.skip_voters)
            if votes >= target:
                state.skip_voters.clear()
                await calls._advance_and_play_unlocked(message.chat.id, force=True)
                await calls.persist_queue(message.chat.id)
                response = 'Vote passed. Skipped.'
            else:
                response = f'Vote recorded: {votes}/{target} unique group members.'
    await message.reply_text(response)
    await player.refresh(message.chat.id)

@Client.on_message(filters.command('policy') & filters.group & admin_filter)
async def policy(_, message):
    args = message.command[1:]
    try:
        if len(args) != 2:
            raise ValueError
        key, value = args
        if key == 'fair' and value in ('on', 'off'):
            fields = {'fair_queue': value == 'on'}
        elif key == 'votes' and value.isdigit() and 0 <= int(value) <= 20:
            fields = {'vote_skip': int(value)}
        elif key == 'music' and value in ('any', 'Hindi', 'English', 'Punjabi', 'Tamil', 'Telugu', 'Bengali', 'Marathi'):
            fields = {'music_language': value}
        elif key == 'video' and value in ('360', '480', '720', '1080'):
            fields = {'video_quality': value}
        else:
            raise ValueError
        async with operation(message.chat.id):
            await preferences.set(message.chat.id, **fields)
            queues.get(message.chat.id).skip_voters.clear()
            if fields.get('fair_queue'):
                queues.fair(message.chat.id)
                calls.schedule_prefetch(message.chat.id)
                await calls.persist_queue(message.chat.id)
        await message.reply_text('Policy saved. Video quality applies to the next stream.')
    except ValueError:
        await message.reply_text('/policy fair on|off · /policy votes 0..20 (0 disables) · /policy music any|Hindi|English|Punjabi|Tamil|Telugu|Bengali|Marathi · /policy video 360|480|720|1080', parse_mode=ParseMode.DISABLED)
