#!/usr/bin/env python3

# --- Simple command line interface for Feishin, displays current song
#     and provides basic single key press controls like play/pause
#     next/previous and volume control. Press '?' for all options.
#
#     This script was created with a multi-paned terminal window in mind,
#     in which this script resides in one of the window's panes.
#
# --- Created for Feishin version 1.15.1 on Arch Linux.
#     Before use:
#        - install python dependencies, see requirements.txt or
#          use `pip install .` or if you prefer system wide installation,
#          install: `python-websockets python-prompt_toolkit python-pyfiglet`
#        - enable the remote control server in Feishin.
#        - provide this script with credentials, see below.
#        - possibly update 'feishin_path' to match your system.
#        - update the key binds to your preference, see below.
#        - if everything works, change 'printlevel' below to 'quiet'.
#        - if it doesn't try setting it to debug instead.

import base64, json
import websockets, asyncio
import os, subprocess
import pprint, logging, sys
import shutil, hashlib
from pyfiglet import Figlet, FigletFont, FontNotFound

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings


# --- credentials for remote control server, get them from:
#     Feishin -> settings -> window -> remote ( as of version 1.15.1 )
hostname = 'ws://localhost:4333'
username = 'feishin'
password = 'MxHVd6F4'

# --- Feishin application path, used to attempt Feishin if not already running.
feishin_path = '/usr/bin/feishin' # Windows users use: double backslashes: c:\\us...

# --- silence Feishin output.
feishin_output_to_null = True

# --- keybindings used to control Feishin from the terminal window.
#     below defaults are vim like, but arrows are supported as well:
#     e.g. 'previous' : 'left', see the prompt-toolkit docs for all options:
#     https://python-prompt-toolkit.readthedocs.io/en/master/pages/advanced_topics/key_bindings.html
simple_controller_actions = {
        'previous'        : 'k',
        'next'            : 'j',
        'step_forward'    : 'l',
        'step_back'       : 'h',
        'play_pause'      : 'p',
        'toggle_favorite' : 'f',
        'rotate_font'     : 'r',
        'toggle_term_font': 'R',
        'volume_up'       : '+',
        'volume_down'     : '-',
        'restart_song'    : '0',
        'dump_status'     : 'd',
        'print_status'    : 's',
        }
other_actions = {
        'quit'            : 'q',
        'help'            : '?',
        }

# --- printlevel options: debug normal quiet
printlevel = 'normal'
logger = logging.getLogger()

# --- Customisation options, by default the values are set for max. compatibility.
#     But a font capable of displaying symbols is assumed.

# symbols
SYMBOL_FAVORITE  = '❤'              # for favorite songs.
SYMBOL_VOLUME    = '🔊'             # used in status print and title bar (if enabled)

# colors for favorite and display of the current song.
COlOR_FAVORITE   = ''               # terminal default
COLOR_FAVORITE   = '\033[38;5;196m' # red
COLOR_FAVORITE   = '\033[38;5;199m' # purple

COLOR_TEXT       = ''               # terminal default
# COLOR_TEXT       = '\033[38;5;202m' # dark orange.

# If using a color, make sure to reset it after printing.
COLOR_RESET      = '\033[0m'

# Set window/pane title to: 'Feishin paused  🔊 35'
# might not always be safe and it won't work on all platforms / terminals / configurations
# check / test implementation in function: _set_window_title
SET_TITLE_BAR = True


# --- start Feishin
def start_feishin():
    try:
        if feishin_output_to_null:
            subprocess.Popen(feishin_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(feishin_path)
    except FileNotFoundError as e:
        logging.error('Unable to launch Feishin, is it installed?')
        logging.error(f"And 'feishin_path' (around line 39 in this script) set correctly?\n{e}")
        exit(1)

# --- shutdown helpers
shutdown_event = asyncio.Event()
prompt_task = None

def setup_logging():
    # --- firstly get everything, setup filters with handlers below.
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(levelname)s: %(message)s')

    # --- set websockets log level to warning, no matter the
    #     chosen printlevel (we are not debugging websockets)
    wslogger = logging.getLogger('websockets')
    wslogger.setLevel(logging.WARNING)
    # wslogger.propagate = False

    # --- Stdout Handler default: INFO
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(lambda f: f.levelno < logging.WARNING)
    stdout_handler.setFormatter(formatter)

    # --- Stderr Handler default: WARNING+
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)

    # --- change defaults
    match printlevel:
        case "quiet":
            stdout_handler.setLevel(logging.ERROR)
        case "debug":
            stdout_handler.setLevel(logging.DEBUG)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)


