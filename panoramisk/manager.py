# -*- coding: utf-8 -*-
from collections import defaultdict
import re
import fnmatch
from .message import Message
from .utils import asyncio
from . import actions
from . import utils
import logging
import time


class Connection(asyncio.Protocol):

    def connection_made(self, transport):
        self.transport = transport
        self.closed = False
        self.queue = utils.Queue()
        self.responses = {}
        self.commands = {}
        self.factory = None
        self.log = logging.getLogger(__name__)

    def send(self, data, as_list=False):
        if not isinstance(data, actions.Action):
            if 'Command' in data:
                klass = actions.Command
            else:
                klass = actions.Action
            data = klass(data, as_list=as_list)
        self.transport.write(str(data).encode('utf8'))
        self.responses[data.id] = data
        return data.future

    def data_received(self, data):
        encoding = getattr(self, 'encoding', 'ascii')
        data = data.decode(encoding, 'ignore')
        # Very verbose, uncomment only if necessary
        # self.log.debug('data received: "%s"', data)
        if not self.queue.empty():
            data = self.queue.get_nowait() + data
        lines = data.split(utils.EOL+utils.EOL)
        self.queue.put_nowait(lines.pop(-1))
        for line in lines:
            # Because sometimes me receive only one EOL from Asterisk
            line = line.strip()
            # Very verbose, uncomment only if necessary
            # self.log.debug('message received: "%s"', line)
            message = Message.from_line(line)
            self.log.debug('message interpreted: %r', message)
            if message is None:
                continue

            response = self.responses.get(message.id)
            if response is not None:
                if response.add_message(message):
                    # completed; dequeue
                    self.responses.pop(response.id)
            elif 'Event' in message:
                self.factory.dispatch(message)

    def connection_lost(self, exc):  # pragma: no cover
        if not self.closed:
            self.close()
            # wait a few before reconnect
            time.sleep(2)
            # reconnect
            self.factory.connect()

    def close(self):  # pragma: no cover
        if not self.closed:
            try:
                self.transport.close()
            finally:
                self.closed = True


class Manager(object):
    """Main object:

    .. code-block:: python

        >>> manager = Manager(
        ...    host='127.0.0.1',
        ...    port=5038,
        ...    ssl=False,
        ...    encoding='utf8')
    """

    defaults = dict(
        host='127.0.0.1',
        port=5038,
        ssl=False,
        encoding='utf8',
        connection_class=Connection,
        loop=None,
    )

    def __init__(self, **config):
        self.config = dict(self.defaults, **config)
        self.loop = self.config['loop']
        self.log = config.get('log', logging.getLogger(__name__))
        self.callbacks = defaultdict(list)
        self.protocol = None
        self.patterns = []
        self.authenticated = False
        self.authenticated_future = None

    def connection_made(self, f):
        if getattr(self, 'protocol', None):
            self.protocol.close()
        try:
            transport, protocol = f.result()
        except OSError as e:  # pragma: no cover
            self.log.exception(e)
            self.loop.call_later(2, self.connect)
        else:
            self.log.info('Manager connected')
            self.protocol = protocol
            self.protocol.queue = utils.Queue(loop=self.loop)
            self.protocol.factory = self
            self.protocol.log = self.log
            self.protocol.config = self.config
            self.protocol.encoding = self.encoding = self.config['encoding']
            self.responses = self.protocol.responses = {}
            if 'username' in self.config:
                self.authenticated = False
                self.authenticated_future = self.send_action({
                    'Action': 'Login',
                    'Username': self.config['username'],
                    'Secret': self.config['secret']})
                self.authenticated_future.add_done_callback(self.login)
            self.loop.call_later(10, self.ping)

    def login(self, future):
        self.authenticated_future = None
        resp = future.result()
        self.authenticated = bool(resp.success)
        return self.authenticated

    def ping(self):  # pragma: no cover
        self.protocol.send({'Action': 'Ping'})
        self.loop.call_later(10, self.ping)

    def send_action(self, action, as_list=False, **kwargs):
        """Send an :class:`~panoramisk.actions.Action` to the server:

        :param action: an Action or dict with action name and parameters to
                       send
        :type action: Action or dict
        :param as_list: If True, the action Future will retrieve all responses
        :type as_list: boolean
        :return: a Future that will receive the response
        :rtype: asyncio.Future

        :Example:

            To retrieve answer in a coroutine:

                manager = Manager()

                resp = yield from manager.send_action({'Action': 'Status'})

            With a callback:

                manager = Manager()

                future = manager.send_action({'Action': 'Status'})

                future.add_done_callback(handle_status_response)

        See https://wiki.asterisk.org/wiki/display/AST/AMI+Actions for
        more information on actions
        """
        action.update(kwargs)
        return self.protocol.send(action, as_list=as_list)

    def send_command(self, command, agi=False, as_list=False):
        """Send a :class:`~panoramisk.actions.Command` to the server::

            manager = Manager()
            resp = manager.send_command('http show status')

        Return a response :class:`~panoramisk.message.Message`.
        See https://wiki.asterisk.org/wiki/display/AST/ManagerAction_Command
        """
        if agi:
            action = actions.Command({'Command': command, 'Action': 'AGI'},
                                     as_list=as_list)
        else:
            action = actions.Action({'Command': command, 'Action': 'Command'},
                                    as_list=as_list)
        return self.send_action(action)

    def connect(self, loop=None):
        """connect to the server"""
        if self.loop is None:  # pragma: no cover
            self.loop = asyncio.get_event_loop()
        t = asyncio.Task(
            self.loop.create_connection(
                self.config['connection_class'],
                self.config['host'], self.config['port'],
                ssl=self.config['ssl']),
            loop=self.loop)
        t.add_done_callback(self.connection_made)
        return t

    def register_event(self, pattern, callback=None):
        """register an event. See :class:`~panoramisk.message.Message`:

        .. code-block:: python

            >>> def callback(event, manager):
            ...     print(event, manager)
            >>> manager = Manager()
            >>> manager.register_event('Meetme*', callback)
            <function callback at 0x...>

        You can also use the manager as a decorator:

        .. code-block:: python

            >>> manager = Manager()
            >>> @manager.register_event('Meetme*')
            ... def callback(event, manager):
            ...     print(event, manager)
        """
        def _register_event(callback):
            self.patterns.append((pattern,
                                 re.compile(fnmatch.translate(pattern))))
            self.callbacks[pattern].append(callback)
            return callback
        if callback is not None:
            return _register_event(callback)
        else:
            return _register_event

    def dispatch(self, event):
        matches = []
        event.manager = self
        for pattern, regexp in self.patterns:
            match = regexp.match(event.event)
            if match is not None:
                matches.append(pattern)
                for callback in self.callbacks[pattern]:
                    for callback in self.callbacks[pattern]:
                        callback(event, self)
        return matches

    def close(self):
        """Close the connection"""
        if getattr(self, 'protocol', None):
            self.protocol.close()
