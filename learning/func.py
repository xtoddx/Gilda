import io
import os
import json
import logging
import aiohttp
import asyncio
import feedparser
import oci
import datetime
import uuid
import fdk

from fdk import response
from slack import WebClient
from slack.errors import SlackApiError

local_dev = os.environ.get('is_local')
slack_bot_token = os.environ.get('slack_bot_token')
compartment_id = os.environ.get('compartment_id')
auth_token = os.environ.get('web_auth_token')

TABLE_NAME = "learning_posts"

logger = logging.getLogger()
slack_client = WebClient(token=slack_bot_token, run_async=True)


# TODO: feed in via env config or db
learning_sites_rss = [
    {
        "url": "https://golangweekly.com/rss/1e3hc386",
        "channel": "gophers"
    },
    {
        "url": "https://pycoders.com/feed/vTty3IR4",
        "channel": "python"
    },
    {
        "url": "https://rubyweekly.com/rss/20a549il",
        "channel": "ruby"
    },
    {
        "url": "https://this-week-in-rust.org/rss.xml",
        "channel": "rust"
    },
    {
        "url": "https://javascriptweekly.com/rss/1o834e3a",
        "channel": "javascript"
    }
]


def should_post_message(oci_client, post_url):
    should_post = False

    details = oci.nosql.models.QueryDetails(
        compartment_id=compartment_id,
        statement=f"SELECT * FROM {TABLE_NAME} where url = '{post_url}'"
    )

    try:
        query_resp = oci_client.query(limit=1, query_details=details)
        if len(query_resp.data.items) == 0:
            should_post = True
    except Exception as ex:
        should_post = False
        logger.info(f"error: {str(ex)}")

    return should_post


def update_db(oci_client, post_url):
    try:
        row = oci.nosql.models.UpdateRowDetails(
            compartment_id=compartment_id,
            value={
                "uuid": str(uuid.uuid4()),
                "last_posted_at": datetime.datetime.now(),
                "url": f"{post_url}"
            })

        oci_client.update_row(TABLE_NAME, update_row_details=row)
    except Exception as ex:
        # TODO: if fail to write to DB alarm
        logger.info(f"db write error: {str(ex)}")


async def post(site, site_title, post_title, post_url):
    logger.debug(f"---------- POSTING NEW post to channel {post_url}")
    success = False

    message_block = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{site_title}*"
            }
        },
        {
            "type": "divider"
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{post_title} \n {post_url}"
            }
        },
    ]

    try:
        await slack_client.chat_postMessage(
            channel=f"#{site['channel']}",
            blocks=message_block)
        success = True
    except SlackApiError as e:
        logger.info(f"error: {e.response['error']}")

    return success


async def fetch(client, site):
    logger.debug(f"------------- fetch {site['url']} --------------")
    async with client.get(site['url']) as response:
        r = await response.text()

        d = feedparser.parse(r)
        site_title = d['feed']['title']
        post_title = d['entries'][0]['title']
        post_url = d['entries'][0]['link']

        oci_client = None
        if local_dev:
            config = oci.config.from_file("./oci_config")
            oci_client = oci.nosql.NosqlClient(config)
        else:
            signer = oci.auth.signers.get_resource_principals_signer()
            oci_client = oci.nosql.NosqlClient({}, signer=signer)

        should_post = should_post_message(oci_client, post_url)

        if should_post:
            new_message_posted = await post(site, site_title,
                                            post_title, post_url)

            if new_message_posted:
                update_db(oci_client, post_url)


async def handler(ctx, data: io.BytesIO = None):
    logger.debug("------------- Launching function --------------")

    token = None
    try:
        body = json.loads(data.getvalue())
        token = body.get("token")
    except (Exception, ValueError) as ex:
        print(str(ex))

    if token != auth_token:
        return response.Response(
            ctx, response_data=json.dumps(
                {"message": "Error"}),
            headers={"Content-Type": "application/json"}
        )

    async with aiohttp.ClientSession() as client:
        await asyncio.gather(*(fetch(client, site) for site in learning_sites_rss))

    return response.Response(
        ctx, response_data=json.dumps(
            {"message": "Success"}),
        headers={"Content-Type": "application/json"}
    )

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    fdk.handle(handler, loop=loop)