# --- class to control the Feishin remote controller.
class FeishinRemote:
    def __init__(self, url, username='', password=''):
        self.url = url
        self.username = username
        self.password = password
        self.ws = None # websocket to hold connection to Feishin remote control server.
        self.song = {}
        self.song_position = 0
        self.status = 'Unknown' # values: paused playing
        self.volume = 25
        # --- Figlet fonts used to print: artist - song
        #     the first font should be 'term'.
        #     any other change is permitted, get full list with: FigletFont.getFonts()
        font_sizes = {
                '1line' : ('term', 'circle') ,
                '2line' : ('smbraille', 'twopoint') ,
                '3line' : ('short', 'eftipiti', 'digital', 'threepoint', 'pagga',
                           'calvin_s', 'future', 'double_blocky') ,
                '4line' : ('straight', 'pepper', 'contessa', 'smblock', 'smshadow',
                           'wavy', 'cybermedium', '4max') ,
                '5line' : ('elite', 'smslant', 'smscript', 'small', 'small_slant',
                           'thick', 'chunky', 'braced') ,
                'large' : ('stampate', 'smkeyboard', 'bolger', 'puffy', 'fire_font-k',
                           'larry3d', 'nancyj', 'poison', 'basic', 'banner', 'defleppard')}
        self.fonts = [f for v in font_sizes.values() for f in v]


        self.font_index = 0      # change to your favorite default font.
        self.font_index_old = 0  # helper to enable switching between 'term' and previous font.

    async def connect(self):
        # connect and receive the first message ( which should be: {'event' : 'state'})
        logging.debug(f"class FeishinRemote connecting to {self.url}")
        try:
            self.ws = await websockets.connect(self.url)
            self._process_message(await self.ws.recv())
        except websockets.exceptions.ConnectionClosed:
            logging.error(f"Unable to connect to server: {self.url}")
            return False

        # authenticate:
        # which we send even if username and password are not needed.
        # although not strictly necessary, this does make the server
        # close the connection immediately if it does require them.
        logging.debug(f"Authenticating {self.username}")
        token = base64.b64encode(
                f"{self.username}:{self.password}".encode()).decode()
        #  --- from the Feishin source code: const auth = json.header.split(' ')[1];
        #      the authentication header expects "word[space]token"
        #      (Like HTTP authentication does)
        #      The actual word is irrelevant, hence the tribute below.
        await self.ws.send(json.dumps({
            "event": "authenticate",
            "header": f"LlamasAss {token}"
            }))
        return True

    async def close(self):
        logging.debug("Closing connection to remote control server")
        return await self.ws.close()

    # --- prints artist - song optionally in an ascii art style using figlet.
    #     __  ___     __                ___                   __     _
    #    /  |/  /__  / /  __ __  ____  / _ \___  ___________ / /__ _(_)__
    #   / /|_/ / _ \/ _ \/ // / /___/ / ___/ _ \/ __/ __/ -_) / _ `/ / _ \
    #  /_/  /_/\___/_.__/\_, /       /_/   \___/_/  \__/\__/_/\_,_/_/_//_/
    #                   /___/
    def _print_current_song(self, fitscreen=True):
        text          = f"{self.song['artistName']} - {self.song['name']}"
        width, height = shutil.get_terminal_size(fallback=(80, 24))
        font          = self.fonts[self.font_index]
        figlet        = None
        fav_text      = ( COLOR_FAVORITE + SYMBOL_FAVORITE + ( COLOR_RESET if not
                        COLOR_TEXT else COLOR_TEXT ) ) if COLOR_FAVORITE else SYMBOL_FAVORITE

        try:
            figlet = Figlet(font=font, width=width, justify="center")
        except FontNotFound:
            logging.warning(f"Figlet font not found: {self.fonts[self.font_index]}")
            font = 'term'
            figlet = Figlet(font=font, width=width, justify="center")
        finally:
            if font == 'term' :
                figlet_text = figlet.renderText(text)[:-1] + (' ' +
                            fav_text if self.song['userFavorite'] else '')
            else:
                figlet_text = figlet.renderText(text)[:-1]
                if self.song['userFavorite'] :
                    # replace first char of middle line with ❤ ( which will _usually_ be a space )
                    text_lines = figlet_text.split('\n')
                    ln = len(text_lines) // 2
                    text_lines[ln] = fav_text + text_lines[ln][1:]
                    figlet_text = '\n'.join(text_lines)

            # check if the ascii art will fit in the current window.
            # pressing 'r' (default) to rotate the screen will set fitscreen to False,
            # so the art is printed regardless.
            lines = figlet_text.count('\n')  + 1
            if fitscreen and font != 'term' and (lines + 1) > height:
                # won't fit the screen, fall back to 'term':
                figlet = Figlet(font='term', width=width, justify="center")
                figlet_text = figlet.renderText(text)[:-1] + (' ' +
                            fav_text if self.song['userFavorite'] else '')
                lines = figlet_text.count('\n')  + 1

            # clear the screen, print the figlet
            print(('\n' * max(1, (height -1) - lines) if printlevel != 'debug' else '') +
                  f"{COLOR_TEXT}{figlet_text}{COLOR_RESET if COLOR_TEXT else ''}",
                  end=('\n' if height > 1 else ' '))

    # --- helper function to deal with possible errors or omissions in the received song data.
    def _validate_song_data(self, song):
        song = song or {}
        song['artistName'] = song.get('artistName') or 'Unknown artist'
        song['name'] = song.get('name') or 'Unknown title'
        song['album'] = song.get('album') or 'Unknown album'
        song['userFavorite'] = str(song.get('userFavorite')).lower() in ('true', 'yes', 'y', '1')
        return song
        # note: These are not all fields used,
        # some functions, notably 'toggle_favorite' and 'print_status' handle their own validation.

    # --- set window title bar to 'Feishin paused  🔊 35'
    def _set_window_title(self):
        # not sure if this is safe and it won't work on all platforms / terminals / configurations
        if SET_TITLE_BAR:
            if 'TMUX' in os.environ:
                subprocess.run(['tmux', 'select-pane', '-T',
                                f"Feishin {self.status.ljust(7)} {SYMBOL_VOLUME} {self.volume}"])
            else:
                print(f"\033]2;Feishin {self.status} {SYMBOL_VOLUME} {self.volume}\007", end="")

    # --- process messages received from the Feishin remote control server.
    def _process_message(self, message):
        event = json.loads(message)

        match event.get('event'):

            case 'state':  # sent once upon connecting.
                state = event.get('data', {})
                logging.debug('State data received from Feishin remote controller:\n' +
                              f"State update: \n{pprint.pformat(state, indent=4)}\n")

                self.song = self._validate_song_data(state.get('song'))
                if isinstance(state.get('volume'), (int, float)):
                    self.volume = int(state['volume'])
                self.status = str(state.get('status', 'Unknown'))
                if isinstance(state.get('position'), (int, float)):
                    self.song_position = state['position']
                self._print_current_song()
                self._set_window_title()

            case 'song':
                self.song = self._validate_song_data(event.get('data'))
                logging.debug('Song data received from Feishin remote controller: ' +
                              f"{self.song['artistName']} - {self.song['name']}")
                self._print_current_song()

            case 'volume':
                logging.debug('Volume data received from Feishin remote controller: ' +
                              f"{event.get('data')}")
                if isinstance(event.get('data'), (int, float)):
                    self.volume = int(event['data'])
                self._set_window_title()

            case 'playback':
                logging.debug('Playback data received from Feishin remote controller: ' +
                              f"{event.get('data')}")
                self.status = str(event.get('data', 'Unknown'))
                self._set_window_title()

            case 'position':
                #logging.debug(f"Position data received: {event['data']}")
                if isinstance(event.get('data'), (int, float)):
                    self.song_position = event['data']

            case 'repeat':
                # event['data'] 'none' 'all' 'one'
                logging.debug('Repeat data received from Feishin remote controller: ' +
                              f"{event.get('data')}")

            case 'shuffle':
                # event['data'] 'False' (I have not seen other values yet)
                logging.debug('Shuffle data received from Feishin remote controller: ' +
                              f"{event.get('data')}")

            case 'favorite':
                # event['data'] {'favorite': True / False, 'id': 'songidstring'}
                logging.debug('Favorite data received from Feishin remote controller: ' +
                              f"{event.get('data')}")
                if self.song.get('id', '') == event.get('data', {}).get('id', None) :
                    if self.song['userFavorite'] != event.get('data', {}).get('favorite', False) :
                        self.song['userFavorite'] = event.get('data', {}).get('favorite', False)
                        self._print_current_song()
                # the server only sends this event if the Favorite flag changes for the song
                # currently playing, so the check for song id is currently redundant.
                # we check anyway in case this changes in the future.

            case 'error':
                # note: have not actually seen this message yet.
                # even when actual playback errors occur.
                print('')
                logging.error('Error data received from Feishin remote controller:\n' +
                              f"{pprint.pformat(event, indent=4)}\n")

            case _:
                # note: have not actually seen a message like this yet.
                print('')
                logging.error('Unknown data received from Feishin remote controller:\n' +
                              f"{pprint.pformat(event, indent=4)}\n")

    # --- Listen loop, sends received messages to _process_message
    #     and handles connections errors.
    async def listen(self):
        # todo: the Feishin remote control server sends most of the messages twice in a row.
        #       todo is figure out why.
        try:
            # await one message to check if authentication was successful
            self._process_message(await self.ws.recv())
            # then enter the listen loop
            async for message in self.ws:
                self._process_message(message)
        except websockets.exceptions.ConnectionClosed as e:
            print('')
            logging.error('Connection error')
            if e.rcvd:
                match e.rcvd.code:
                    case 1005 | 1008:
                        # server currently sends a general 1005 for authentication failure.
                        logging.error(f"{e.rcvd.code}: Check username and password.")
                    case 4002:
                        # application codes found in: Feishin src/main/features/core/remote/index.ts
                        logging.error('4002: Username and or password changed in the remote controller.')
                    case 4000:
                        logging.error('4000: Feishin remote controller was shut down.')
                    case _:
                        logging.error(f"{e.rcvd}")
                        if e.rcvd != e.sent: logging.error(f"{e.sent}")
            else:
                # as of Feishin 1.15.1 the app does not close the connection on exit.
                logging.error(f"{e}")
                logging.error('Did Feishin stop or crash?')

            raise ConnectionLost(str(e)) from e

    # --- send sends events to the Feishin remote control server
    async def send(self, event, **kwargs):
        # https://github.com/jeffvli/feishin/blob/development/src/shared/types/remote-types.ts
        data = {'event': event}
        data.update(kwargs)
        try:
            await self.ws.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed as e:
            print('')
            logging.error(f"Error while sending to the remote controller: {e}")
            logging.error(f"Press {other_actions['quit']} to quit.")

    async def play_pause(self):
        await self.send('play') if self.status != "playing" else await self.send('pause')

    async def play(self):
        await self.send('play')

    async def pause(self):
        await self.send('pause')

    async def next(self):
        await self.send('next')

    async def previous(self):
        await self.send('previous')

    async def set_volume(self, volume):
        await self.send('volume', volume=volume)

    async def volume_up(self, stepsize=5):
        await self.send('volume', volume=min(self.volume + stepsize, 100))

    async def volume_down(self, stepsize=5):
        await self.send('volume', volume=max(self.volume - stepsize, 0))

    async def restart_song(self):
        await self.send('position', position=0)

    async def set_posistion(self, position=0):
        await self.send('position', position=position)

    async def step_forward(self, stepsize=10):
        await self.send('position', position=min(self.song.get("duration",0), self.song_position + stepsize))

    async def step_back(self, stepsize=10):
        await self.send('position', position=max(0, self.song_position - stepsize))

    # Song rating not currently implemented in Feishin.
    async def set_rating(self, song_id, rating):
        await self.send('rating', id=song_id, rating=rating)

    async def toggle_favorite(self):
        song_id = self.song.get('id', -1)
        if song_id == -1:
            logging.warning('Toggle favorite failed, no data for song.')
            return
        await self.send('favorite', favorite=not self.song['userFavorite'], id=song_id)

    async def rotate_font(self):
        self.font_index = (self.font_index + 1 ) % len(self.fonts)
        logging.debug(f"New font selected: {self.fonts[self.font_index]}\n")
        self._print_current_song(fitscreen=False)

    async def toggle_term_font(self):
        #assumes 'term' is the first font in self.fonts.
        if self.font_index == 0:
            self.font_index = self.font_index_old
        else:
            self.font_index_old = self.font_index
            self.font_index = 0
        logging.debug(f"New font selected: {self.fonts[self.font_index]}\n")
        self._print_current_song(fitscreen=False)

    async def dump_status(self):
        # note this is the received song data after processing by _validate_song_data
        pprint.pprint(self.song, indent=4)

    # --- converts seconds into m:s
    #     Feishin reports position in s, duration in ms, hence the milli flag.
    def _convert_seconds(self, seconds, milli=False):
        if isinstance(seconds, (int, float)):
            seconds = round(seconds / 1000) if milli else round(seconds)
            minutes, seconds = divmod(seconds, 60)
            return f"{round(minutes)}:{str(round(seconds)).zfill(2)}"
        return "?"

    async def print_status(self):
        w1 = 12
        year = self.song.get('releaseYear')
        year = f" ({year})" if year else ''
        print(f"\n{'Artist':<{w1}} {self.song['artistName']}")
        print(f"{'Album':<{w1}} {self.song['album']}")
        print(f"{'Song' + (' ' +  SYMBOL_FAVORITE if self.song['userFavorite'] else ''):<{w1}} " +
              f"{self.song['name']}" + year)
        print(f"{'Filetype':<{w1}} {self.song.get('container', '??')} " +
              f"{self.song.get('bitRate', '??')} kbps")
        print(f"{self.status.replace('p','P'):<{w1}} " +
              f"{self._convert_seconds(self.song_position)}-" +
              f"{self._convert_seconds(self.song.get('duration'), milli=True):<{w1}} " +
              f"{SYMBOL_VOLUME} {self.volume}")


