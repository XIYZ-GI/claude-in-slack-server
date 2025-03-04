import contextlib
import os

import emoji
import httpx

import asyncio
import uuid
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request, Response, status, HTTPException
from starlette.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from sse_starlette.sse import EventSourceResponse

from model import ConversationResponse, ConversationRequest, Message, Content, Author
from secure import encrypt_token, decrypt_token

import html

message_mappings = {}

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CLIENT_ID = os.environ["SLACK_OAUTH_CLIENT_ID"]
SLACK_CLIENT_SECRET = os.environ["SLACK_OAUTH_CLIENT_SECRET"]
SLACK_REDIRECT_URI = os.environ["SLACK_OAUTH_REDIRECT_URI"]

APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = os.environ.get("APP_PORT", "3000")

SSE_MSG_DONE = { 'event': 'data', 'data': '[DONE]'}
SSE_MSG_PING = { 'event': 'ping', 'data': ''}

TIMEOUT_SECONDS = 30

async_client = httpx.AsyncClient()
fastapi_app = FastAPI()
slack_app = AsyncApp(token=SLACK_BOT_TOKEN)

templates = Jinja2Templates(directory="template")


@fastapi_app.get("/login")
async def login():
    return RedirectResponse(url=f"https://slack.com/oauth/v2/authorize?"
                                f"user_scope=chat:write"
                                f"&scope=chat:write,users:read,channels:history"
                                f"&client_id={SLACK_CLIENT_ID}"
                                f"&redirect_uri={quote(SLACK_REDIRECT_URI)}")


@fastapi_app.get("/callback")
async def callback(request: Request, code: str = None, error: str = None):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    response = await async_client.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id": SLACK_CLIENT_ID,
            "client_secret": SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": SLACK_REDIRECT_URI
        },
    )
    body = response.json()
    return (
        templates.TemplateResponse(
            "success.html",
            {
                "request": request,
                "access_token": encrypt_token(body['authed_user']['access_token']),
            },
        )
        if body['ok']
        else templates.TemplateResponse(
            "error.html", {"request": request, "error_msg": body['error']}
        )
    )


@slack_app.command("/hello-socket-mode")
async def hello_command(ack, body):
    user_id = body["user_id"]
    ack(f"Hi, <@{user_id}>!")


@slack_app.event("message")
async def event_message(say, event, message):
    if message.get('subtype') == 'message_changed':
        user_id = message['message'].get('parent_user_id')
        user_ts = message['message'].get('thread_ts')
        if not user_id or not user_ts:
            return
        text = message['message']['text']
        if queue := message_mappings.get(f"{user_id}-{user_ts}"):
            await queue.put(text)
            await queue.join()


@fastapi_app.get('/backend-api/revoke')
async def revoke(request: Request, response: Response):
    # Perform some processing on the request data
    channel, access_token = request.headers \
        .get('Authorization', '@') \
        .removeprefix('Bearer ') \
        .split('@', 1)
    if not access_token:
        response.status_code = status.HTTP_403_FORBIDDEN
        return ConversationResponse(error="You need to provide CHANNEL_ID@ACCESS_TOKEN in Authorization header.")

    try:
        access_token = decrypt_token(access_token)
    except ValueError:
        response.status_code = status.HTTP_403_FORBIDDEN
        return ConversationResponse(error="Invalid ACCESS_TOKEN.")

    resp = await async_client.get(url="https://slack.com/api/auth.revoke", headers={
            'Authorization': f'Bearer {access_token}'
    })
    return resp.json()


@fastapi_app.get('/backend-api/conversations')
async def conversations():
    return {
        "items": []
    }


@fastapi_app.post('/backend-api/conversation', response_model=ConversationResponse)
async def conversation(request_data: ConversationRequest, request: Request, response: Response):
    # Perform some processing on the request data
    channel, access_token = request.headers \
        .get('Authorization', '@') \
        .removeprefix('Bearer ') \
        .split('@', 1)
    if not access_token:
        response.status_code = status.HTTP_403_FORBIDDEN
        return ConversationResponse(error="You need to provide CHANNEL_ID@ACCESS_TOKEN in Authorization header.")

    try:
        access_token = decrypt_token(access_token)
    except ValueError:
        response.status_code = status.HTTP_403_FORBIDDEN
        return ConversationResponse(error="Invalid ACCESS_TOKEN.")
    prompt = ''.join(request_data.messages[0].content.parts)

    if ':' in channel:
        channel, bot_id = channel.split(':', 1)
    else:
        bot_id = 'claude'

    payload = {
        'text': f'<@{bot_id}> {prompt}',
        'channel': channel,
        "thread_ts": request_data.conversation_id,
        "link_names": "true"
    }

    resp = await async_client.post(url="https://slack.com/api/chat.postMessage", headers={
            'Authorization': f'Bearer {access_token}'
    }, data=payload)
    body = resp.json()
    if error := body.get('error'):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ConversationResponse(error=error)

    user_id = body["message"]["user"]
    user_ts = request_data.conversation_id or body["message"]["ts"]
    
    key = f"{user_id}-{user_ts}"
    if key not in message_mappings:
        message_mappings[key] = asyncio.Queue()

    queue: asyncio.Queue = message_mappings[key]

    async def sse_emitter():
        try:
            yield SSE_MSG_PING
            while True:
                if await request.is_disconnected():
                    del message_mappings[key]
                    return
                message = await asyncio.wait_for(queue.get(), TIMEOUT_SECONDS)
                message = message.strip()
                message = emoji.emojize(message, variant="emoji_type", language='alias')
                message = html.unescape(message)
                yield {
                    'event': 'data',
                    'data': ConversationResponse(
                        message=Message(
                            id=str(uuid.uuid4()),
                            role="assistant",
                            content=Content(content_type="text", parts=[message.removesuffix('\n\n_Typing…_')]),
                            author=Author(role="assistant"), ),
                        conversation_id=user_ts,
                        error=None,
                    ).json()
                }
                queue.task_done()
                if not (message.endswith(' ...') or message.endswith('hourglass_flowing_sand:')):
                    return
        except asyncio.exceptions.TimeoutError:
            print(f"Key {key} has been forced-teriminated due to timeout over {TIMEOUT_SECONDS} seconds.")
        finally:
            yield SSE_MSG_DONE
            with contextlib.suppress(KeyError):
                if queue.empty():
                    del message_mappings[key]
    return EventSourceResponse(sse_emitter())


@fastapi_app.on_event("startup")
async def startup_event():
    await AsyncSocketModeHandler(slack_app, os.environ["SLACK_APP_TOKEN"]).connect_async()


if __name__ == "__main__":
    uvicorn.run(fastapi_app, host="0.0.0.0", port=int(APP_PORT), reload=False)
