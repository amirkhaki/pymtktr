import base64
import os
import hashlib
from quart import Quart, request, jsonify
import aiohttp
from telethon import TelegramClient, utils, functions, errors, events
from hypercorn.config import Config
from hypercorn.asyncio import serve


def get_env(name, message):
    if name in os.environ:
        return os.environ[name]
    return input(message)


# Session name, API ID and hash to use; loaded from environmental variables
SESSION = os.environ.get('MTK_PHONE_SESSION', 'phone')
BOT_SESSION = os.environ.get('MTK_BOT_SESSION', 'bot')
BOT_TOKEN = get_env('MTK_BOT_TOKEN', 'Enter bot token: ')
API_ID = int(get_env('MTK_APP_ID', 'Enter your API ID: '))
API_HASH = get_env('MTK_APP_HASH', 'Enter your API hash: ')
DJ_URL = os.environ.get('MTK_DJ_URL', 'http://127.0.0.1:8000')


# Helper method to add the HTML head/body
def html(inner):
    return '''
<!DOCTYPE html>
<html>
    <head>
        <meta charset="UTF-8">
        <title>Telethon + Quart</title>
    </head>
    <body>{}</body>
</html>
'''.format(inner)


# Helper method to format messages nicely
async def format_message(message):
    if message.photo:
        content = '<img src="data:image/png;base64,{}" alt="{}" />'.format(
                base64.b64encode(await message.download_media(bytes)).decode(),
                message.raw_text
                )
    else:
        # client.parse_mode = 'html', so bold etc. will work!
        content = (message.text or '(action message)').replace('\n', '<br>')

    return '<p><strong>{}</strong>: {}<sub>{}</sub></p>'.format(
            utils.get_display_name(message.sender),
            content,
            message.date
            )


# Define the global phone and Quart app variables
phone = None
app = Quart(__name__)

async def connect_client():
    if not client.is_connected():
        await client.connect()


@app.route('/ischannelmember', methods=['GET'])
async def is_channel_member():
    await connect_client()
    if not await client.is_user_authorized():
        return 'error'
    ch = request.args.get('ch')
    user = request.args.get('u')
    try:
        _ = await client(functions.channels.GetParticipantRequest(
        channel=ch,
        participant=user
        ))
        result['ismember'] = True
    except errors.UserNotParticipantError:
        result['ismember'] = False
    return jsonify(result)

@app.route('/doesusernameexists', methods=['GET'])
async def does_username_exists():
    await connect_client()
    user = request.args.get('u')
    result = {}
    try:
        u = await client.get_entity(user)
        if isinstance(u, types.User):
            result['exists'] = True
        else:
            result['exists'] = False
    except ValueError:
        result['exists'] = False

    return jsonify(result)



# Quart handlers
@app.route('/', methods=['GET', 'POST'])
async def session():
    # Connect if we aren't yet
    await connect_client()
    # We want to update the global phone variable to remember it
    global phone

    # Check form parameters (phone/code)
    form = await request.form
    if 'phone' in form:
        phone = form['phone']
        await client.send_code_request(phone)

    if 'code' in form:
        await client.sign_in(code=form['code'])

    # If we're logged in, show them some messages from their first dialog
    if await client.is_user_authorized():
        # They are logged in, show them some messages from their first dialog
        result = 'you are logged in'
        return html(result)

    # Ask for the phone if we don't know it yet
    if phone is None:
        return html('''
<form action="/" method="post">
    Phone (international format): <input name="phone" type="text" placeholder="+34600000000">
    <input type="submit">
</form>''')

    # We have the phone, but we're not logged in, so ask for the code
    return html('''
<form action="/" method="post">
    Telegram code: <input name="code" type="text" placeholder="70707">
    <input type="submit">
</form>''')


# By default, `Quart.run` uses `asyncio.run()`, which creates a new asyncio
# event loop. If we create the `TelegramClient` before, `telethon` will
# use `asyncio.get_event_loop()`, which is the implicit loop in the main
# thread. These two loops are different, and it won't work.
#
# So, we have to manually pass the same `loop` to both applications to
# make 100% sure it works and to avoid headaches.
#
# Quart doesn't seem to offer a way to run inside `async def`
# (see https://gitlab.com/pgjones/quart/issues/146) so we must
# run and block on it last.
#
# This example creates a global client outside of Quart handlers.
# If you create the client inside the handlers (common case), you
# won't have to worry about any of this.
client = TelegramClient(SESSION, API_ID, API_HASH)
client.parse_mode = 'html'  # <- render things nicely

bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
#bot.loop = client.loop
bot.start(bot_token=BOT_TOKEN)
@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    start_text = event.text.replace('/start', '').strip().split('_')
    tid = start_text[0]
    token = start_text[1]
    sender = await event.get_sender()
    async with aiohttp.ClientSession() as session:
        url = f'{DJ_URL}/tasks/accounts/telegram/{tid}/verify/{token}/'
        data = {'tid':str(sender.id), 'username':sender.username}
        async with session.post(url, json=data) as response:
            if response.status == 404:
                await event.reply('invalid link')
                raise events.StopPropagation()
            rsp = await response.json()
            if rsp['verified']:
                await event.reply('verified')
                raise events.StopPropagation()

def hash_user(uid):
    return hashlib.sha256(str(uid).encode('utf-8')).hexdigest()[:12]
@bot.on(events.ChatAction())
async def joined(event):
    if event.user_joined:
        await event.reply("Send your invite code (if any | reply to this):\n"+hash_user(event.user.id))
    if event.user_added:
        await event.reply(event.user.stringify())
    await event.reply(event.stringify())
@bot.on(events.NewMessage(incoming=True))
async def reply_invite(event):
    if not event.is_reply:
        return
    msg = await event.get_reply_message()

    sender = await event.get_sender()
    if not hash_user(sender.id) in msg.text or not (await msg.get_sender()).is_self:
        return
    async with aiohttp.ClientSession() as session:
        url = f'{DJ_URL}/tasks/telegram/invite/'
        data = {'code': event.text}
        async with session.post(url, json=data) as response:
            if response.status == 404:
                await event.reply('invalid code')
                raise events.StopPropagation()
            rsp = await response.json()
            if rsp['done']:
                await event.reply('done')
                raise events.StopPropagation()
            await event.reply(rsp['error'])
            
    
if __name__ == '__main__':
    config = Config()
    config.bind = ['unix:/run/pymtktr.sock']
    client.loop.run_until_complete(serve(app, config))