# --- Exception thrown by FeishinRemote.
class ConnectionLost(Exception):
    pass


# --- check if Feishin is running
#     by checking whether the remote control server is running.
async def feishin_running():
    try:
        ws = await websockets.connect(hostname)
        await ws.close()
        return True
    except:
        return False

async def request_shutdown(controller):
    logging.debug('Exiting application')
    shutdown_event.set()
    ret = await controller.close()
    logging.debug(f"Controller exit code: {ret}")
    if prompt_task is not None:
        prompt_task.cancel()

async def interactive(controller):
    kb = KeyBindings()
    # for all simple_controller_actions, the block below
    # generates declarations like these:
    # @kb.add(simple_controller_actions["next"])
    # def _(event):
    #     asyncio.create_task(controller.next())
    for function, keypress in simple_controller_actions.items():
        method = getattr(controller, function)
        def make_handler(method):
            @kb.add(keypress)
            def handler(event):
                asyncio.create_task(method())
            return handler
        make_handler(method)

    @kb.add(other_actions["quit"])
    def _(event):
        asyncio.create_task(request_shutdown(controller))

    @kb.add(other_actions["help"])
    def _(event):
        trans_table = str.maketrans({"'": "", ",": "", "_": " ", "{": "\n ", "}": ""})
        print(pprint.pformat(simple_controller_actions | other_actions,
                             sort_dicts=False, indent=4).translate(trans_table))

    session = PromptSession(key_bindings=kb)

    while not shutdown_event.is_set():
        await session.prompt_async("")

