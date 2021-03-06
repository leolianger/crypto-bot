import functools
import json
import threading
import traceback
from threading import Thread
import time
from time import sleep

from binance.client import Client

import asyncio
import websockets
import websockets.exceptions

import signal
import os
import os.path

from retrying import retry
from websockets import ConnectionClosed, InvalidStatusCode

from Utils.Logger import Logger


class BinanceWebsocket(Thread, Logger):
    REFRESH_KEY_TIMEOUT = 30 * 60
    WS_URL = 'wss://stream.binance.com:9443/'
    __EVENT_LOOP = None

    def __init__(self, client: Client):
        Thread.__init__(self)
        Logger.__init__(self)


        self.client = client
        self.stop = False

        self.ticker_websocket = None
        self.user_webscoket = None

        self.ticker_ws_future = None
        self.user_ws_future = None
        self.mngmt_future = None

        self.connection_key = None
        self.user_info_cb = None

        self.ticker_cb = None
        self.symbols = None

        if not BinanceWebsocket.__EVENT_LOOP:
            self.loop = asyncio.get_event_loop()
            BinanceWebsocket.__EVENT_LOOP = self.loop
        else:
            self.loop = BinanceWebsocket.__EVENT_LOOP

        self.time = None

        self.name = 'Binance WebSocket Thread'


    @retry(
        stop_max_attempt_number=3,
        wait_fixed=1000
        # retry_on_exception=BinanceWebsocket.check_exception
    )
    def run(self):
        try:
            asyncio.set_event_loop(self.loop)
            self.start_management_loop()

            # if not asyncio.get_event_loop().is_running():
            asyncio.get_event_loop().run_forever()
        finally:
            # self.loop.close()
            pass

    def start_management_loop(self):
        self.mngmt_future = asyncio.ensure_future(self.management_loop())
        self.mngmt_future.add_done_callback(
            functools.partial(self.feature_finished, reconnect_fn=self.start_management_loop, name='management loop'))

    @asyncio.coroutine
    async def management_loop(self):
        while True:
            if self.stop:
                return

            if self.time and (time.time() - self.time) > BinanceWebsocket.REFRESH_KEY_TIMEOUT:
                self.start_user_info()

            await asyncio.sleep(60)

    def start_ticker(self, symbols=None, callback=None):
        if symbols:
            self.symbols = symbols

        if callback:
            self.ticker_cb = callback

        url = os.path.join(BinanceWebsocket.WS_URL,
                           'stream?streams=' + '/'.join([s.lower() + '@ticker' for s in self.symbols]))
        self.ticker_ws_future = asyncio.run_coroutine_threadsafe(self.websocket_handler(url, self.ticker_cb), self.loop)

        self.ticker_ws_future.add_done_callback(
            functools.partial(self.feature_finished, reconnect_fn=self.start_ticker, name='ticker websocket'))

    def start_user_info(self, callback=None):
        self.time = time.time()

        if callback:
            self.user_info_cb = callback

        get_key = asyncio.run_coroutine_threadsafe(self.refresh_listen_key(), self.loop)
        get_key.add_done_callback(self.listen_key_received)

    def feature_finished(self, future: asyncio.Future, reconnect_fn=None, name=''):
        self.logInfo('Feature finished: "{}"'.format(name))

        if future.cancelled():
            return

        exc = future.exception()
        if exc:
            if (isinstance(exc, ConnectionClosed) and exc.code > 1002) or isinstance(exc, InvalidStatusCode):
                self.logError(exc)

                if reconnect_fn and not self.stop:
                    self.logInfo('Trying to reconnect...')
                    sleep(1)
                    reconnect_fn()
                else:
                    self.logInfo('No reconnection function...')
                return
            else:
                self.logError(exc)
        # try:
        #     self.logInfo('Feature finished: "{}"'.format(name))
        #
        #     # 2018 - 05 - 03
        #     # 02: 14:01, 464[INFO][BinanceWebsocket | Binance
        #     # WebSocket
        #     # Thread]: Feature
        #     # finished: < Task
        #     # finished
        #     # coro = < BinanceWebsocket.websocket_handler()
        #     # done, defined
        #     # at / usr / src / app / Bot / Exchange / Binance / BinanceWebsocket.py: 137 > exception = ConnectionClosed(
        #     #     'WebSocket connection is closed: code = 1006 (connection closed abnormally [internal]), no reason', ) >
        # except Exception as e:
        #     self.logError(traceback.format_exc())

    @asyncio.coroutine
    async def refresh_listen_key(self):
        return self.client.stream_get_listen_key()

    def listen_key_received(self, future):
        key = future.result()
        create_user_ws = False

        if key != self.connection_key or not self.user_ws_future or \
                self.user_ws_future.cancelled() or self.user_ws_future.done():
            create_user_ws = True

        if create_user_ws:
            self.stop_user_future()
            self.user_ws_future = asyncio.ensure_future(
                self.websocket_handler(os.path.join(BinanceWebsocket.WS_URL, 'ws', key), self.user_info_cb))

            # self.user_ws_future.add_done_callback(self.feature_finished)
            self.user_ws_future.add_done_callback(
                functools.partial(self.feature_finished, reconnect_fn=self.start_user_info, name='user websocket'))


        self.connection_key = key

    async def manage_user_ws_loop(self):
        return self.refresh_listen_key()

    def stop_user_future(self):
        if self.user_ws_future:
            self.user_ws_future.cancel()
            self.user_ws_future = None

    def stop_ticker_future(self):
        if self.ticker_ws_future:
            self.ticker_ws_future.cancel()
            self.ticker_ws_future = None

    @asyncio.coroutine
    async def websocket_handler(self, url, callback):
        if self.stop:
            return

        async with websockets.connect(url, timeout=1) as websocket:
            self.ticker_websocket = websocket
            self.logInfo('Websocket Connected to "{}"'.format(url))

            async for message in websocket:
                if self.stop:
                    return

                if callback:
                    callback(json.loads(message))

    def stop_sockets(self):
        self.stop = True

        self.stop_ticker_future()
        self.stop_user_future()

        if self.mngmt_future:
            self.mngmt_future.cancel()

        self.loop.call_soon_threadsafe(self.loop.stop)

        if threading.current_thread().ident != self.ident:
            self.join(timeout=1)

        self.logInfo('Stopped')
