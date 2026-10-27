"""Launch privately; group identity is only a hint and is verified by the API."""
from urllib.parse import urlsplit, urlencode
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import settings

async def launch(client, message, chat_id=''):
    url = settings.webapp_url
    if not url or urlsplit(url).scheme != 'https':
        await message.reply_text('Mini App is not configured. Set WEBAPP_URL to your HTTPS /app endpoint.')
        return
    if message.chat.type != ChatType.PRIVATE:
        me = await client.get_me()
        button = InlineKeyboardButton('Open music room', url=f'https://t.me/{me.username}?start=app_{message.chat.id}')
    else:
        button = InlineKeyboardButton('Open Vibetarana', web_app=WebAppInfo(url=url + ('&' if '?' in url else '?') + urlencode({'chat': chat_id})))
    await message.reply_text('Your music room: queue, saved playlists and playback controls.', reply_markup=InlineKeyboardMarkup([[button]]))

@Client.on_message(filters.command('app'))
async def app_cmd(client, message):
    await launch(client, message, message.command[1] if len(message.command) > 1 else '')