async def listen_wrapper(controller):
    # used like this so a connection loss automatically shuts down the keyboard listener.
    try:
        await controller.listen()
    except ConnectionLost:
        shutdown_event.set()

async def main():
    # --- logging
    setup_logging()
    # --- find or start Feishin remote control server
    logging.info('Checking for active Feishin remote control server')
    running = await feishin_running()
    if not running:
        if hostname.startswith(('ws://localhost', 'ws://127.0.0.1', 'ws://0.0.0.0', 'ws://[::1]')):
            # local connection.
            logging.info("Server not found, attempting to start Feishin")
            start_feishin() # will exit application if unsuccessfull
            for _ in range(5):
                running = await feishin_running()
                if not running:
                    await asyncio.sleep(1)
                else:
                    break
        if not running:
            logging.error(f"Unable to connect to remote control server at: {hostname}\n" +
                          "Check whether it's enabled in Feishin settings.")
            exit(1)
    logging.info('Feishin remote control server active.')

    # --- initialise remote controller.
    feishin = FeishinRemote(
        hostname, username, password)

    listen_task = None
    interactive_task = None

    # --- connect to remote controller
    if not await feishin.connect():
        exit(1)
    # --- launch websocket listener
    listen_task = asyncio.create_task(listen_wrapper(feishin))

    # --- launch keyboard listener
    interactive_task = asyncio.create_task(interactive(feishin))

    # --- wait until the application closes,
    # either because the user pressed 'q' or the websocket died.
    await shutdown_event.wait()

def run_cli():
    asyncio.run(main())

if __name__ == "__main__":
    run_cli()
